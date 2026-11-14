#!/usr/bin/env python3
"""PixoTicker relay: holds the newest frame and a short op queue for the panel.

  GET  /health                      no auth, no data
  POST /frame?fmt=565|888           raw bytes, becomes the newest frame
  POST /op                          JSON op, queued (brightness, reboot, ota)
  GET  /next?since=N&wait=25        long poll: {seq, frame, fmt, ops}
  GET  /raw?since=N&wait=25         binary frame + X-Seq header, 204 when idle
  GET  /stream?since=N              chunked forever: "PXTF" + seq + 16384B frames
  GET  /stream?frames=0             the same stream with ops and keepalives only
  GET  /latest                      same as /next, no wait
  UDP  --udp-port                   hello in, signed frame strips out

Auth: X-Api-Key header or ?key=. Constant-time compare. UDP datagrams carry a
truncated HMAC-SHA256 under the same key.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "pxtrelay-1"
MAX_FRAME = 128 * 64 * 3
MAX_OPS = 64
KNOWN_OPS = ("brightness", "reboot", "ota")
OP_MAX_BYTES = 180  # firmware op buffer is 224 bytes
OP_TTL = 900        # an op older than this belongs to nobody
STREAM_FPS = 4
FW_PATH = os.path.expanduser("~/firmware/pxt.bin")
RANGE = re.compile(r"^bytes=(\d+)-(\d*)$")
KEEPALIVE = 15      # seconds between keepalives on a frames=0 stream

# UDP frame channel. A frame goes out as 16 strips of 4 rows. Each strip
# decodes on its own, so a lost datagram costs 4 rows of one frame.
UDP_PORT = 18102
MAC_BYTES = 16
STRIP_ROWS = 4
ROW_BYTES = 128 * 2
HELLO_TTL = 20      # forget the panel's address after this long without a hello
NONCE_BYTES = 8
STRIP_GAP = 0.002   # spacing between strips, so the panel's small UDP queue keeps up


def rle565(raw):
    """Pixel runs: hi, lo, count. Flat areas cost three bytes per run."""
    out = bytearray()
    n = len(raw) // 2
    i = 0
    while i < n:
        hi, lo = raw[i * 2], raw[i * 2 + 1]
        run = 1
        while i + run < n and run < 255 and raw[(i + run) * 2] == hi and raw[(i + run) * 2 + 1] == lo:
            run += 1
        out += bytes((hi, lo, run))
        i += run
    return bytes(out)


class State:
    def __init__(self):
        self.lock = threading.Condition()
        # Time-seeded: a client holding a higher `since` from before a restart
        # must not wait forever for a frame that already happened.
        self.seq = int(time.time())
        self.frame_seq = 0  # seq of the newest frame; ops move seq, not this
        self.frame = None
        self.rle = b""
        self.fmt = "565"
        self.ops = []  # list of (seq, dict, ts)
        self.device = {}
        self.udp_addr = None
        self.udp_boot = None
        self.udp_seen = 0.0           # time.monotonic() of the last hello
        self.udp_nonce = b""          # the last hello's nonce, echoed in every reply
        self.udp_hello_n = 0          # the last hello's counter under the current token
        self.udp_token = None         # handed to the panel on its control stream
        self.udp_kick = False


STATE = State()


class Handler(BaseHTTPRequestHandler):
    server_version = VERSION
    # The stream is chunked, which HTTP/1.0 does not define. Every other
    # response carries a Content-Length, so keep-alive is safe.
    protocol_version = "HTTP/1.1"
    key = ""

    def log_message(self, fmt, *args):
        pass

    def _auth(self, q):
        supplied = self.headers.get("X-Api-Key") or (q.get("key", [""])[0])
        return bool(self.key) and hmac.compare_digest(supplied.encode(), self.key.encode())

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # puller went away mid response

    def _raw(self, code, body, seq, fmt):
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Seq", str(seq))
            self.send_header("X-Fmt", fmt)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    @staticmethod
    def _int(q, name, default):
        try:
            return int(q.get(name, [str(default)])[0])
        except ValueError:
            return default

    def _since(self, q):
        since = self._int(q, "since", 0)
        # A restart reseeds the counter; a cursor from a previous run can sit
        # above it. Without this the stream waits until it catches up (hours).
        return 0 if since > STATE.seq else since

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            with STATE.lock:
                return self._json(200, {"ok": True, "version": VERSION, "seq": STATE.seq,
                                        "pending_ops": len(STATE.ops),
                                        "device_age": (int(time.time()) - STATE.device.get("_seen", 0)) if STATE.device else None})
        if not self._auth(q):
            return self._json(401, {"error": "unauthorized"})
        if u.path == "/next":
            since = self._since(q)
            wait = min(30, max(0, self._int(q, "wait", 25)))
            deadline = time.time() + wait
            with STATE.lock:
                while STATE.seq <= since and time.time() < deadline:
                    STATE.lock.wait(deadline - time.time())
                return self._json(200, self._snapshot(since))
        if u.path == "/stream":
            return self._stream(self._since(q), enc=q.get("enc", ["raw"])[0],
                                frames=q.get("frames", ["1"])[0] != "0")
        if u.path == "/raw":
            since = self._since(q)
            wait = min(30, max(0, self._int(q, "wait", 25)))
            deadline = time.time() + wait
            with STATE.lock:
                while STATE.seq <= since and time.time() < deadline:
                    STATE.lock.wait(deadline - time.time())
                seq, frame, fmt = STATE.seq, STATE.frame, STATE.fmt
            if not frame or seq <= since:
                return self._raw(204, b"", seq, fmt)
            return self._raw(200, frame, seq, fmt)
        if u.path in ("/pxt/fw", "/fw"):
            return self._firmware()
        if u.path in ("/pxt/device", "/device"):
            with STATE.lock:
                return self._json(200, STATE.device)
        if u.path == "/latest":
            with STATE.lock:
                return self._json(200, self._snapshot(0))
        return self._json(404, {"error": "not found"})

    def _firmware(self):
        try:
            with open(FW_PATH, "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"error": "no firmware"})
        size = len(body)
        start, end = 0, size - 1
        code = 200
        # A panel whose download dies partway reconnects and asks for the rest.
        # One range, "bytes=N-" or "bytes=N-M"; anything else gets the whole file.
        m = RANGE.match(self.headers.get("Range", "").strip())
        if m:
            first = int(m.group(1))
            last = int(m.group(2)) if m.group(2) else size - 1
            if first >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if last >= first:
                start, end, code = first, min(last, size - 1), 206
        part = body[start:end + 1]
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(part)))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("X-Firmware-Sha256", hashlib.sha256(body).hexdigest())
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(part)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the panel hung up mid-download; it will ask again from where it stopped

    def _register(self, frames):
        # The panel reports itself on the request rather than opening a second
        # TLS session it cannot afford: the handshake needs a contiguous block
        # the stream already holds. Display only, so it is capped and only
        # known short scalar fields are kept.
        state = self.headers.get("X-Pxt-State")
        if not state or len(state) > 600:
            return None
        try:
            raw = json.loads(state)
            dev = {k: v for k, v in raw.items()
                   if k in ("build", "uptime", "heap", "maxblock", "frames", "errors",
                            "phase", "rssi", "tls", "ota", "boot", "udp")
                   and isinstance(v, (int, str)) and len(str(v)) < 48}
        except Exception:
            return None
        dev["_seen"] = int(time.time())
        # The header is frozen at connect, so its frame count stays at zero
        # after a reboot. _sent counts frames sent to this connection's panel,
        # on this stream (tcp) or by UDP to the same boot (udp), which is how
        # publish_fw.sh sees a new build draw.
        dev["_sent"] = 0
        dev["path"] = "tcp" if frames else "udp"
        with STATE.lock:
            STATE.device = dev
            # UDP hellos must carry this token. Every connect, and so every
            # relay restart, hands out a new one, and a hello captured under an
            # earlier one is dead.
            STATE.udp_token = os.urandom(NONCE_BYTES)
            STATE.udp_hello_n = 0
            self.udp_token = STATE.udp_token
        return dev

    @staticmethod
    def _take_ops(op_seq):
        """Ops posted after op_seq, and the cursor past them. Caller holds the lock."""
        cutoff = time.time() - OP_TTL
        STATE.ops = [e for e in STATE.ops if e[2] >= cutoff]
        ops = [o for s, o, t in STATE.ops if s > op_seq]
        return ops, (STATE.seq if ops else op_seq)

    def _stream(self, last, op_since=0, enc="raw", frames=True):
        self.udp_token = None
        mine = self._register(frames)
        self.send_response(200)
        if self.udp_token:
            self.send_header("X-Pxt-Udp", self.udp_token.hex())
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with STATE.lock:
            # Only ops posted after this connect: replaying a queued reboot
            # would restart the panel on every reconnect, forever.
            op_seq = op_since if op_since else STATE.seq
        try:
            # A small send buffer makes a slow panel throttle this loop instead
            # of queueing minutes of stale frames behind it.
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)
        except OSError:
            pass
        wrote = time.time()
        try:
            while True:
                with STATE.lock:
                    if frames and STATE.seq <= last:
                        STATE.lock.wait(20)
                    elif not frames and not self._take_ops(op_seq)[0]:
                        # Frames travel by UDP: wake for ops, and send a
                        # keepalive only when the stream has been quiet.
                        STATE.lock.wait(max(0.1, KEEPALIVE - (time.time() - wrote)))
                    seq, frame, rle = STATE.seq, STATE.frame, STATE.rle
                    ops, op_seq = self._take_ops(op_seq)
                for op in ops:
                    # One op per message: the firmware buffer is small, and a
                    # batch that overflows would be dropped after /op said ok.
                    body = json.dumps([op], separators=(",", ":")).encode()
                    if len(body) > OP_MAX_BYTES:
                        continue
                    payload = b"PXTO" + bytes([len(body)]) + body
                    self.wfile.write(("%X\r\n" % len(payload)).encode() + payload + b"\r\n")
                    self.wfile.flush()
                    wrote = time.time()
                if not frames:
                    if time.time() - wrote >= KEEPALIVE:
                        self.wfile.write(b"1\r\n \r\n")
                        self.wfile.flush()
                        wrote = time.time()
                    continue
                if frame and seq > last:
                    # The panel's receive window caps it near 25 KB/s, so the
                    # run-encoded form goes out whenever it is smaller.
                    if enc == "rle" and rle and len(rle) < len(frame):
                        payload = b"PXTR" + seq.to_bytes(4, "big") + rle
                    else:
                        payload = b"PXTF" + seq.to_bytes(4, "big") + frame
                    self.wfile.write(("%X\r\n" % len(payload)).encode() + payload + b"\r\n")
                    last = seq
                    with STATE.lock:
                        # A stale stream from before a reboot must not count.
                        if mine is not None and STATE.device is mine:
                            mine["_sent"] += 1
                    if STREAM_FPS > 0:
                        time.sleep(1.0 / STREAM_FPS)
                else:
                    self.wfile.write(b"1\r\n \r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def _snapshot(self, since):
        return {
            "seq": STATE.seq,
            "frame": base64.b64encode(STATE.frame).decode() if STATE.frame else None,
            "fmt": STATE.fmt,
            "ops": [op for s, op, t in STATE.ops if s > since],
        }

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        # Both early answers leave the body unread, so the connection cannot
        # carry another request.
        if not self._auth(q):
            self.close_connection = True
            return self._json(401, {"error": "unauthorized"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > max(MAX_FRAME, 8192):
            self.close_connection = True
            return self._json(413, {"error": "bad size"})
        body = self.rfile.read(n)

        if u.path == "/frame":
            fmt = q.get("fmt", ["565"])[0]
            if fmt not in ("565", "888"):
                return self._json(400, {"error": "fmt"})
            if fmt == "888":
                if len(body) % 3:
                    return self._json(400, {"error": "888 body"})
                out = bytearray(len(body) // 3 * 2)
                for i in range(0, len(body), 3):
                    r, g, b = body[i], body[i + 1], body[i + 2]
                    v = ((r & 0xF8) << 8) | ((b & 0xFC) << 3) | (g >> 3)
                    j = i // 3 * 2
                    out[j] = v >> 8
                    out[j + 1] = v & 0xFF
                body = bytes(out)
                fmt = "565"  # the stream carries 565 only
            # The panel reads exactly 16384 bytes per frame; anything else
            # would leave it mid-frame and desync the stream.
            if len(body) != 128 * 64 * 2:
                return self._json(400, {"error": "frame must be 128x64"})
            with STATE.lock:
                STATE.frame = body
                STATE.rle = rle565(body)
                STATE.fmt = fmt
                STATE.seq += 1
                STATE.frame_seq = STATE.seq
                STATE.lock.notify_all()
            return self._json(200, {"ok": True, "seq": STATE.seq})

        if u.path in ("/pxt/device", "/device"):
            try:
                dev = json.loads(body)
                dev["_seen"] = int(time.time())
            except Exception:
                return self._json(400, {"error": "bad json"})
            with STATE.lock:
                STATE.device = dev
            return self._json(200, {"ok": True})

        if u.path == "/op":
            try:
                op = json.loads(body)
                if not isinstance(op, dict) or op.get("type") not in KNOWN_OPS:
                    raise ValueError
            except Exception:
                return self._json(400, {"error": "bad op"})
            if len(json.dumps([op], separators=(",", ":"))) > OP_MAX_BYTES:
                return self._json(413, {"error": "op too large"})
            with STATE.lock:
                STATE.seq += 1
                STATE.ops.append((STATE.seq, op, time.time()))
                STATE.ops = STATE.ops[-MAX_OPS:]
                STATE.lock.notify_all()
                seq = STATE.seq
            return self._json(200, {"ok": True, "seq": seq})

        return self._json(404, {"error": "not found"})


def mac(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()[:MAC_BYTES]


def session(seq, boot, nonce):
    """Every datagram to the panel names its boot and echoes the nonce of its
    latest hello. The panel keeps its last few nonces and drops anything else,
    so a captured datagram replayed later never draws or keeps a dead link
    looking alive. No clock is involved."""
    return struct.pack(">II", seq, boot) + nonce


def strips(frame, seq, key, boot, nonce):
    """One frame as self-contained datagrams: PXTS, seq, boot (u32 each), nonce
    (8 bytes), index, count, first row, rows, enc (0 raw, 1 runs), pixels, mac."""
    count = 64 // STRIP_ROWS
    span = STRIP_ROWS * ROW_BYTES
    out = []
    for i in range(count):
        raw = frame[i * span:(i + 1) * span]
        runs = rle565(raw)
        enc, body = (1, runs) if len(runs) < len(raw) else (0, raw)
        d = b"PXTS" + session(seq, boot, nonce) + bytes((i, count, i * STRIP_ROWS, STRIP_ROWS, enc)) + body
        out.append(d + mac(key, d))
    return out


HELLO_BYTES = 4 + 2 * NONCE_BYTES + 16


def udp_hello(data, addr, key):
    """PXTH, nonce (8 bytes), control stream token (8 bytes), then u32 each:
    boot nonce, hello counter, newest seq shown, whole frames shown since boot.
    Then the mac."""
    if len(data) != HELLO_BYTES + MAC_BYTES or data[:4] != b"PXTH":
        return
    if not hmac.compare_digest(mac(key, data[:HELLO_BYTES]), data[HELLO_BYTES:]):
        return
    nonce = data[4:4 + NONCE_BYTES]
    token = data[4 + NONCE_BYTES:4 + 2 * NONCE_BYTES]
    boot, n, newest, whole = struct.unpack(">IIII", data[4 + 2 * NONCE_BYTES:HELLO_BYTES])
    with STATE.lock:
        dev = STATE.device
        # A hello must carry the token of the current TLS control stream, from
        # the boot that opened it. TLS cannot be replayed, so a hello captured
        # under an earlier stream, boot or relay run is dead. Under one token
        # the counter must go up, so a replay from another address is too.
        if (STATE.udp_token is None or not hmac.compare_digest(token, STATE.udp_token)
                or dev.get("boot") != boot or n <= STATE.udp_hello_n):
            return
        STATE.udp_addr, STATE.udp_boot, STATE.udp_seen = addr, boot, time.monotonic()
        STATE.udp_nonce, STATE.udp_hello_n = nonce, n
        if newest > STATE.seq:
            # The counter restarted from the clock below what the panel has
            # shown, and the panel drops older seqs. Move past it, and restamp
            # the frame we hold so it shows now.
            STATE.seq = newest + 1
            if STATE.frame:
                STATE.frame_seq = STATE.seq
        # _sent is what the panel says it showed whole: a datagram the link
        # dropped never counts toward proving a new build draws.
        if dev.get("path") == "udp":
            dev["_sent"] = max(dev.get("_sent", 0), whole)
        STATE.udp_kick = True
        STATE.lock.notify_all()


def udp_listen(sock, key):
    while True:
        try:
            data, addr = sock.recvfrom(2048)
        except OSError:
            continue
        udp_hello(data, addr, key)


def _udp_next(last):
    """Wait for something to send: a hello, or a frame newer than `last`. Caller holds the lock."""
    while True:
        fresh = STATE.udp_addr and time.monotonic() - STATE.udp_seen < HELLO_TTL
        if fresh and (STATE.udp_kick or (STATE.frame and STATE.frame_seq != last)):
            STATE.udp_kick = False
            return (STATE.udp_addr, STATE.udp_boot, STATE.udp_nonce,
                    STATE.frame, STATE.frame_seq, STATE.seq)
        STATE.lock.wait(1.0)


def udp_send(sock, key):
    # Always the newest frame: frames posted while strips go out are skipped,
    # so a slow link loses frames instead of queueing them.
    last = 0
    while True:
        with STATE.lock:
            addr, boot, nonce, frame, seq, now = _udp_next(last)
        try:
            if not frame:
                # Nothing to draw yet, but the panel learns its UDP path works.
                d = b"PXTA" + session(now, boot, nonce)
                sock.sendto(d + mac(key, d), addr)
                continue
            for d in strips(frame, seq, key, boot, nonce):
                sock.sendto(d, addr)
                time.sleep(STRIP_GAP)
        except OSError:
            time.sleep(0.5)  # an unreachable address must not spin this loop
            continue
        last = seq


def start_udp(bind, port, key):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((bind, port))
    for target in (udp_listen, udp_send):
        threading.Thread(target=target, args=(sock, key.encode()), daemon=True).start()


def main():
    global FW_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18100)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--key-file", default=os.path.expanduser("~/.pxt_relay_key"))
    ap.add_argument("--firmware", default=FW_PATH, help="image served at /fw")
    ap.add_argument("--udp-port", type=int, default=UDP_PORT, help="UDP frame channel; 0 turns it off")
    args = ap.parse_args()
    FW_PATH = os.path.expanduser(args.firmware)

    try:
        with open(args.key_file) as f:
            Handler.key = f.read().strip()
    except OSError:
        raise SystemExit(f"no key: write a long random string to {args.key_file} "
                         f"or pass --key-file")
    if len(Handler.key) < 16:
        raise SystemExit(f"key in {args.key_file} is shorter than 16 characters")

    if args.udp_port:
        start_udp(args.bind, args.udp_port, Handler.key)
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"{VERSION} on {args.bind}:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
