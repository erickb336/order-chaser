#!/bin/bash
# Builds OCSpike.app (Release, hardened runtime) signed by your Personal Team, then checks the signature.
# The OWNER runs it. Usage: spike/build.sh   (optional env: OC_TEAM_ID, OC_BUNDLE_ID)
set -euo pipefail
cd "$(dirname "$0")"
export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
OUT="$PWD/build"
APP="$OUT/Build/Products/Release/OCSpike.app"
# A bundle id must be unique across all Apple teams; this one is unique to your Mac user.
BUNDLE_ID="${OC_BUNDLE_ID:-io.github.$(id -un | tr -cd 'A-Za-z0-9-').ocspike}"

fail() { echo "FAIL: $*" >&2; exit 1; }

# 1. The team: env var, else the OU of an "Apple Development" certificate, else Xcode's signed-in team.
TEAM="${OC_TEAM_ID:-}"
if [[ -z "$TEAM" ]]; then
  TEAM=$(security find-certificate -c "Apple Development" -p 2>/dev/null \
    | openssl x509 -noout -subject 2>/dev/null | sed -n 's/.*OU *= *\([A-Z0-9]\{10\}\).*/\1/p' | head -1) || true
fi
if [[ -z "$TEAM" ]]; then
  TEAM=$(defaults read com.apple.dt.Xcode IDEProvisioningTeamByIdentifier 2>/dev/null \
    | sed -n 's/.*teamID = \([A-Z0-9]\{10\}\);.*/\1/p' | head -1) || true
fi
[[ -n "$TEAM" ]] || fail "no team found. Sign in to Xcode (Settings > Accounts), or set OC_TEAM_ID=<your 10-character team id>."
echo "team: $TEAM   bundle id: $BUNDLE_ID"

# 2. Build. -allowProvisioningUpdates lets Xcode create the certificate and the profile for the Personal Team.
xcodebuild -project OCSpike.xcodeproj -scheme OCSpike -configuration Release \
  -derivedDataPath "$OUT" -allowProvisioningUpdates \
  DEVELOPMENT_TEAM="$TEAM" PRODUCT_BUNDLE_IDENTIFIER="$BUNDLE_ID" build -quiet \
  || fail "xcodebuild failed (see above)"

# 3. Checks.
codesign --verify --strict --verbose=1 "$APP" || fail "codesign --verify --strict"
ENTS=$(codesign -d --entitlements - --xml "$APP" 2>/dev/null)
grep -q "<string>$TEAM.$BUNDLE_ID</string>" <<<"$ENTS" || fail "access group $TEAM.$BUNDLE_ID is not in the entitlements"
if grep -q "get-task-allow" <<<"$ENTS"; then fail "get-task-allow is in the entitlements"; fi
[[ -f "$APP/Contents/embedded.provisionprofile" ]] || fail "embedded.provisionprofile is missing"
codesign -dvv "$APP" 2>&1 | grep -q "flags=.*runtime" || fail "hardened runtime is off"

echo "identity: $(codesign -dvv "$APP" 2>&1 | sed -n 's/^Authority=//p' | head -1)"
echo "access group: $TEAM.$BUNDLE_ID   get-task-allow: absent   hardened runtime: on"
echo "OK: $APP"
