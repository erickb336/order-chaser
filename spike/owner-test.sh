#!/bin/bash
# R10 spike test: the OWNER runs it after build.sh. It pauses before each Touch ID step.
# It stores a random SAMPLE (never a real key), tries to read it in several ways, then deletes it.
# Usage: spike/owner-test.sh [--dry-run]    (--dry-run prints the steps and touches nothing)
set -uo pipefail
cd "$(dirname "$0")"
DRY=0; [[ "${1:-}" == "--dry-run" ]] && DRY=1

SERVICE="order-chaser.spike"
ORDER="SPIKE: buy 0.0001 BTC/USD at 60000.0, chase OC-SPIKE-1"
APP="$PWD/build/Build/Products/Release/OCSpike.app"
BIN="$APP/Contents/MacOS/oc-spike"
LOG="$HOME/order-chaser-spike-result.txt"
SAMPLE_BYTES=64   # 32 random bytes, hex-encoded
PASSN=0; FAILN=0; STEP=""

log()   { echo "$*"; if ((!DRY)); then echo "$*" >> "$LOG"; fi; }
step()  { STEP="$1"; log ""; log "== Step $1: $2"; }
pause() { if ((DRY)); then echo "  owner: $1"; else read -r -p "  $1  Press Return when ready. " _ </dev/tty; fi; }
ask()   { local a; read -r -p "  $1 [y/n] " a </dev/tty; [[ "$a" == [yY]* ]]; }
check() { # check "<expected result>" <command that succeeds when the result is right>
  local desc="$1"; shift
  if ((DRY)); then echo "  expect: $desc"; return; fi
  if "$@"; then log "  PASS  $STEP  $desc"; PASSN=$((PASSN + 1))
  else log "  FAIL  $STEP  $desc"; FAILN=$((FAILN + 1)); fi
}
skip()  { log "  SKIP  $STEP  $1"; }
# run <shown text> <command...>: sets OUT and RC. Only oc-spike output goes to the log (it never holds a value).
run()   { echo "  \$ $1"; shift; if ((DRY)); then OUT=""; RC=0; return; fi; OUT=$("$@" 2>&1); RC=$?; }
spike() { run "$1" "${@:2}"; if ((!DRY)); then log "  > ${OUT:-(no output)} (exit $RC)"; fi; }
has()   { [[ "$OUT" == *"$1"* ]]; }
no_value() { [[ -z "$SAMPLE" || "$OUT" != *"$SAMPLE"* ]]; }
store_sample() { printf '%s' "$SAMPLE" | "$BIN" store "$SERVICE"; }

if ((DRY)); then
  echo "DRY RUN: no store, read or delete, no codesign, no log file. SAMPLE is not generated."
  SAMPLE=""
else
  [[ -x "$BIN" ]] || { echo "Run spike/build.sh first: $BIN is missing." >&2; exit 1; }
  : > "$LOG"
  SAMPLE=$(openssl rand -hex 32)
fi
WORK=$(mktemp -d "${TMPDIR:-/tmp}/oc-spike.XXXXXX")
trap 'rm -rf "$WORK"; unset SAMPLE' EXIT

log "order-chaser R10 spike result, $(date '+%Y-%m-%d %H:%M:%S')"
log "macOS $(sw_vers -productVersion), $(uname -m). This file holds no secret."

step 0 "the helper's signature"
spike "oc-spike whoami" "$BIN" whoami
check "signed by a team (not ad-hoc)" eval '! has "team: (none"'
check "has a keychain-access-groups entitlement" has "keychain-access-groups"
check "no get-task-allow" eval '! has "get-task-allow"'
if ((!DRY)); then log "  identity: $(codesign -dvv "$APP" 2>&1 | sed -n 's/^Authority=//p' | head -1)"; fi

step 1 "remove an item from an earlier run (no result)"
spike "oc-spike delete $SERVICE" "$BIN" delete "$SERVICE"

step 2 "store a random SAMPLE (32 bytes, hex) under service $SERVICE"
spike "printf <SAMPLE> | oc-spike store $SERVICE" store_sample
check "store: status=0" has "store: status=0"

step 3 "/usr/bin/security cannot read the item"
run "security find-generic-password -s $SERVICE -w" security find-generic-password -s "$SERVICE" -w
if ((!DRY)); then log "  > exit $RC (output not logged)"; fi
check "security exits with an error and gives no value" eval '((RC != 0)) && no_value'

step 4 "Python keyring cannot read the item"
if ((!DRY)) && ! python3 -c "import keyring" 2>/dev/null; then
  skip "python3 keyring is not installed"
