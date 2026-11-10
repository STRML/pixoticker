# Pixoticker

Firmware and a small relay server for a 128×64 HUB75 LED panel driven by an
ESP32 (no PSRAM, 4 MB flash). The panel draws whatever frames a remote relay
hands it, over a single outbound TLS connection, so it needs no inbound ports,
no port forwarding and no laptop left running on the same network.

The display is a 128×64 panel built from two 64×64 modules side by side
(1/32 scan, FM6126A drivers). The module layout is a runtime setting, so other
HUB75 layouts of up to four modules work too.

```
renderer ──POST /frame──▶ relay ──TLS stream──▶ ESP32 ──HUB75──▶ panel
   ▲                       │
   └────── /op, /device ───┘   (brightness, OTA, telemetry)
```

## What is here

```
src/                firmware (Arduino, ESP32)
  main.cpp          boot, HTTP API, relay pull loop, OTA, Wi-Fi setup page
  panel.cpp/.h      HUB75 setup, drawing, panel configuration
  relay_ca.h        ISRG Root X1, pinned so the relay chain verifies
  secrets.h.example  copy to secrets.h: relay host, port, key
server/
  pxtrelay.py       relay: frame store, stream, ops, firmware hosting
  test_relay.py     loopback checks for the relay (python3 server/test_relay.py)
tools/
  onboard.sh        build, flash over the air, verify frames arrive
  publish_fw.sh     ship a firmware image to the panel through the relay
  flash.sh          POST a firmware image to a panel
  flash_serial.py   serial flash and full flash dump, for recovery
  mon.py            capture the boot log over serial
  render.py         draw a chart or text and push it as a frame
  pull.py           long-poll the relay and repaint a local network panel
  pixels_demo.py    colour and geometry checks
  mirror.js         mirror a browser canvas of any website onto the panel
  serial_cmd.py     serial helpers
  orderbook.js      example: live Deribit BTC-PERPETUAL depth (bun)
  ccusage.py        example: Claude Code spend from ccusage, daily bars
  screen.py         layout helper: chart band over five lines of text
  font46.py         4x6 pixel font (X11 Misc-Fixed, public domain)
  bdf2font.py       turn a BDF font into a table like font46.py
  test_publish_fw.py  loopback checks for publish_fw.sh (no hardware needed)
board/              cluster status board: node agents post GPU, memory and token rates
  agent.py          per-Mac agent (stdlib, Python 3.9): ioreg GPU %, vm_stat memory %, mlx-serve rates
  board.py          ingest, layout rotation, LED-style preview gallery, pushes frames to the relay
  layouts.py        five 128x64 layouts for four nodes: rows cols quad spark focus
  preview.py        contact sheet of every layout against fixture clusters
  Dockerfile        one image for the relay and the board
  test_board.py     loopback checks (python3 board/test_board.py)
```

The examples push straight to the panel and send `PXT_RELAY_KEY` as
`X-Api-Key`; `ccusage.py --relay URL` goes through the relay instead.

```sh
export PXT_RELAY_KEY=...
bun tools/orderbook.js 192.168.4.1          # 8 fps by default
tools/ccusage.py 192.168.4.1 --watch 300    # needs ccusage on PATH
tools/screen.py --out preview.png           # needs pillow
```

## Cluster board

