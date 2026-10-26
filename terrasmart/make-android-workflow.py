#!/usr/bin/env python3
"""Writes .github/workflows/terrasmart-android.yml from upstream's flutter-build.yml.

TerraSmart Destek only ships the Android app, so the workflow keeps upstream's
Android jobs (bridge, per-ABI APKs, universal APK) as they are and changes how
they are signed and published:

- the build stops when the TerraSmart signing key is missing, instead of
  publishing an APK signed with a throwaway key (how the field's 1.4.5 lost
  its key);
- every signed APK must carry TerraSmart's certificate before it is uploaded;
- the release is tagged destek-<version>-r<revision>;
- the secret TERRASMART_RUSTDESK_PASSWORD, when set, becomes the app's preset
  permanent password; only its salted hash reaches the build;
- apt gives up on a stalled mirror after seconds instead of minutes (runners'
  Azure mirror sometimes hangs, which stretched builds from ~35 min to 2 h);
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
JOBS = ["generate-bridge", "build-rustdesk-android", "build-rustdesk-android-universal"]
VCPKG_CACHE = "${{ github.workspace }}-vcpkg-bincache"
ACTIONS_CACHE = "actions/cache@caa296126883cff596d87d8935842f9db880ef25"  # v5.1.0


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
                }
            ],
        }
    }
    for name in JOBS:
        job = src["jobs"][name]
        jobs[name] = job if name == "generate-bridge" else adapt(job)

    out = {
        "name": "TerraSmart Destek Android",
        "on": {
            "workflow_dispatch": {
                "inputs": {
                    "revision": {
                        "description": "TerraSmart revision on this upstream version (0, 1, ...)",
                        "default": "0",
                        "required": True,
                    }
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
