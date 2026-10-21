#!/usr/bin/env bash
# Push the built firmware to a running PixoTicker over OTA (no serial needed).
# Usage: tools/flash.sh [host] [bin]
set -euo pipefail

HOST="${1:-192.168.4.1}"
BIN="${2:-.pio/build/esp32dev/firmware.bin}"

[ -f "$BIN" ] || { echo "no image: $BIN"; exit 1; }
SIZE=$(stat -f%z "$BIN")
echo "image: $BIN ($SIZE bytes)"

echo "before:"
curl -sS -m 5 "http://$HOST/state" || echo "(no /state, vendor firmware?)"

echo "uploading..."
# /u reads server->upload(): multipart only. Expect: off, the ESP32 server
# answers before the body otherwise. Retry once, the parser is occasionally flaky.
upload() {
  curl -sS -m 180 -H 'Expect:' -F "update=@$BIN;type=application/octet-stream" "http://$HOST/u"
}
RESP=$(upload || true)
case "$RESP" in
  *"update ok"*) ;;
  *) echo "first attempt: $RESP"; sleep 1; RESP=$(upload || true);;
esac
case "$RESP" in
  *"update ok"*) echo "upload accepted" ;;
  *) echo "upload rejected: $RESP"; exit 1;;
esac

echo "waiting for reboot..."
sleep 12
for i in 1 2 3 4 5 6; do
  if curl -sS -m 4 "http://$HOST/state"; then echo; exit 0; fi
  sleep 5
done
echo "device did not come back on $HOST"
exit 1
