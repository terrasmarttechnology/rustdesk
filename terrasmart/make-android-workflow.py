#!/usr/bin/env python3
"""Writes .github/workflows/terrasmart-android.yml from upstream's flutter-build.yml.

TerraSmart Destek only ships the Android app, so the workflow keeps upstream's
Android jobs (bridge, per-ABI APKs, universal APK) as they are and changes how
they are signed and published:

- the build stops when the TerraSmart signing key is missing, instead of
  publishing an APK signed with a throwaway key (how the field's 1.4.5 lost
  its key);
- every signed APK must carry TerraSmart's certificate before it is uploaded;
- the release is tagged destek-<version>-r<revision>.

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
            'got=$("$tools/apksigner" verify --print-certs "$f" | sed -n \'s/^Signer #1 certificate SHA-256 digest: //p\')\n'
            'echo "Signed with $got"\n'
            'if [ "$got" != "$TERRASMART_CERT_SHA256" ]; then\n'
            '  echo "::error::$f is not signed with the TerraSmart key"\n'
            "  exit 1\n"
            "fi\n"
        ),
    }


def adapt(job):
    job.pop("if", None)  # the universal APK is always built
    needs = job.get("needs", [])
    job["needs"] = (needs if isinstance(needs, list) else [needs]) + ["check-signing"]
    steps = []
    for step in job["steps"]:
        name = step.get("name", "")
        if name == "Publish unsigned apk package":
            continue
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
