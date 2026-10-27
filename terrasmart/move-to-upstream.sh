#!/usr/bin/env bash
# Moves TerraSmart Destek onto a RustDesk release: replays the TerraSmart commits
# of hbb_common and of this repository on the release, points libs/hbb_common at
# the replayed hbb_common, regenerates the Android workflow, and pushes both as
# terrasmart-<release>. The terrasmart branch itself moves only by hand, after
# that build was tried on the VERA Development tablets (README.md).
#
# Usage: terrasmart/move-to-upstream.sh <release tag> [--no-push]
# Run in a clone of terrasmarttechnology/rustdesk that has the terrasmart branch
# with full history. Exits with 2 when the commits do not apply cleanly.
set -euo pipefail

NEW="$1"
PUSH=true
[ "${2:-}" = --no-push ] && PUSH=false
BRANCH="terrasmart-$NEW"
OURS_REF="${TERRASMART_REF:-terrasmart}"
UPSTREAM=https://github.com/rustdesk/rustdesk
HB_UPSTREAM=https://github.com/rustdesk/hbb_common

cd "$(git rev-parse --show-toplevel)"
OURS=$(git rev-parse "$OURS_REF^{commit}")
OLD=$(git show "$OURS:.github/workflows/terrasmart-android.yml" | sed -n 's/^  VERSION: *//p' | tr -d "'\"")
HB_URL=$(git config --blob "$OURS:.gitmodules" submodule.libs/hbb_common.url)

stop() { echo "::error::$1"; shift; [ $# -gt 0 ] && git -C "$1" status --short | head -40; exit 2; }

for tag in "$OLD" "$NEW"; do
    git rev-parse -q --verify "refs/tags/$tag" >/dev/null ||
        git fetch --quiet --no-tags "$UPSTREAM" "refs/tags/$tag:refs/tags/$tag"
done
HB_OLD=$(git rev-parse "refs/tags/$OLD:libs/hbb_common")
HB_NEW=$(git rev-parse "refs/tags/$NEW:libs/hbb_common")
HB_OURS=$(git rev-parse "$OURS:libs/hbb_common")
echo "Moving TerraSmart Destek from $OLD to $NEW"

# hbb_common: our commits since the one $OLD used, on the one $NEW uses.
HB=$(mktemp -d)
trap 'rm -rf "$HB"' EXIT
git clone --quiet --no-checkout "$HB_URL" "$HB"
git -C "$HB" fetch --quiet "$HB_UPSTREAM" "$HB_NEW" "$HB_OLD"
git -C "$HB" checkout --quiet -B "$BRANCH" "$HB_NEW"
for c in $(git -C "$HB" rev-list --reverse "$HB_OLD..$HB_OURS"); do
    git -C "$HB" cherry-pick "$c" >/dev/null 2>&1 ||
        stop "hbb_common: $(git -C "$HB" log -1 --format='%h %s' "$c") does not apply on $NEW's hbb_common ${HB_NEW:0:7}" "$HB"
done
HB_TIP=$(git -C "$HB" rev-parse HEAD)

# This repository: our commits since $OLD, on $NEW. Both sides move the
# submodule, so a conflict there alone is settled with the replayed hbb_common.
git checkout --quiet -B "$BRANCH" "refs/tags/$NEW"
for c in $(git rev-list --reverse "refs/tags/$OLD..$OURS"); do
    git cherry-pick "$c" >/dev/null 2>&1 && continue
    unmerged=$(git diff --name-only --diff-filter=U)
    if [ -z "$unmerged" ]; then
        git cherry-pick --skip  # already in $NEW
        continue
    fi
    [ "$unmerged" = libs/hbb_common ] ||
        stop "$(git log -1 --format='%h %s' "$c") does not apply on $NEW" .
    git update-index --cacheinfo "160000,$HB_TIP,libs/hbb_common"
    GIT_EDITOR=true git cherry-pick --continue >/dev/null
done
git update-index --cacheinfo "160000,$HB_TIP,libs/hbb_common"
python3 terrasmart/make-android-workflow.py || stop "make-android-workflow.py cannot handle $NEW's flutter-build.yml"
git add .github/workflows/terrasmart-android.yml
git diff --cached --quiet || git commit --quiet -m "Move TerraSmart Destek to RustDesk $NEW"

echo "$BRANCH: $(git rev-parse --short HEAD), hbb_common $BRANCH: ${HB_TIP:0:7}"
if $PUSH; then
    git -C "$HB" push --quiet origin "refs/heads/$BRANCH"
    git push --quiet origin "refs/heads/$BRANCH"
fi
[ -z "${GITHUB_OUTPUT:-}" ] || echo "branch=$BRANCH" >>"$GITHUB_OUTPUT"
