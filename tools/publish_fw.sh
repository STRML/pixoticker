#!/usr/bin/env bash
# Publish firmware to the relay and have the panel install it.
#
#   tools/publish_fw.sh
#
# Always rebuilds: a stale .pio/build binary may carry a placeholder relay key,
# and shipping that would leave the panel unable to connect (and rolling back).
#
# The panel pulls /pxt/fw over its existing outbound link and installs only if
# the size and SHA-256 in the op match what it downloaded.
set -euo pipefail

SSH_HOST="${PXT_RELAY_SSH:-}"
URL="${PXT_RELAY_URL:-}"
KEYVAL="${PXT_RELAY_KEY:-}"
RELAY_HOST="${PXT_RELAY_HOST:-}"
SETTLE="${PXT_SETTLE:-60}"  # seconds the new build must hold before success
BIN=".pio/build/esp32dev/firmware.bin"
cd "$(dirname "$0")/.."

[ -n "$URL" ] || { echo "set PXT_RELAY_URL, for example https://relay.example.com/pxt"; exit 1; }
[ -n "$SSH_HOST" ] || { echo "set PXT_RELAY_SSH to the host that serves the relay"; exit 1; }
[ -n "$KEYVAL" ] || { echo "set PXT_RELAY_KEY"; exit 1; }
[ -n "$RELAY_HOST" ] || { echo "set PXT_RELAY_HOST: an image built without it never dials out"; exit 1; }

PLATFORMIO_BUILD_FLAGS="-DPXT_RELAY_HOST=\\\"${RELAY_HOST}\\\" -DPXT_RELAY_KEY=\\\"${KEYVAL}\\\"" pio run -e esp32dev >/dev/null

[ "${#KEYVAL}" -ge 16 ] || { echo "key too short"; exit 1; }
grep -a -q -F -- "$KEYVAL" "$BIN" || { echo "refusing: relay key is not embedded in $BIN"; exit 1; }

SIZE=$(wc -c < "$BIN" | tr -d " ")
SHA=$(shasum -a 256 "$BIN" | awk '{print $1}')

ssh "$SSH_HOST" 'mkdir -p ~/firmware'
scp -q "$BIN" "$SSH_HOST:firmware/pxt.bin"

served=$(curl -sSf -m 30 -D - -o /dev/null -H "X-Api-Key: ${KEYVAL}" \
  "$URL/fw" | tr -d '\r' | awk 'tolower($1)=="x-firmware-sha256:"{print $2}')
[ "$served" = "$SHA" ] || { echo "relay serves $served, expected $SHA"; exit 1; }

device_json() {
  # A DNS blip must not abort the publish loop.
  curl -sSf -m 10 -H "X-Api-Key: ${KEYVAL}" "$URL/device" 2>/dev/null || true
}

device_field() {
  device_json | python3 -c "import json,sys
try: print(json.load(sys.stdin).get('$1',''))
except Exception: print('')"
}

before_build="$(device_field build)"
before_up="$(device_field uptime)"

queue_op() {
  if ! curl -sSf -m 20 -H "X-Api-Key: ${KEYVAL}" -H 'Content-Type: application/json' \
    -d "{\"type\":\"ota\",\"path\":\"/pxt/fw\",\"size\":${SIZE},\"sha256\":\"${SHA}\"}" \
    "$URL/op" >/dev/null; then
    echo "relay rejected the op" >&2
    return 1
  fi
}

# _sent > 0 only says the relay wrote a frame to the new build. A build that
# crashes drawing it resets, and the bootloader puts the old image back, so the
# new build has to hold for SETTLE seconds before this counts as a success.
settle() {  # build, uptime and _seen when the new build first got a frame
  sleep "$SETTLE"
  local b u s
  b="$(device_field build)"
  u="$(device_field uptime)"
  s="$(device_field _seen)"
  if [ -z "$b" ] || [ -z "$u" ] || [ -z "$s" ]; then
    echo "could not read /device after ${SETTLE}s; check the panel before trusting ${1}"
    return 2
  fi
  if [ "$b" != "$1" ]; then
    echo "panel went back to ${b}: ${1} did not survive its first frames"
    return 3
  fi
  # A reconnect without a restart reports uptime grown by the time between the
  # two connects; a restart reports it near zero.
  if [ $((u + 5)) -lt $(($2 + s - $3)) ]; then
    echo "panel restarted during the ${SETTLE}s check (uptime ${u}s): ${1} may be crash-looping"
    return 3
  fi
  echo "panel running ${b} (uptime ${u}s, held ${SETTLE}s)"
}

echo "published ${SIZE} bytes sha256 ${SHA} (device build before: ${before_build:-unknown})"
queue_op

# The op rides the live stream, so a panel that reconnects at that moment misses
# it. A restart shows up as a new build or as uptime going backwards; stop then.
for _ in $(seq 1 18); do
  sleep 10
  now_build="$(device_field build)"
  now_up="$(device_field uptime)"
  restarted=0
  if [ -n "$now_build" ] && [ "$now_build" != "$before_build" ]; then restarted=1; fi
  if [ -n "$now_up" ] && [ -n "$before_up" ] && [ "$now_up" -lt "$before_up" ]; then restarted=1; fi
  if [ "$restarted" = 1 ]; then
    # A new build that never draws rolls back on the next reset: only call this
    # a success once the relay has streamed it a frame. The panel's own frame
    # count only refreshes when it reconnects, so it reads zero here.
    frames="$(device_field _sent)"
    if [ -n "$frames" ] && [ "$frames" -gt 0 ]; then
      echo "panel restarted into ${now_build} and was sent a frame; watching it for ${SETTLE}s"
      settle "$now_build" "$now_up" "$(device_field _seen)" && exit 0 || exit $?
    fi
    echo "panel restarted but reports no frames yet; waiting"
  fi
  queue_op
done

echo "panel did not report a new build; check /pxt/device and the serial log"
exit 2