else
  run "python3 -c \"import keyring; print(keyring.get_password('$SERVICE','spike'))\"" \
    env -u PYTHON_KEYRING_BACKEND python3 -c \
    "import keyring; print(keyring.get_password('$SERVICE','spike'))"
  if ((!DRY)); then
    log "  > exit $RC, backend: $(env -u PYTHON_KEYRING_BACKEND python3 -c 'import keyring; print(keyring.get_keyring())' 2>&1)"
  fi
  check "keyring gives nothing (None or an error)" eval 'no_value && { [[ "$OUT" == None ]] || ((RC != 0)); }'
fi

step 5 "read with Touch ID"
pause "A Touch ID sheet will open. Read its text, then touch the sensor."
spike "oc-spike read $SERVICE \"$ORDER\"" "$BIN" read "$SERVICE" "$ORDER"
check "read: status=0 bytes=$SAMPLE_BYTES" has "read: status=0 bytes=$SAMPLE_BYTES"
if ((DRY)); then echo "  owner: answer y/n: the sheet showed \"$ORDER\""
else check "owner: the sheet showed the order text" ask "Did the sheet show \"$ORDER\"?"; fi

step 6 "read again: a second touch is needed"
pause "A Touch ID sheet will open again. Touch the sensor."
spike "oc-spike read $SERVICE \"$ORDER\"" "$BIN" read "$SERVICE" "$ORDER"
check "read: status=0 bytes=$SAMPLE_BYTES" has "read: status=0 bytes=$SAMPLE_BYTES"
if ((DRY)); then echo "  owner: answer y/n: macOS asked for a new touch"
else check "owner: macOS asked for a new touch" ask "Did macOS ask you to touch the sensor again?"; fi

step 7 "read and press Cancel"
pause "A Touch ID sheet will open. Press Cancel. Do not touch the sensor."
spike "oc-spike read $SERVICE \"$ORDER\"" "$BIN" read "$SERVICE" "$ORDER"
check "read: status=-128 (user cancelled)" has "read: status=-128 bytes=0"

step 8 "an ad-hoc copy with the same entitlements is refused"
run "cp -R OCSpike.app <tmp>/adhoc/ and copy its entitlements" eval \
  'mkdir -p "$WORK/adhoc" && cp -R "$APP" "$WORK/adhoc/" && codesign -d --entitlements - --xml "$APP" > "$WORK/ents.plist" 2>/dev/null'
run "codesign --force --sign - --options runtime --entitlements <ents> <tmp>/adhoc/OCSpike.app" \
  codesign --force --sign - --options runtime --entitlements "$WORK/ents.plist" "$WORK/adhoc/OCSpike.app"
spike "<tmp>/adhoc/OCSpike.app/Contents/MacOS/oc-spike whoami" "$WORK/adhoc/OCSpike.app/Contents/MacOS/oc-spike" whoami
check "macOS kills it at launch (exit 137)" eval '((RC == 137))'

step 9 "a copy re-signed without the entitlement cannot read"
IDENTITY=""
if ((!DRY)); then IDENTITY=$(codesign -dvv "$APP" 2>&1 | sed -n 's/^Authority=//p' | head -1); fi
pause "codesign will use your signing key. macOS can ask for your login password: allow it once."
run "cp -R OCSpike.app <tmp>/noent/" eval 'mkdir -p "$WORK/noent" && cp -R "$APP" "$WORK/noent/"'
run "codesign --force --sign \"<your Apple Development identity>\" --options runtime <tmp>/noent/OCSpike.app" \
  codesign --force --sign "$IDENTITY" --options runtime "$WORK/noent/OCSpike.app"
check "codesign re-signed the copy" eval '((RC == 0))'
if ((DRY || RC == 0)); then
  pause "If a Touch ID sheet opens now, press Cancel (that is a FAIL)."
  spike "<tmp>/noent/OCSpike.app/Contents/MacOS/oc-spike read $SERVICE \"$ORDER\"" \
    "$WORK/noent/OCSpike.app/Contents/MacOS/oc-spike" read "$SERVICE" "$ORDER"
  check "read: status=-34018 (missing entitlement)" has "read: status=-34018"
fi

step 10 "delete the item"
spike "oc-spike delete $SERVICE" "$BIN" delete "$SERVICE"
check "delete: status=0" has "delete: status=0"
spike "oc-spike delete $SERVICE (again)" "$BIN" delete "$SERVICE"
check "delete again: status=-25300 (the item is gone)" has "delete: status=-25300"

if ((DRY)); then echo ""; echo "DRY RUN done: 11 steps (0 to 10) printed."; exit 0; fi

if grep -qF "$SAMPLE" "$LOG"; then
  sed -i '' "s/$SAMPLE/<removed>/g" "$LOG"
  log "  FAIL  the SAMPLE appeared in this log and was removed"; FAILN=$((FAILN + 1))
fi
log ""
log "RESULT: $([[ $FAILN == 0 ]] && echo PASS || echo FAIL)  ($PASSN passed, $FAILN failed)"
echo ""
echo "Saved: $LOG  (send this file; it holds no secret)"
