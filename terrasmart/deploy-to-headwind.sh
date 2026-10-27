#!/usr/bin/env bash
# Uploads a TerraSmart Destek APK to Headwind MDM as a new version of the
# "TerraSmart Destek" application and sets it to install in one configuration.
# Same Headwind calls as terrasmart-vera-android/scripts/deploy-to-mdm.sh.
#
# Usage: terrasmart/deploy-to-headwind.sh <apk> <configuration name>
# Needs HEADWIND_MDM_URL, HEADWIND_MDM_USERNAME and HEADWIND_MDM_PASSWORD.
set -euo pipefail

APK="$1"
CONFIG_NAME="$2"
PKG="tr.com.terrasmart.rustdesk"
APP_NAME="TerraSmart Destek"

: "${HEADWIND_MDM_URL:?HEADWIND_MDM_URL is required}"
: "${HEADWIND_MDM_USERNAME:?HEADWIND_MDM_USERNAME is required}"
: "${HEADWIND_MDM_PASSWORD:?HEADWIND_MDM_PASSWORD is required}"
URL="${HEADWIND_MDM_URL%/}"
JAR=$(mktemp)
trap 'rm -f "$JAR"' EXIT

fail() { echo "::error::$1"; shift; for l in "$@"; do echo "$l"; done; exit 1; }

# Headwind wants the password as an uppercase MD5.
PASSWORD_MD5=$(printf '%s' "$HEADWIND_MDM_PASSWORD" | md5sum | awk '{print toupper($1)}')
AUTH=$(curl -sS -c "$JAR" -X POST "$URL/rest/public/auth/login" -H 'Content-Type: application/json' \
    -d "$(jq -n --arg login "$HEADWIND_MDM_USERNAME" --arg password "$PASSWORD_MD5" '{login: $login, password: $password}')")
[ "$(jq -r '.status' <<<"$AUTH")" = OK ] || fail "Headwind login failed" "$AUTH"
CUSTOMER_ID=$(jq -r '.data.customerId // 1' <<<"$AUTH")

UPLOAD=$(curl -sS -b "$JAR" -X POST "$URL/rest/private/web-ui-files" -F "file=@$APK")
[ "$(jq -r '.status' <<<"$UPLOAD")" = OK ] || fail "APK upload failed" "$UPLOAD"
FILE_PATH=$(jq -r '.data.serverPath' <<<"$UPLOAD")
VERSION=$(jq -r '.data.fileDetails.version' <<<"$UPLOAD")
VERSION_CODE=$(jq -r '.data.fileDetails.versionCode' <<<"$UPLOAD")
UPLOADED_PKG=$(jq -r '.data.fileDetails.pkg // empty' <<<"$UPLOAD")
[ -z "$UPLOADED_PKG" ] || [ "$UPLOADED_PKG" = "$PKG" ] || fail "Uploaded APK is $UPLOADED_PKG, not $PKG"
echo "Uploaded $(basename "$APK"): $VERSION ($VERSION_CODE)"

APPS=$(curl -sS -b "$JAR" "$URL/rest/private/applications/search")
APP_ID=$(jq -r --arg pkg "$PKG" '[.data[] | select(.pkg == $pkg) | .id][0] // empty' <<<"$APPS")
if [ -n "$APP_ID" ]; then
    RESP=$(curl -sS -b "$JAR" -X PUT "$URL/rest/private/applications/versions" -H 'Content-Type: application/json' \
        -d "$(jq -n --argjson app "$APP_ID" --arg file "$FILE_PATH" --arg version "$VERSION" --argjson code "$VERSION_CODE" \
            '{applicationId: $app, filePath: $file, version: $version, versionCode: $code}')")
    [ "$(jq -r '.status' <<<"$RESP")" = OK ] || fail "Adding the version failed" "$RESP"
    VERSION_ID=$(jq -r '.data.id' <<<"$RESP")
    echo "Added version $VERSION_ID to application $APP_ID"
else
    RESP=$(curl -sS -b "$JAR" -X PUT "$URL/rest/private/applications/android" -H 'Content-Type: application/json' \
        -d "$(jq -n --arg name "$APP_NAME" --arg pkg "$PKG" --arg file "$FILE_PATH" --arg version "$VERSION" \
            --argjson code "$VERSION_CODE" --argjson customer "$CUSTOMER_ID" \
            '{name: $name, pkg: $pkg, filePath: $file, version: $version, versionCode: $code, showIcon: true, action: 1, type: "app", customerId: $customer}')")
    [ "$(jq -r '.status' <<<"$RESP")" = OK ] || fail "Creating the application failed" "$RESP"
    VERSION_ID=$(jq -r '.data.latestVersion' <<<"$RESP")
    echo "Created application $(jq -r '.data.id' <<<"$RESP") ($APP_NAME)"
fi

# The install link belongs to the version (a new version starts as "do not
# install" everywhere). The server replaces all links of the version with the
# list sent, so existing ones are kept; notify pushes the change to devices.
CONFIGS=$(curl -sS -b "$JAR" "$URL/rest/private/applications/version/$VERSION_ID/configurations")
CONFIG_ID=$(jq -r --arg name "$CONFIG_NAME" '[.data[] | select(.configurationName == $name) | .configurationId][0] // empty' <<<"$CONFIGS")
[ -n "$CONFIG_ID" ] || fail "Headwind has no configuration named '$CONFIG_NAME'" "$CONFIGS"
ASSIGN=$(jq --argjson version "$VERSION_ID" --argjson config "$CONFIG_ID" \
    '{applicationVersionId: $version,
      configurations: [.data[]
        | if .configurationId == $config then .action = 1 | .remove = false | .notify = true else . end
        | select((.action // 0) != 0)]}' <<<"$CONFIGS")
RESP=$(curl -sS -b "$JAR" -X POST "$URL/rest/private/applications/version/configurations" \
    -H 'Content-Type: application/json' -d "$ASSIGN")
[ "$(jq -r '.status' <<<"$RESP")" = OK ] || fail "Assigning the version to '$CONFIG_NAME' failed" "$RESP"
echo "$APP_NAME $VERSION ($VERSION_CODE) set to install in '$CONFIG_NAME'"
