#!/usr/bin/env python3
"""PixoTicker relay: a keyed queue the panel's pusher can pull from.

The panel is only reachable on its own AP, so this service never talks to it.
It stores the newest frame plus a short op queue; a puller on the same network
as the panel fetches with /next and forwards to the device.

  GET  /health                      no auth, no data
  POST /frame?fmt=565|888           raw bytes, becomes the newest frame
  POST /op                          JSON op, queued (pixel, pixels, brightness)
  GET  /next?since=N&wait=25        long poll: {seq, frame, fmt, ops}
  GET  /raw?since=N&wait=25         binary frame + X-Seq header, 204 when idle
  GET  /stream?since=N              chunked forever: "PXTF" + seq + 16384B frames
  GET  /latest                      same, no wait

Auth: X-Api-Key header or ?key=. Constant-time compare.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import socket
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
        self.frame = None
        self.rle = b""
        self.fmt = "565"
        self.ops = []  # list of (seq, dict, ts)
        self.device = {}


STATE = State()


class Handler(BaseHTTPRequestHandler):
    server_version = VERSION
    key = ""

    def log_message(self, fmt, *args):
        pass

    def _auth(self, q):
        supplied = self.headers.get("X-Api-Key") or (q.get("key", [""])[0])
        return bool(self.key) and hmac.compare_digest(supplied, self.key)

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

    def _since(self, q):
        since = int(q.get("since", ["0"])[0])
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
            wait = min(30, max(0, int(q.get("wait", ["25"])[0])))
            deadline = time.time() + wait
            with STATE.lock:
                while STATE.seq <= since and time.time() < deadline:
                    STATE.lock.wait(deadline - time.time())
                return self._json(200, self._snapshot(since))
        if u.path == "/stream":
            return self._stream(self._since(q), enc=q.get("enc", ["raw"])[0])
        if u.path == "/raw":
            since = self._since(q)
            wait = min(30, max(0, int(q.get("wait", ["25"])[0])))
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
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Firmware-Sha256", hashlib.sha256(body).hexdigest())
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, last, op_since=0, enc="raw"):
        self.send_response(200)
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
        try:
            while True:
                with STATE.lock:
                    if STATE.seq <= last:
                        STATE.lock.wait(20)
                    seq, frame, rle = STATE.seq, STATE.frame, STATE.rle
                    cutoff = time.time() - OP_TTL
                    STATE.ops = [e for e in STATE.ops if e[2] >= cutoff]
                    ops = [o for s, o, t in STATE.ops if s > op_seq]
                    if ops:
                        op_seq = STATE.seq
                for op in ops:
                    # One op per message: the firmware buffer is small, and a
                    # batch that overflows would be dropped after /op said ok.
                    body = json.dumps([op]).encode()
                    if len(body) > OP_MAX_BYTES:
                        continue
                    payload = b"PXTO" + bytes([len(body)]) + body
                    self.wfile.write(("%X\r\n" % len(payload)).encode() + payload + b"\r\n")
                    self.wfile.flush()
                if frame and seq > last:
                    # The panel's receive window caps it near 25 KB/s, so the
                    # run-encoded form goes out whenever it is smaller.
                    if enc == "rle" and rle and len(rle) < len(frame):
                        payload = b"PXTR" + seq.to_bytes(4, "big") + rle
                    else:
                        payload = b"PXTF" + seq.to_bytes(4, "big") + frame
                    self.wfile.write(("%X\r\n" % len(payload)).encode() + payload + b"\r\n")
                    last = seq
                    if STREAM_FPS > 0:
                        time.sleep(1.0 / STREAM_FPS)
                else:
                    self.wfile.write(b"1\r\n \r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

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
        if not self._auth(q):
            return self._json(401, {"error": "unauthorized"})
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0 or n > max(MAX_FRAME, 8192):
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
            if len(json.dumps([op])) > OP_MAX_BYTES:
                return self._json(413, {"error": "op too large"})
            with STATE.lock:
                STATE.seq += 1
                STATE.ops.append((STATE.seq, op, time.time()))
                STATE.ops = STATE.ops[-MAX_OPS:]
                STATE.lock.notify_all()
                seq = STATE.seq
            return self._json(200, {"ok": True, "seq": seq})

        return self._json(404, {"error": "not found"})


def main():
    global MAX_FRAME
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18100)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--key-file", default=os.path.expanduser("~/.pxt_relay_key"))
    args = ap.parse_args()

    try:
        with open(args.key_file) as f:
            Handler.key = f.read().strip()
    except OSError:
        raise SystemExit(f"no key: write a long random string to {args.key_file} "
                         f"or pass --key-file")
    if len(Handler.key) < 16:
        raise SystemExit(f"key in {args.key_file} is shorter than 16 characters")

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"{VERSION} on {args.bind}:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
