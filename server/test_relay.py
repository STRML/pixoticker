#!/usr/bin/env python3
"""Loopback checks for pxtrelay.py: start it on 127.0.0.1, talk to it the way
the panel and the tools do, and print one PASS/FAIL line per check.

    python3 server/test_relay.py            # exits 1 if any check fails

Stdlib only. Nothing leaves the machine; the key is random per run.
"""
import hashlib
import hmac
import http.client
import json
import os
import random
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

RELAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pxtrelay.py")
PORT = 18979
UDP_PORT = 18980
KEY = os.urandom(16).hex()
FRAME = 128 * 64 * 2
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def req(path, data=None):
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data,
                               headers={"X-Api-Key": KEY})
    return json.load(urllib.request.urlopen(r, timeout=5))


def stream(state, query=""):
    """Open a panel-style stream and drain it in the background."""
    s = socket.create_connection(("127.0.0.1", PORT))
    s.sendall((f"GET /stream?since=0{query} HTTP/1.1\r\nHost: x\r\nX-Api-Key: {KEY}\r\n"
               f"X-Pxt-State: {state}\r\nConnection: close\r\n\r\n").encode())

    def drain():
        try:
            while s.recv(65536):
                pass
        except OSError:
            pass

    threading.Thread(target=drain, daemon=True).start()
    return s


def device_state():
    # The panel's telemetry is display only: capped, and unknown or nested
    # fields are dropped.
    stream(json.dumps({"build": "b1", "uptime": 5, "phase": "stream", "evil": "x",
                       "heap": {"a": 1}, "tls": "verified"}))
    time.sleep(0.5)
    dev = req("/device")
    check("X-Pxt-State keeps known scalar fields only",
          dev.get("build") == "b1" and "evil" not in dev and "heap" not in dev, json.dumps(dev))
    stream(json.dumps({"build": "y" * 700}))
    time.sleep(0.5)
    check("X-Pxt-State over 600 bytes is ignored", req("/device").get("build") == "b1")


def sent_counter():
    # publish_fw.sh treats _sent > 0 on a restarted panel as proof it draws. A
    # stale stream from before the reboot must not count toward it.
    req("/frame", bytes(FRAME))
    stream(json.dumps({"build": "old", "uptime": 900, "frames": 50}))
    time.sleep(0.5)
    stream(json.dumps({"build": "new", "uptime": 3, "frames": 0}))
    time.sleep(0.5)
    req("/frame", b"\x01" * FRAME)
    time.sleep(0.8)
    dev = req("/device")
    check("_sent counts frames streamed to the current device only",
          dev.get("build") == "new" and dev.get("_sent") == 2, f"_sent={dev.get('_sent')}")


