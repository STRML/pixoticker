#!/usr/bin/env bash
# One command for the visit to the panel: join the "PixoTicker" Wi-Fi, run this.
# It builds with the relay settings, flashes over the air, then proves the panel
# is pulling frames from the relay on its own.
#
#   PXT_RELAY_HOST=relay.example.com PXT_RELAY_KEY=... tools/onboard.sh
#   tools/onboard.sh --no-build          # reuse the last build
#   tools/onboard.sh 192.168.4.1         # panel address, if not the default
#
# Settings come from the environment, or from src/secrets.h when you have
# already written one. Needs no internet: the panel sits on its own Wi-Fi while
# its station link carries the frames.
set -euo pipefail

cd "$(dirname "$0")/.."

NO_BUILD=0
HOST=""
for arg in "$@"; do
  case "$arg" in
    --no-build) NO_BUILD=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) HOST="$arg" ;;
  esac
done

BIN=".pio/build/esp32dev/firmware.bin"

fail() { echo "FAIL: $*" >&2; exit 1; }

HOSTNAME_="${PXT_RELAY_HOST:-}"
KEYVAL="${PXT_RELAY_KEY:-}"
# Fall back to src/secrets.h so the header comment holds.
secret() { sed -n "s/^#define $1 \"\(.*\)\".*/\1/p" src/secrets.h 2>/dev/null | head -1; }
if [ -f src/secrets.h ]; then
  [ -n "$HOSTNAME_" ] || HOSTNAME_="$(secret PXT_RELAY_HOST)"
  [ -n "$KEYVAL" ] || KEYVAL="$(secret PXT_RELAY_KEY)"
fi
if [ -z "$KEYVAL" ] && [ -n "${PXT_KEY_FILE:-}" ]; then
  [ -f "$PXT_KEY_FILE" ] || fail "missing $PXT_KEY_FILE"
  KEYVAL="$(cat "$PXT_KEY_FILE")"
fi
[ -n "$HOSTNAME_" ] || fail "set PXT_RELAY_HOST (or write src/secrets.h)"
[ -n "$KEYVAL" ] || fail "set PXT_RELAY_KEY or PXT_KEY_FILE"

if [ "$NO_BUILD" = 0 ]; then
  echo "== build"
  PLATFORMIO_BUILD_FLAGS="-DPXT_RELAY_HOST=\\\"${HOSTNAME_}\\\" -DPXT_RELAY_KEY=\\\"${KEYVAL}\\\"" \
    pio run -e esp32dev >/dev/null || fail "build failed (run pio run yourself to see why)"
fi
[ -f "$BIN" ] || fail "missing $BIN"
grep -a -q -F -- "$KEYVAL" "$BIN" || fail "relay key is not embedded in $BIN"
echo "image ok: $(wc -c < "$BIN") bytes"

if [ -z "$HOST" ]; then
  echo "== find panel"
  if curl -sS -m 3 "http://192.168.4.1/state" 2>/dev/null | grep -q '"fw":"pxt'; then
    HOST="192.168.4.1"
  fi
fi
[ -n "$HOST" ] || fail "no panel at 192.168.4.1: join the PixoTicker Wi-Fi, or pass its address"

echo "== panel at $HOST"
curl -sS -m 5 "http://$HOST/state" || fail "no /state from $HOST"
echo

echo "== flash"
PXT_RELAY_KEY="$KEYVAL" tools/flash.sh "$HOST" "$BIN"

echo "== verify (up to 2 min)"
frames=0
for i in $(seq 1 24); do
  state="$(curl -sS -m 4 "http://$HOST/state" 2>/dev/null || true)"
  [ -n "$state" ] || { sleep 5; continue; }
  frames="$(printf '%s' "$state" | sed -n 's/.*"pull_frames":\([0-9]*\).*/\1/p')"
  errs="$(printf '%s' "$state" | sed -n 's/.*"pull_errors":\([0-9]*\).*/\1/p')"
  code="$(printf '%s' "$state" | sed -n 's/.*"pull_code":\([-0-9]*\).*/\1/p')"
  phase="$(printf '%s' "$state" | sed -n 's/.*"pull_phase":"\([a-z]*\)".*/\1/p')"
  mode="$(printf '%s' "$state" | sed -n 's/.*"tls_mode":"\([a-z]*\)".*/\1/p')"
  err="$(printf '%s' "$state" | sed -n 's/.*"tls_err":"\([^"]*\)".*/\1/p')"
  echo "  ${i}: frames=${frames:-?} errors=${errs:-?} http=${code:-?} phase=${phase:-?} tls=${mode:-?} ${err}"
  if [ -n "$frames" ] && [ "$frames" -gt 0 ] 2>/dev/null; then
    echo
    echo "PASS: panel is pulling frames from the relay (${frames} so far)"
    if [ "$mode" = "insecure" ]; then
      echo "WARN: chain verification failed (${err:-no error text}); running unverified TLS."
    fi
    echo "      disconnect from PixoTicker; nothing else is required."
    exit 0
  fi
  sleep 5
done

echo
echo "FAIL: flashed, but no frames after 2 minutes. Read the state above:"
echo "  errors rising with http=-1  -> cannot connect or TLS refused (clock/cert)"
echo "  http=200 and frames=0       -> connected, relay sent nothing (renderer down?)"
echo "  no /state at all            -> panel rebooted into safe mode; retry the flash"
exit 1
