# Pixoticker

Firmware and a small relay server for a 128×64 HUB75 LED panel driven by an
ESP32 (no PSRAM, 4 MB flash). The panel draws whatever frames a remote relay
hands it, over a single outbound TLS connection, so it needs no inbound ports,
no port forwarding and no laptop left running on the same network.

The display is a 128×64 panel built from two 64×32 modules.

```
renderer ──POST /frame──▶ relay ──TLS stream──▶ ESP32 ──HUB75──▶ panel
   ▲                       │
   └────── /op, /device ───┘   (brightness, OTA, telemetry)
```

## What is here

```
src/                firmware (Arduino, ESP32)
  main.cpp          boot, HTTP API, relay pull loop, OTA, WiFiManager
  panel.cpp/.h      HUB75 setup, drawing, panel configuration
  relay_ca.h        ISRG Root X1, pinned so the relay chain verifies
  secrets.h.example  copy to secrets.h: relay host, port, key
server/
  pxtrelay.py       relay: frame store, stream, ops, firmware hosting
tools/
  onboard.sh        build, flash over the air, verify frames arrive
  flash.sh          POST a firmware image to a panel
  render.py         draw a chart or text and push it as a frame
  pull.py           long-poll the relay and repaint a local network panel
  pixels_demo.py    colour and geometry checks
  mirror.js         mirror a browser canvas of any website onto the panel
  serial_cmd.py     serial helpers
```

## Quick start

1. Flash the firmware over USB the first time:

   ```sh
   cp src/secrets.h.example src/secrets.h   # fill in relay host and key
   pio run -e esp32dev -t upload
   ```

2. The panel brings up a Wi-Fi access point named `PixoTicker`. Join it, open
   `http://192.168.4.1/`, and give it your network. It reconnects itself from
   then on and keeps the access point up for recovery.

3. Run the relay on any host with a public HTTPS name:

   ```sh
   echo "a-long-random-string" > ~/.pxt_relay_key
   python3 server/pxtrelay.py --port 18100        # binds 127.0.0.1
   ```

   Put it behind nginx with a Let's Encrypt certificate:

   ```nginx
   location /pxt/ {
       proxy_pass http://127.0.0.1:18100/;
       proxy_http_version 1.1;
       proxy_set_header Connection "";
       proxy_buffering off;
       proxy_read_timeout 3600s;
   }
   ```

4. Push a frame:

   ```sh
   tools/render.py 192.168.4.1 --watch 60
   ```

## Configuration

The relay host, port and key are compile-time settings in `src/secrets.h`
(gitignored) or build flags:

```
-DPXT_RELAY_HOST=\"relay.example.com\" -DPXT_RELAY_PORT=443 -DPXT_RELAY_KEY=\"...\"
```

With no host the firmware still runs: it serves its access point and its HTTP
API, and simply never dials out.

The relay reads its key from `--key-file` (default `~/.pxt_relay_key`), binds
loopback by default so nginx terminates TLS, and serves nothing to a client
without the key.

## Panel API

Served on the panel's own address (`192.168.4.1` on its access point):

| Route | Method | Purpose |
|---|---|---|
| `/state` | GET | heap, pull counters, TLS mode, panel config |
| `/frame` | GET/POST | read the current frame as a PNG, or post raw RGB565/RGB888 |
| `/pixel`, `/pixels` | POST | set one or many pixels |
| `/brightness` | POST | 0–255 |
| `/panel` | POST | module layout: width, height, module size, serpentine |
| `/panel/try` | POST | apply a layout for a few seconds, then revert |
| `/test` | GET | colour bars |
| `/u` | POST | firmware upload |
| `/update` | GET | upload page |
| `/safe` | POST | boot into safe mode (no panel init) |
| `/reboot` | GET | restart |

## Relay protocol

One long-lived chunked stream carries everything. Each chunk starts with a
four-byte magic, a big-endian sequence number, then the payload:

```
PXTF + seq + 16384 bytes     raw frame, 128×64 RGB565
PXTR + seq + runs            the same frame, run encoded: hi, lo, count
PXTO + n + json              one op: brightness, ota, panel config, reboot
```

`PXTR` is only used when the client asks for it (`/stream?enc=rle`) and the
encoded form is smaller. On a typical chart it is 2.5 KB against 16 KB, which
matters because a panel's receive window and the round trip cap throughput far
below its Wi-Fi link speed.

Relay routes: `POST /frame`, `GET /stream?since=N&enc=rle`, `GET /latest`,
`POST /device`, `POST /op`, `POST /fw`, `GET /health`.

Frames are written straight into the display buffer as they arrive, so a
truncated frame never reaches the panel and a new one is only shown once it is
complete. `panel_show` copies into the DMA buffer and flips, so updates are
tear free.

## Recovery

The panel never trusts the image it just booted blindly. On first boot after an
update it waits for one complete frame; if none arrives it resets into the
previous image. A boot counter plus a one-shot timer catches a panel
configuration that hangs boot, and `/safe` (or five reboots in a row) boots
with the panel disabled so the HTTP API stays reachable.

**Dump your own stock firmware before replacing it.** The vendor image is not
included here and must not be redistributed; a full 4 MB read over the serial
bootloader is the only reliable way back.

## Known limits

- One TLS session at a time while starting up: the ESP32 needs a contiguous
  block for the RSA handshake, and two sessions do not fit.
- The panel reports its state in an `X-Pxt-State` header on the stream request,
  so telemetry costs no second TLS session. A relay that never sees the panel
  reconnect will show a stale `device_age`.
- The relay keeps no history: a client that reconnects gets the newest frame.

`src/relay_ca.h` pins ISRG Root X1, the Let's Encrypt anchor. A relay on any
other certificate needs its own root pasted there, or the panel falls back to
unverified TLS after five failures.

## License

MIT.
