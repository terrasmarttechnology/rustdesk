#!/usr/bin/env python3
"""Writes .github/workflows/terrasmart-android.yml from upstream's flutter-build.yml.

TerraSmart Destek only ships the universal Android APK for the VERA tablets, so
the workflow keeps upstream's Android jobs (bridge, per-ABI libraries, universal
APK) and changes what they build and how it is signed and published:

- only arm64-v8a and armeabi-v7a are built (the tablets are ARM; x86_64 only
  served emulators and was the slowest job), and the per-ABI jobs stop at the
  library: the per-ABI APKs carried an ABI versionCode offset that would block
  updates to the universal APK, so they are not published;
- the library jobs skip the JDK in upstream's apt list (only the APK build uses
  it, and it is the package a stalled mirror held longest);
- the universal APK goes to Headwind MDM as "TerraSmart Destek" and is set to
  install in the configuration named by the headwind_configuration input
  (deploy-to-headwind.sh);
- the build stops when the TerraSmart signing key is missing, instead of
  publishing an APK signed with a throwaway key (how the field's 1.4.5 lost
  its key);
- every signed APK must carry TerraSmart's certificate before it is uploaded;
- the release is tagged destek-<version>-r<revision>;
- the secret TERRASMART_RUSTDESK_PASSWORD, when set, becomes the app's preset
  permanent password; only its salted hash reaches the build;
- apt gives up on a stalled mirror after seconds instead of minutes, and the
  .deb files are kept with actions/cache, because the runners' Azure mirror
  sometimes crawls (100 MB in half an hour), which stretched builds to 2 h;
- vcpkg keeps its built packages in a file cache saved with actions/cache,
  because the x-gha backend upstream uses was removed from vcpkg.

Run it again after moving the branch onto a new upstream release:

    python3 terrasmart/make-android-workflow.py
"""

import pathlib
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / ".github/workflows/flutter-build.yml"
OUT = ROOT / ".github/workflows/terrasmart-android.yml"

# SHA-256 of terrasmart-rustdesk.jks's certificate (CN=Terrasmart, valid to 2053).
CERT_SHA256 = "b86ff2fa10fbd23712bc22142aff253ce4d2cd5992f6e1a9791dc6042a2dec37"
VCPKG_CACHE = "${{ github.workspace }}-vcpkg-bincache"
APT_CACHE = "${{ runner.temp }}/apt-archives"
ACTIONS_CACHE = "actions/cache@caa296126883cff596d87d8935842f9db880ef25"  # v5.1.0
ABIS = ["aarch64-linux-android", "armv7-linux-androideabi"]
# What deploy-to-headwind.sh reads, from this repository's secrets.
HEADWIND_ENV = {
    "HEADWIND_MDM_URL": "${{ secrets.HEADWIND_MDM_URL }}",
    "HEADWIND_MDM_USERNAME": "${{ secrets.HEADWIND_LOGIN }}",
    "HEADWIND_MDM_PASSWORD": "${{ secrets.HEADWIND_PASSWORD }}",
}


class Literal(str):
    pass