def http11():
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    c.request("GET", "/health")
    r = c.getresponse()
    r.read()
    check("status line is HTTP/1.1", r.version == 11, f"version={r.version}")
    c.request("POST", "/frame", body=bytes(FRAME), headers={"X-Api-Key": KEY})
    r = c.getresponse()
    seq = json.loads(r.read())["seq"]
    c.request("GET", f"/raw?since={seq}&wait=0", headers={"X-Api-Key": KEY})
    r = c.getresponse()
    r.read()
    check("idle /raw is a 204 with Content-Length 0 on a kept-alive connection",
          r.status == 204 and r.getheader("Content-Length") == "0", f"status={r.status}")
    c.close()

    # A 401 leaves the body unread; it must not run as a second request.
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    inner = b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n"
    s.sendall(b"POST /frame HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(inner) + inner)
    data = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        pass
    s.close()
    check("a 401 with an unread body closes the connection",
          data.count(b"HTTP/1.1 ") == 1 and b" 401 " in data)

    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    s.sendall(f"GET /stream?since=0 HTTP/1.1\r\nHost: x\r\nX-Api-Key: {KEY}\r\n\r\n".encode())
    head = s.recv(4096).split(b"\r\n\r\n")[0].decode()
    s.close()
    check("the stream is HTTP/1.1 chunked",
          head.startswith("HTTP/1.1 200") and "Transfer-Encoding: chunked" in head)


FW = bytes((i * 7 + 3) % 256 for i in range(5000))


def fw_get(rng=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    headers = {"X-Api-Key": KEY}
    if rng:
        headers["Range"] = rng
    c.request("GET", "/fw", headers=headers)
    r = c.getresponse()
    body = r.read()
    out = (r.status, r.getheaders(), body)
    c.close()
    return out


def header(headers, name):
    return next((v for k, v in headers if k.lower() == name), None)


def firmware_ranges():
    # A panel whose download dies partway reconnects and asks for the rest.
    status, headers, body = fw_get()
    check("a whole firmware download is a 200 that advertises ranges",
          status == 200 and body == FW and header(headers, "accept-ranges") == "bytes",
          f"status={status}")
    status, headers, body = fw_get("bytes=1000-")
    check("Range: bytes=N- resumes at N with a 206 and a Content-Range",
          status == 206 and body == FW[1000:] and header(headers, "content-range") == "bytes 1000-4999/5000"
          and header(headers, "content-length") == "4000", f"status={status} len={len(body)}")
    status, headers, body = fw_get("bytes=100-199")
    check("Range: bytes=N-M returns exactly that slice",
          status == 206 and body == FW[100:200] and header(headers, "content-range") == "bytes 100-199/5000")
    status, headers, body = fw_get("bytes=4000-9999")
    check("a range past the end is clipped to the end",
          status == 206 and body == FW[4000:] and header(headers, "content-range") == "bytes 4000-4999/5000")
    status, headers, body = fw_get("bytes=5000-")
    check("a range that starts past the end is a 416 that names the size",
          status == 416 and header(headers, "content-range") == "bytes */5000", f"status={status}")
    status, headers, body = fw_get("bytes=abc-")
    check("an unreadable Range header is ignored, not an error", status == 200 and body == FW, f"status={status}")
    status, headers, body = fw_get("bytes=0-99,200-299")
    check("several ranges are not supported and get the whole file", status == 200 and body == FW)
    check("the firmware hash header is on a ranged reply too",
          header(fw_get("bytes=10-")[1], "x-firmware-sha256") == hashlib.sha256(FW).hexdigest())


def ota_state():
    stream(json.dumps({"build": "b2", "ota": "ranged 65536/1153312 closed", "evil": "x"}))
    time.sleep(0.5)
    check("X-Pxt-State carries the panel's last download note",
          req("/device").get("ota") == "ranged 65536/1153312 closed")


# ---------------------------------------------------------------- UDP frames
# The panel says hello on UDP; the relay answers with the newest frame as
# signed strips. Panel below mirrors the firmware's rules in src/main.cpp
# (udpFeed and udpSettle) one for one, so these checks cover both ends.

MAC = 16
HEAD = 21          # PXTS, seq, boot, hello echo, index, count, first row, rows, enc
LIVE_S = 10        # an echo of a hello older than this many seconds is not live
ROWS = 64
ROW_BYTES = 256
PARTIAL_S = 0.25
_hello_ts = [0]
_hellos = {}       # hello clock -> monotonic time it was sent, as the panel keeps them


def sign(data):
    return hmac.new(KEY.encode(), data, hashlib.sha256).digest()[:MAC]


def hello_bytes(boot, newest=0, whole=0, ts=None):
    if ts is None:
        _hello_ts[0] = max(_hello_ts[0] + 1, int(time.time()))
        ts = _hello_ts[0]
    _hellos[ts] = time.monotonic()
    d = b"PXTH" + struct.pack(">IIII", ts, boot, newest, whole)
    return d + sign(d)


def udp_sock(rcvbuf=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    s.bind(("127.0.0.1", 0))
    return s


def say_hello(s, boot, newest=0, whole=0, ts=None):
    d = hello_bytes(boot, newest, whole, ts)
    s.sendto(d, ("127.0.0.1", UDP_PORT))
    return d


def collect(s, quiet=0.3, limit=5.0):
    """Every datagram until the socket has been quiet for `quiet` seconds."""
    out = []
    end = time.time() + limit
    s.settimeout(quiet)
    while time.time() < end:
        try:
            out.append(s.recv(2048))
        except socket.timeout:
            break
    return out


def unrle(body, npx):
    if len(body) % 3:
        return None
    px = bytearray()
    for i in range(0, len(body), 3):
        px += bytes(body[i:i + 2]) * body[i + 2]
    return bytes(px) if len(px) == npx * 2 else None


class Panel:
    def __init__(self, boot, clock=None):
        self.boot = boot
        self.clock = clock  # the panel's monotonic clock; default: now
        self.live = 0
        self.fb = bytearray(FRAME)
        self.cur = 0
        self.mask = 0
        self.full = 0
        self.first = 0.0
        self.shown = False
        self.complete = False
        self.shows = []   # (seq, complete, fb copy)
        self.bad = 0

    def feed(self, d, now=None):
        now = time.time() if now is None else now
        if len(d) < 16 + MAC or not hmac.compare_digest(sign(d[:-MAC]), d[-MAC:]):
            self.bad += 1
            return
        seq, boot, echo = struct.unpack(">III", d[4:16])
        mono = time.monotonic() if self.clock is None else self.clock
        if boot != self.boot or echo not in _hellos or mono - _hellos[echo] > LIVE_S:
            return
        self.live += 1
        if d[:4] != b"PXTS" or len(d) < HEAD + MAC:
            return
        idx, count, row0, nrows, enc = d[16:HEAD]
        body = d[HEAD:-MAC]
        if not 0 < count <= 16 or idx >= count or row0 + nrows > ROWS:
            return
        if enc == 0:
            px = body if len(body) == nrows * ROW_BYTES else None
        else:
            px = unrle(body, nrows * 128) if enc == 1 else None
        if px is None or seq < self.cur:
            return
        if seq > self.cur:
            # An unshown frame's deadline carries over, so frames that each
            # lose a strip still show.
            if not self.mask or self.shown:
                self.first = now
            self.cur, self.mask, self.full = seq, 0, (1 << count) - 1
            self.shown, self.complete = False, False
        if self.mask & (1 << idx):
            return
        self.fb[row0 * ROW_BYTES:(row0 + nrows) * ROW_BYTES] = px
        self.mask |= 1 << idx

    def settle(self, now=None):
        now = time.time() if now is None else now
        if self.mask and self.mask == self.full and not self.complete:
            self.complete = self.shown = True
            self.shows.append((self.cur, True, bytes(self.fb)))
        elif self.mask and not self.shown and now - self.first > PARTIAL_S:
            self.shown = True
            self.shows.append((self.cur, False, bytes(self.fb)))

    def take(self, dgrams):
        for d in dgrams:
            self.feed(d)
        self.settle()


def post_frame(body):
    return req("/frame", body)["seq"]


def noise():
    return os.urandom(FRAME)


def udp_ack_without_frame():
    s = udp_sock()
    say_hello(s, 1)
    got = collect(s)
    check("a hello with no frame yet gets a signed ack",
          len(got) == 1 and got[0][:4] == b"PXTA" and sign(got[0][:-MAC]) == got[0][-MAC:],
          f"got={[g[:4] for g in got]}")
    s.close()


def udp_hello_auth():
    s = udp_sock()
    d = bytearray(hello_bytes(2))
    d[6] ^= 1
    s.sendto(bytes(d), ("127.0.0.1", UDP_PORT))
    check("a hello with a bad HMAC gets no answer", collect(s) == [])
    say_hello(s, 2, ts=int(time.time()) - 300)
    check("a hello five minutes old gets no answer", collect(s) == [])
    good = say_hello(s, 2)
    collect(s)
    t = udp_sock()
    t.sendto(good, ("127.0.0.1", UDP_PORT))
    check("a replayed hello from another address gets no answer", collect(t) == [])
    s.close()
    t.close()


def udp_strips():
    BOOT = 3
    s = udp_sock()
    say_hello(s, BOOT)
    collect(s)
    for name, frame in (("noise", noise()), ("flat", bytes(FRAME))):
        seq = post_frame(frame)
        got = collect(s)
        p = Panel(BOOT)
        p.take(got)
        biggest = max((len(g) for g in got), default=0)
        check(f"a posted {name} frame arrives as signed strips of 1400 bytes or less that rebuild it",
              got and biggest <= 1400 and p.shows and p.shows[-1][:2] == (seq, True)
              and p.shows[-1][2] == frame, f"n={len(got)} max={biggest}")
    s.close()


def udp_loss_order_tamper():
    BOOT = 4
    s = udp_sock()
    say_hello(s, BOOT)
    collect(s)
    frame = noise()
    seq = post_frame(frame)
    got = [g for g in collect(s) if g[:4] == b"PXTS"]

    p = Panel(BOOT)
    p.take(got[:5] + got[6:])
    time.sleep(PARTIAL_S + 0.05)
    p.settle()
    lost = got[5] if len(got) > 5 else bytes(32)
    row0, nrows = lost[18], lost[19]
    keep = bytes(p.fb[:row0 * ROW_BYTES]) + bytes(p.fb[(row0 + nrows) * ROW_BYTES:])
    want = frame[:row0 * ROW_BYTES] + frame[(row0 + nrows) * ROW_BYTES:]
    check("a lost strip costs only its rows: the rest shows after the partial timeout",
          bool(p.shows) and p.shows[-1][:2] == (seq, False) and keep == want)
    say_hello(s, BOOT, newest=p.cur)
    p.take(collect(s))
    check("the next hello resends the frame and fills the lost strip",
          bool(p.shows) and p.shows[-1][:2] == (seq, True) and bytes(p.fb) == frame)

    p = Panel(BOOT)
    shuffled = list(got)
    random.shuffle(shuffled)
    p.take(shuffled)
    check("strips in any order rebuild the frame", bool(p.shows) and p.shows[-1][1] and bytes(p.fb) == frame)

    p = Panel(BOOT)
    bad = bytearray(got[0] if got else bytes(64))
    bad[30] ^= 0xFF
    p.take([bytes(bad)])
    check("a strip with a bad HMAC is dropped", p.bad == 1 and p.mask == 0 and bytes(p.fb) == bytes(FRAME))

    newer = noise()
    seq2 = post_frame(newer)
    got2 = [g for g in collect(s) if g[:4] == b"PXTS"]
    p = Panel(BOOT)
    p.take(got2)
    p.take(got)
    check("strips of an older frame replayed after a newer one are dropped",
          p.cur == seq2 and bytes(p.fb) == newer and len(p.shows) == 1 and seq2 > seq)
    s.close()


def udp_slow_panel():
    # A panel that falls behind must come back to the newest frame, never play
    # out a backlog. The relay sends only the newest frame it holds.
    BOOT = 5
    s = udp_sock(rcvbuf=1 << 20)
    say_hello(s, BOOT)
    collect(s)
    frames = [noise() for _ in range(30)]
    seqs = [post_frame(f) for f in frames]
    time.sleep(1.0)
    backlog = collect(s)
    order = [int.from_bytes(g[4:8], "big") for g in backlog if g[:4] == b"PXTS"]
    check("the relay never sends an older frame after a newer one and skips superseded frames",
          bool(order) and order == sorted(order) and order[-1] == seqs[-1] and len(set(order)) < len(seqs),
          f"posted={len(seqs)} sent={len(set(order))}")
    p = Panel(BOOT)
    p.take(backlog)
    say_hello(s, BOOT, newest=p.cur)
    p.take(collect(s))
    check("a slow panel shows the newest frame after it drains, not a backlog",
          bool(p.shows) and p.shows[-1][0] == seqs[-1] and bytes(p.fb) == frames[-1])
    s.close()


def udp_restart_seq():
    # The relay's counter restarts from the clock. A panel that saw a higher
    # seq would drop every new frame as old, so its hello names that seq and
    # the relay moves past it.
    BOOT = 6
    s = udp_sock()
    seq = post_frame(noise())
    collect(s)
    say_hello(s, BOOT, newest=seq + 100000)
    p = Panel(BOOT)
    p.cur = seq + 100000
    p.take(collect(s))
    check("after a relay restart the panel's higher seq is passed, so the current frame shows",
          bool(p.shows) and p.shows[-1][0] > seq + 100000)
    check("frames after that keep counting up from there", post_frame(noise()) > seq + 100000)
    s.close()


def udp_sent_counter():
    # _sent is what the panel says it showed whole, and only for the boot that
    # holds the control stream. A send the link dropped never counts.
    stream(json.dumps({"build": "udp", "uptime": 2, "boot": 777}), "&frames=0")
    time.sleep(0.4)
    s = udp_sock()
    say_hello(s, 555, whole=5)
    collect(s)
    post_frame(noise())
    collect(s)
    dev = req("/device")
    check("frames sent by UDP do not count toward _sent until the panel reports them",
          dev.get("boot") == 777 and dev.get("_sent") == 0 and dev.get("path") == "udp", json.dumps(dev))
    say_hello(s, 777, whole=2)
    collect(s)
    sent = req("/device").get("_sent")
    check("_sent takes the whole frames the current boot reports in its hello", sent == 2, f"_sent={sent}")
    say_hello(s, 777, whole=1)
    collect(s)
    sent = req("/device").get("_sent")
    check("_sent never goes down", sent == 2, f"_sent={sent}")
    s.close()


def udp_session():
    # A datagram names the boot it was sent to and echoes that panel's hello
    # clock. A replay to a rebooted panel, or a replay used to fake liveness
    # after the relay went quiet, is dropped.
    s = udp_sock()
    say_hello(s, 8)
    collect(s)
    frame = noise()
    post_frame(frame)
    got = collect(s)
    other = Panel(9)
    other.take(got)
    check("a datagram for another boot is dropped", other.live == 0 and not other.shows)
    later = Panel(8, clock=time.monotonic() + LIVE_S + 2)
    later.take(got)
    check("a datagram that echoes an old hello is dropped and is not live", later.live == 0 and not later.shows)
    mine = Panel(8)
    mine.take(got)
    check("a datagram for this boot and a recent hello shows", mine.live > 0 and bytes(mine.fb) == frame)
    s.close()


def udp_partial_deadline():
    # Frames closer than the partial timeout that each lose a strip must still
    # show: the oldest unshown frame's deadline carries over.
    BOOT = 10
    s = udp_sock()
    say_hello(s, BOOT)
    collect(s)
    p = Panel(BOOT)
    for _ in range(12):
        post_frame(noise())
        for d in collect(s, quiet=0.08):
            if d[:4] == b"PXTS" and d[16] == 3:
                continue
            p.feed(d)
        p.settle()
    check("frames that each lose a strip still show within the partial timeout",
          len(p.shows) >= 2, f"shows={len(p.shows)}")
    s.close()


def read_for(s, seconds, until=None):
    data = b""
    end = time.time() + seconds
    s.settimeout(0.3)
    while time.time() < end and not (until and until in data):
        try:
            data += s.recv(65536)
        except socket.timeout:
            pass
    return data


def control_stream():
    s = socket.create_connection(("127.0.0.1", PORT), timeout=3)
    s.sendall((f"GET /stream?since=0&enc=rle&frames=0 HTTP/1.1\r\nHost: x\r\nX-Api-Key: {KEY}\r\n"
               f"Connection: close\r\n\r\n").encode())
    time.sleep(0.2)
    for _ in range(5):
        post_frame(noise())
        time.sleep(0.2)
    req("/op", json.dumps({"type": "brightness", "value": 40}).encode())
    body = read_for(s, 1.5).split(b"\r\n\r\n", 1)[-1]
    s.close()
    check("a frames=0 stream carries the op and no frames",
          b"PXTO" in body and b'"brightness"' in body and b"PXTF" not in body and b"PXTR" not in body
          and len(body) < 400, f"bytes={len(body)}")


def old_stream():
    frame = noise()
    s = socket.create_connection(("127.0.0.1", PORT), timeout=3)
    s.sendall((f"GET /stream?since=0 HTTP/1.1\r\nHost: x\r\nX-Api-Key: {KEY}\r\n"
               f"X-Pxt-State: {json.dumps({'build': 'old', 'uptime': 9})}\r\nConnection: close\r\n\r\n").encode())
    time.sleep(0.3)
    seq = post_frame(frame)
    tag = b"PXTF" + seq.to_bytes(4, "big")
    data = read_for(s, 2.0, until=frame[-64:])
    s.close()
    at = data.find(tag)
    check("an old-firmware stream (no frames flag) still gets every frame on /stream",
          at >= 0 and data[at + 8:at + 8 + FRAME] == frame)
    check("an old-firmware stream registers the device on the tcp path", req("/device").get("path") == "tcp")


def op_too_big():
    try:
        req("/op", json.dumps({"type": "ota", "path": "x" * 300}).encode())
        code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    check("an op over 180 bytes is refused with a 413", code == 413, f"code={code}")


def main():
    with tempfile.NamedTemporaryFile("w", suffix=".key") as kf, tempfile.NamedTemporaryFile(suffix=".bin") as fwf:
        kf.write(KEY)
        kf.flush()
        fwf.write(FW)
        fwf.flush()
        proc = subprocess.Popen([sys.executable, RELAY, "--port", str(PORT), "--key-file", kf.name,
                                 "--firmware", fwf.name, "--udp-port", str(UDP_PORT)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            udp_ack_without_frame()
            udp_hello_auth()
            device_state()
            sent_counter()
            http11()
            firmware_ranges()
            ota_state()
            udp_strips()
            udp_loss_order_tamper()
            udp_slow_panel()
            udp_restart_seq()
            udp_sent_counter()
            udp_session()
            udp_partial_deadline()
            control_stream()
            old_stream()
            op_too_big()
        finally:
            proc.terminate()
            proc.wait()
    print("ALL PASS" if all(results) else "SOME FAILED")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