`board/` shows up to four Macs running [mlx-serve](https://github.com/ddalcu/mlx-serve): GPU %,
memory %, and decode and prefill tokens per second.

```
agent.py (each Mac) ──POST /ingest──▶ board.py ──POST /frame──▶ relay ──▶ panel
                                         └── GET /  every layout, live, as LED dots
```

Each agent reads the local `/metrics.json` once a second and posts one small JSON document with
`X-Api-Key`. Live rates are the counter growth over 8 s, because the server's counters move in lumps
(a prefill chunk, a round of drafted tokens). A busy node shows its live rate; an idle one shows the
last request's average, dimmed, until it is 30 s old (`LAST_S`), then `0`; a busy node whose prefill or decode has stopped shows a dimmed `0` too, in the metric's own colour (grey at the night-dimming level is nearly black on the panel). `--` means no data. The agent reports each average with its age (`dec_last_age`, `pre_last_age`), so the clocks of the Studios and the board need not agree, and an average with no age is not shown (an agent older than this change shows `0` there until it is updated). A node that stops reporting for 10 s turns grey with `STALE`, so a dead
agent never leaves old numbers on the panel. A node that never reported shows `--`.

**Night dimming.** Set `--dim "20:00-07:30@0.25"` (`BOARD_DIM`, with `--tz` or `BOARD_TZ`) and the board dims every
frame it sends inside that window. The board does this because firmware brightness ops are not replayed after a
reconnect. Each pixel scales by its brightest channel, so its hue survives. Plain scaling takes a grey label from
95 to 24, which the LEDs barely show, so a content pixel (brightest channel 50 or more) never goes below a floor of
40; `@0.25:55` sets another floor. Above the floor, brightness still climbs at half the dim level (at least 0.025
a level, so two colours 40 apart stay a whole step apart at any setting), so a live colour stays brighter than its
idle variant. A pixel never gets brighter than it was, and anything under 50 (bar tracks,
rules) fades as usual.

**Range bars.** The focus view draws a bar under the decode and prefill numbers: the live rate as a fraction of
that node's usual peak, so 40 tok/s against a 200 tok/s peak reads as 20%. The board learns the peak by watching
(`board/peaks.py`): the highest value that held for three samples in a row (a one-sample spike cannot set the
scale), decaying with a three-day half-life so it follows a node that got slower. It is kept per node and per
number of nodes sharing the job, because four Studios in tensor parallel decode at a different rate from one
alone, and per metric. Until 60 such samples have been seen the bars use a default scale (150 tok/s decode,
3000 tok/s prefill). `--state-file` (env `BOARD_STATE_FILE`) saves the peaks every minute, so a restart keeps them.

**Machine stats.** The agent also reports the machine's GPU temperature, power and CPU load, and the layouts draw them
where there is room. Every number on the panel is a letter and a figure in its metric's colour: `G93` GPU, `M82`
memory, `D118` decode, `P2.4K` prefill, and for the machine `T58` temperature, `W209` watts, `C12` CPU percent.

| Figure | Source (no sudo) | Shown |
|---|---|---|
| `temp`, GPU degrees C | `~/.local/bin/latch sensors` every 10 s (about 0.4 s of one core; a failed read keeps the last figure for a minute) | `rows` in the gap after memory, `quad` and `focus` in the stats row, as `T58`. White below 70, amber from 70, red from 85 |
| `watts`, whole machine | the SMC key `PD0R` through IOKit and `ctypes`, every second (`PSTR` reads 0 on a Studio) | `rows` right of the bars, `spark` in the column after the history, `quad` and `focus` in the stats row, as `W209`. Capped at `W999` for width |
| `cpu`, busy percent | the kernel's `host_statistics` tick counters between two reads | with the watts, as `C12`. Temperature, watts and CPU each sit right-aligned in a fixed four-character slot, so a figure gaining a digit moves nothing beside it |

To make room the `rows` and `focus` GPU and memory bars are shorter, the `spark` history is 36 samples (it was 62),
`quad` no longer prints `TOK/S` (a small `D` follows the big decode figure and `P` labels prefill beside it), and
`focus` keeps its own `G` and `M` beside its tag, with the cluster totals on the right. `cols` has no room. A figure outside its
range (temperature 10 to 130, watts above 1000, CPU outside 0 to 100) is dropped, a node that is not reporting shows
none, and without Latch or readable SMC the agent just reports less.

**Tensor parallel.** Only rank 0 runs an HTTP server, so only it has token counters; the other ranks run
`mlx-serve worker ... --coordinator 10.254.<ab>.<n>:<port>` and would show idle while their GPUs work. An agent
that finds such a process (and has no server of its own) reports `worker: true` and `follows: studio<n>` (the last octet of rank 0's cable
address is rank 0's Studio number). The board then shows rank 0's live rates, last averages and state on each
worker, since every rank decodes every token, and leaves the workers out of the `ALL` totals so a token counts
once. A worker whose coordinator is off the mesh follows the busiest node that is not a worker. A worker never
follows a worker, and one whose leader has gone stale shows its own numbers.

```sh
python3 board/preview.py --out sheet.png                       # every layout x busy, prefill, degraded
python3 board/board.py --port 18101 --ingest-key-file ~/.pxt_ingest_key   # gallery on :18101
python3 board/agent.py --name studio1 --board http://board-host:18101      # on each Mac
curl -H "X-Api-Key: $KEY" -d '{"layout":"rows"}' http://board-host:18101/layout   # or "auto"
```

`BOARD_NODES` names the slots (default `studio1,studio2,studio3,studio4`), `BOARD_RELAY_URL` and
`BOARD_RELAY_KEY_FILE` send frames to the relay, and `BOARD_LAYOUT` pins a layout (default `auto`,
15 s each). The same image runs the relay: `python server/pxtrelay.py --bind 0.0.0.0`.

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

4. Push a frame. Straight to the panel while you are on its access point:

   ```sh
   PXT_RELAY_KEY=... tools/render.py 192.168.4.1 --watch 60
   ```

   Through the relay, from anywhere (a 128x64 RGB565 file is 16384 bytes):

   ```sh
   curl -H "X-Api-Key: $KEY" --data-binary @frame.565 https://relay.example.com/pxt/frame
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

## Shipping firmware

> **Unconfirmed on a panel:** the first version of this download stopped after 64 KB
> ([issue 2](https://github.com/STRML/pixoticker/issues/2)). The panel now resumes a download that
> ends early with an HTTP `Range` request and leaves the reason in `/device` (`ota`, for example
> `closed 65536/1153312`), but no one has run it on hardware yet. Until a run passes, keep a USB
> cable within reach and flash with `tools/flash_serial.py` if it fails.

The panel has no inbound port, so firmware reaches it the same way frames do.
Set four variables and run `tools/publish_fw.sh`:

```sh
export PXT_RELAY_URL=https://relay.example.com/pxt   # relay base URL
export PXT_RELAY_SSH=relay-host                      # ssh name of the relay host
export PXT_RELAY_HOST=relay.example.com              # built into the image
export PXT_RELAY_KEY=...                             # built into the image
export PXT_RELAY_FW=firmware/pxt.bin                 # optional: where the relay reads the image on that host
tools/publish_fw.sh
```

The script:

1. Rebuilds the image with the relay host and key, and refuses an image that
   doesn't contain the key.
2. Copies it to `~/firmware/pxt.bin` on the relay host (`PXT_RELAY_FW` changes the path, and
   the relay's `--firmware` flag must name the same file) and checks the SHA-256 the relay serves
   from `/fw`.
3. Queues `{"type":"ota","path":"/pxt/fw","size":N,"sha256":"..."}`.
4. Re-queues the op every 10 s for up to three minutes, until the panel
   reports a restart (a new build, or uptime going backwards) and the relay has
   streamed the restarted panel a frame.
5. Watches the new build for 60 s (`PXT_SETTLE`). A build that crashes drawing
   its first frames is rolled back by the bootloader, so the script exits 0
   only if the same build is still up and has not restarted. A rollback or a
   restart exits 3, and a `/device` it cannot read exits 2.

The panel closes its stream before it downloads, since a second TLS session
doesn't fit beside the first. It installs only when the size and SHA-256 match,
and it ignores an op for the image it already runs, so a re-queued op never
reinstalls.

## Panel API

Served on the panel's own address (`192.168.4.1` on its access point). Every route
that changes the device needs the relay key in an `X-Api-Key` header (the Wi-Fi
setup form takes it as a field). A build with no key configured leaves the API
open, which suits the bench and nothing else.

| Route | Method | Purpose |
|---|---|---|
| `/state` | GET | heap, pull counters, TLS mode, panel config |
| `/frame` | GET/POST | read the frame as raw RGB565, or post it base64 encoded (RGB565 or RGB888) |
| `/pixel`, `/pixels` | POST | set one or many pixels |
| `/brightness` | POST | 0–255 |
| `/panel` | POST | module layout: `across`, `down`, `serp`, `invclk`, `modh`; saves and reboots |
| `/panel/try` | POST | apply a layout now under the boot watchdog; saved if the panel starts |
| `/boots` | POST | reset the boot counter |
| `/test` | GET | ruler: border, corner markers, 16 px ticks |
| `/u` | POST | firmware upload |
| `/update` | GET | upload page |
| `/safe` | POST | boot into safe mode (no panel init) |
| `/reboot` | POST | restart |

Frames are 128×64 RGB565, big-endian. The panel is wired with green and blue
swapped, so a client that packs RGB565 itself has to swap them too:

```
((r & 0xF8) << 8) | ((b & 0xFC) << 3) | (g >> 3)
```

`/pixel`, `/pixels`, RGB888 frames and the relay's `fmt=888` do the swap for
you. Frame bodies posted to the panel are base64 because its web server keeps a
plain body in a NUL-terminated string, where the first 0x00 byte would cut the
frame short. The relay's `POST /frame` takes raw bytes. Firmware uploads to
`/u` need `Expect:` turned off (`curl -H 'Expect:'`), or the server answers
before the body arrives.

## Mirror a browser canvas

`tools/mirror.js` streams any same-origin `<canvas>` to the panel, so a
browser game or chart becomes the display. Set the panel address, then paste
the script into the page console or inject it over the DevTools protocol:

```js
window.__PXT_HOST = "192.168.4.1";
window.__PXT_KEY = "...";           // only if the build has a key
// paste tools/mirror.js here
window.__mirror.running = false;    // stop
```

The script letterboxes the canvas into 128×64, skips identical frames, and sends
at most about 11 frames a second. WebGL canvases clear their drawing buffer
after each composite, so a capture from a timer reads an empty buffer about
half the time and the panel flickers. The script wraps `requestAnimationFrame`
and captures right after the page's own draw callback instead. Uploads use
`mode: "cors"`; the panel answers the preflight for `X-Api-Key`.

## Relay protocol

One long-lived chunked stream carries everything. Each chunk starts with a
four-byte magic, a big-endian sequence number, then the payload:

```
PXTF + seq + 16384 bytes     raw frame, 128×64 RGB565
PXTR + seq + runs            the same frame, run encoded: hi, lo, count
PXTO + n + json              one op: brightness, ota, reboot
```

`PXTR` is only used when the client asks for it (`/stream?enc=rle`) and the
encoded form is smaller. On a typical chart it is 2.5 KB against 16 KB, which
matters because a panel's receive window and the round trip cap throughput far
below its Wi-Fi link speed.

Relay routes: `POST /frame`, `GET /stream?since=N&enc=rle`, `GET /next`, `GET /raw`,
`GET /latest`, `POST /op`, `GET`/`POST /device`, `GET /fw`, `GET /health`.

Four rules keep the stream current:

- A new stream gets only the ops posted after it connected. Replaying a queued
  `reboot` on every reconnect would restart the panel forever.
- Each op travels in its own chunk, capped at 180 bytes, because the firmware's
  op buffer is small.
- The sequence counter starts from the clock. When a client sends a `since`
  above it (a cursor from before a relay restart), the relay treats it as zero.
- The stream sends at most 4 frames a second (`STREAM_FPS`) through a 32 KB
  socket buffer. A slow panel then slows the relay down instead of falling
  minutes behind a queue of stale frames.

Frames are written straight into the display buffer as they arrive, so a
truncated frame never reaches the panel and a new one is only shown once it is
complete. `panel_show` copies into the DMA buffer and flips, so updates are
tear free.

## Recovery

The panel never trusts the image it just booted blindly. On first boot after an
update it waits for one complete frame; if none arrives it resets into the
previous image. A boot counter plus a one-shot timer catches a panel
configuration that hangs boot, and `/safe` (or two failed boots in a row) boots
with the panel disabled so the HTTP API stays reachable.

**Dump your own stock firmware before replacing it.** The vendor image is not
included here and must not be redistributed; a full 4 MB read over the serial
bootloader is the only reliable way back.

When the panel's Wi-Fi is gone, the USB serial port still works:

```sh
tools/flash_serial.py --dump stock.bin   # full 4 MB read; run before the first write
tools/flash_serial.py                    # write the newest build to 0x10000
tools/mon.py                             # reset and print the boot log
tools/serial_cmd.py state                # also test, reboot, bright N, wifi SSID PASS,
                                         # panel ACROSS DOWN [SERP] [INVCLK] [MODH]
```

`flash_serial.py` erases the OTA data partition before it writes. An earlier
over-the-air update leaves the bootloader pointed at the other app slot, and it
would keep booting the old image. Serial commands need no key, since they
assume physical access. All three tools take `--port`.

## Known limits

- One TLS session at a time while starting up: the ESP32 needs a contiguous
  block for the RSA handshake, and two sessions do not fit.
- The panel reports its state in an `X-Pxt-State` header on the stream request,
  so telemetry costs no second TLS session. A relay that never sees the panel
  reconnect will show a stale `device_age`. The `_sent` field in `/device` does
  stay current: it counts frames streamed to the panel since that connect.
- The relay keeps no history: a client that reconnects gets the newest frame.
- Posting frames straight to the panel's HTTP API at 15 a second wedged its web
  server: it still answered ping, HTTP stopped, and only a power cycle brought
  it back. The tools here stay at or below about 11 (`mirror.js` about 11,
  `orderbook.js` 8 by default), which has held.

`src/relay_ca.h` pins ISRG Root X1, the Let's Encrypt anchor. A relay on any
other certificate needs its own root pasted there, or the panel falls back to
unverified TLS after five rejected certificates, and retries the verified chain
every 30 minutes.

## License

MIT.