def literal(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


def plain(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


yaml.add_representer(Literal, literal)
yaml.add_representer(str, plain)


def check_cert_step():
    return {
        "name": "Check the signing certificate",
        "shell": "bash",
        "run": Literal(
            'f="${{ steps.sign-rustdesk.outputs.signedReleaseFile }}"\n'
            'tools="/usr/local/lib/android/sdk/build-tools/${{ env.ANDROID_SIGN_TOOL_VERSION }}"\n'
            'out=$("$tools/apksigner" verify --print-certs "$f" 2>&1 || true)\n'
            "got=$(printf '%s\\n' \"$out\" | grep -oiE 'certificate SHA-256 digest: [0-9a-f]{64}' | head -1 | awk '{print $NF}' | tr 'A-F' 'a-f')\n"
            'echo "$f is signed with ${got:-an unreadable certificate}"\n'
            'if [ "$got" != "$TERRASMART_CERT_SHA256" ]; then\n'
            "  printf '%s\\n' \"$out\" | grep -v '^WARNING'\n"
            '  echo "::error::$f is not signed with the TerraSmart key"\n'
            "  exit 1\n"
            "fi\n"
        ),
    }


def preset_password_step():
    return {
        "name": "Preset the TerraSmart password",
        "shell": "bash",
        "env": {"PASSWORD": "${{ secrets.TERRASMART_RUSTDESK_PASSWORD }}"},
        "run": Literal(
            'if [ -z "$PASSWORD" ]; then\n'
            '  echo "::warning::TERRASMART_RUSTDESK_PASSWORD is not set; the app has no preset password"\n'
            "  exit 0\n"
            "fi\n"
            "salt=$(openssl rand -hex 8)\n"
            "hash=\"00$(printf '%s%s' \"$PASSWORD\" \"$salt\" | openssl dgst -sha256 -binary | base64)\"\n"
            'echo "::add-mask::$hash"\n'
            'echo "TERRASMART_PRESET_PASSWORD_SALT=$salt" >> "$GITHUB_ENV"\n'
            'echo "TERRASMART_PRESET_PASSWORD_HASH=$hash" >> "$GITHUB_ENV"\n'
        ),
    }


def apt_timeouts_step():
    return {
        "name": "Make apt skip stalled mirrors",
        "shell": "bash",
        "run": Literal(
            "printf '%s\\n' 'Acquire::http::Timeout \"15\";' 'Acquire::https::Timeout \"15\";' 'Acquire::Retries \"5\";' \\\n"
            "  | sudo tee /etc/apt/apt.conf.d/99terrasmart-timeouts\n"
        ),
    }


def vcpkg_cache_steps():
    key = "vcpkg-bincache-${{ matrix.job.target }}-${{ env.VCPKG_COMMIT_ID }}-"
    return [
        {
            "name": "Restore the vcpkg binary cache",
            "uses": ACTIONS_CACHE,
            "with": {
                "path": VCPKG_CACHE,
                "key": key + "${{ hashFiles('vcpkg.json') }}",
                "restore-keys": key,
            },
        },
        {
            "name": "Create the vcpkg binary cache directory",
            "shell": "bash",
            "run": f'mkdir -p "{VCPKG_CACHE}"\n',
        },
    ]


def deploy_step():
    return {
        "name": "Deploy to Headwind MDM",
        "if": "inputs.headwind_configuration != ''",
        "shell": "bash",
        "env": dict(HEADWIND_ENV),
        "run": Literal(
            'bash terrasmart/deploy-to-headwind.sh "${{ steps.sign-rustdesk.outputs.signedReleaseFile }}" '
            '"${{ inputs.headwind_configuration }}"\n'
        ),
    }


def replace_once(text, old, new):
    if old not in text:
        raise SystemExit(f"upstream's workflow changed; not found: {old!r}")
    return text.replace(old, new)


def apt_cache_step():
    return {
        "name": "Restore the apt packages",
        "uses": ACTIONS_CACHE,
        "with": {
            "path": APT_CACHE,
            "key": "apt-${{ github.job }}-${{ hashFiles('.github/workflows/terrasmart-android.yml') }}",
            "restore-keys": "apt-${{ github.job }}-",
        },
    }


def cache_apt_downloads(step):
    # apt takes the .deb files it already has from APT_CACHE and leaves new ones
    # there; they belong to the runner user again so actions/cache can save them.
    run = replace_once(
        step["run"],
        "sudo apt-get install -y ",
        f'mkdir -p "{APT_CACHE}/partial"\nsudo apt-get install -y -o Dir::Cache::Archives="{APT_CACHE}" ',
    )
    step["run"] = Literal(run.rstrip("\n") + f'\nsudo chown -R "$(id -u):$(id -g)" "{APT_CACHE}"\n')


def drop_jdk(job):
    # The rest of upstream's list is needed: bindgen finds bits/*.h only with gcc-multilib.
    step = next(s for s in job["steps"] if s.get("name") == "Install dependencies")
    step["run"] = Literal(replace_once(step["run"], "     openjdk-17-jdk-headless \\\n", ""))


def lib_job(job):
    matrix = job["strategy"]["matrix"]
    matrix["job"] = [j for j in matrix["job"] if j["target"] in ABIS]
    if sorted(j["target"] for j in matrix["job"]) != sorted(ABIS):
        raise SystemExit(f"upstream's Android matrix changed: {matrix['job']}")
    names = [s.get("name") for s in job["steps"]]
    last = names.index("Upload Rustdesk library to Artifacts")
    job["steps"] = job["steps"][: last + 1]
    drop_jdk(job)
    return job


def apk_job(job):
    job["steps"] = [
        s for s in job["steps"]
        if s.get("with", {}).get("name") != "librustdesk.so.x86_64-linux-android"
    ]
    build = next(s for s in job["steps"] if s.get("name") == "Build rustdesk")
    run = build["run"]
    for line in (
        "mv ./flutter/android/app/src/main/jniLibs/x86_64/liblibrustdesk.so ./flutter/android/app/src/main/jniLibs/x86_64/librustdesk.so\n",
        "cp ${ANDROID_NDK_HOME}/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib/x86_64-linux-android/libc++_shared.so ./flutter/android/app/src/main/jniLibs/x86_64/\n",
    ):
        run = replace_once(run, line, "")
    run = replace_once(
        run,
        "--target-platform android-arm64,android-arm,android-x64${{ env.x86_target }}",
        "--target-platform android-arm64,android-arm",
    )
    build["run"] = Literal(run)
    job["steps"].append(deploy_step())
    return job


def adapt(job):
    job.pop("if", None)  # the universal APK is always built
    needs = job.get("needs", [])
    job["needs"] = (needs if isinstance(needs, list) else [needs]) + ["check-signing"]
    steps = []
    for step in job["steps"]:
        name = step.get("name", "")
        if name == "Publish unsigned apk package":
            continue
        if name == "Install dependencies":
            steps.append(apt_timeouts_step())
            steps.append(apt_cache_step())
            cache_apt_downloads(step)
        if name == "Setup vcpkg with Github Actions binary cache":
            steps.extend(vcpkg_cache_steps())
        if name == "Build rustdesk lib":
            steps.append(preset_password_step())
        if name == "Sign app APK":
            step.pop("if", None)
            steps.append(step)
            steps.append(check_cert_step())
            continue
        if name in ("Upload Artifacts", "Publish signed apk package"):
            step["if"] = "env.UPLOAD_ARTIFACT == 'true'"
        if name == "Publish signed apk package":
            # GITHUB_TOKEN may not tag a commit whose workflows differ from the default
            # branch's (403), which is every build of a terrasmart-<release> branch.
            step["with"]["token"] = "${{ secrets.TERRASMART_BOT_TOKEN || github.token }}"
            step["with"]["target_commitish"] = "${{ github.sha }}"
            step["with"]["name"] = "TerraSmart Destek ${{ env.VERSION }} r${{ inputs.revision }}"
        steps.append(step)
    job["steps"] = steps
    return job


def main():
    src = yaml.safe_load(SRC.read_text())
    env = dict(src["env"])
    version = env["VERSION"]
    env["TAG_NAME"] = f"destek-{version}-r${{{{ inputs.revision }}}}"
    env["UPLOAD_ARTIFACT"] = "true"
    env["TERRASMART_CERT_SHA256"] = CERT_SHA256
    env["VCPKG_BINARY_SOURCES"] = f"clear;files,{VCPKG_CACHE},readwrite"
    # Gradle reads -PterrasmartRevision from this (versionCode = upstream * 100 + revision).
    env["ORG_GRADLE_PROJECT_terrasmartRevision"] = "${{ inputs.revision }}"
    for k in ("MACOS_P12_BASE64", "SIGN_BASE_URL"):
        env.pop(k, None)

    jobs = {
        "check-signing": {
            "runs-on": "ubuntu-24.04",
            "steps": [
                {
                    "name": "Require the TerraSmart signing key",
                    "env": {
                        "KEY": "${{ secrets.ANDROID_SIGNING_KEY }}",
                        "ALIAS": "${{ secrets.ANDROID_ALIAS }}",
                        "STORE_PASSWORD": "${{ secrets.ANDROID_KEY_STORE_PASSWORD }}",
                        "KEY_PASSWORD": "${{ secrets.ANDROID_KEY_PASSWORD }}",
                    },
                    "run": Literal(
                        "for v in KEY ALIAS STORE_PASSWORD KEY_PASSWORD; do\n"
                        '  if [ -z "${!v}" ]; then echo "::error::Signing secret $v is missing; refusing to build"; exit 1; fi\n'
                        "done\n"
                    ),
                },
                {
                    "name": "Require the Headwind MDM login",
                    "if": "inputs.headwind_configuration != ''",
                    "env": dict(HEADWIND_ENV),
                    "run": Literal(
                        "for v in HEADWIND_MDM_URL HEADWIND_MDM_USERNAME HEADWIND_MDM_PASSWORD; do\n"
                        '  if [ -z "${!v}" ]; then echo "::error::A Headwind secret (HEADWIND_MDM_URL, HEADWIND_LOGIN, HEADWIND_PASSWORD) is missing; add it or leave headwind_configuration empty"; exit 1; fi\n'
                        "done\n"
                    ),
                },
            ],
        }
    }
    jobs["generate-bridge"] = src["jobs"]["generate-bridge"]
    jobs["build-rustdesk-android"] = adapt(lib_job(src["jobs"]["build-rustdesk-android"]))
    jobs["build-rustdesk-android-universal"] = adapt(apk_job(src["jobs"]["build-rustdesk-android-universal"]))

    out = {
        "name": "TerraSmart Destek Android",
        "on": {
            "workflow_dispatch": {
                "inputs": {
                    "revision": {
                        "description": "TerraSmart revision on this upstream version (0, 1, ...)",
                        "default": "0",
                        "required": True,
                    },
                    "headwind_configuration": {
                        "description": "Headwind configuration to install it in (empty: do not deploy)",
                        "default": "VERA Development",
                        "required": False,
                    },
                }
            }
        },
        "permissions": {"contents": "write"},
        "env": env,
        "jobs": jobs,
    }
    header = (
        "# Generated by terrasmart/make-android-workflow.py from upstream's\n"
        f"# flutter-build.yml (RustDesk {version}). Edit the script, not this file.\n"
    )
    OUT.write_text(header + yaml.dump(out, sort_keys=False, width=1000, allow_unicode=True))
    print(f"wrote {OUT.relative_to(ROOT)} for RustDesk {version}")


if __name__ == "__main__":
    main()
