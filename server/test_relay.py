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
# The panel says hello on UDP with a fresh random nonce. The relay answers with
# the newest frame as signed strips that echo the nonce. Panel below mirrors
# the firmware's rules in src/main.cpp (udpSession, udpFeed, udpSettle) one
# for one. No wall clock is an input on either side: replay checks use the
# nonces, the hello counter and the boot that holds the TLS stream, and
# liveness is timed by a monotonic clock.

MAC = 16
SESSION = 20       # PXTS or PXTA, seq, boot, nonce (8)
HEAD = 25          # then index, count, first row, rows, enc
NONCES = 4         # outstanding hello nonces the panel keeps
LIVE_S = 10        # a reply to a hello older than this is not live
ROWS = 64
ROW_BYTES = 256
PARTIAL_S = 0.25


def sign(data):
    return hmac.new(KEY.encode(), data, hashlib.sha256).digest()[:MAC]


class Boot:
    """One panel boot: its boot nonce, hello counter, the token of its current
    control stream, and the hello nonces it has sent with the monotonic time
    each went out."""

    def __init__(self, boot, token=b"\0" * 8):
        self.boot = boot
        self.token = token
        self.n = 0
        self.sent = []

    def hello(self, s, newest=0, whole=0, n=None):
        self.n += 1
        nonce = os.urandom(8)
        self.sent = (self.sent + [(nonce, time.monotonic())])[-NONCES:]
        d = (b"PXTH" + nonce + self.token
             + struct.pack(">IIII", self.boot, self.n if n is None else n, newest, whole))
        d += sign(d)
        s.sendto(d, ("127.0.0.1", UDP_PORT))
        return d


def register(boot, me=None):
    """Open the panel's control stream, which names its boot to the relay, and
    take the UDP token the relay answers with. Returns the Boot."""
    state = json.dumps({"build": "udp", "uptime": 1, "boot": boot})
    c = socket.create_connection(("127.0.0.1", PORT), timeout=3)
    c.sendall((f"GET /stream?since=0&frames=0 HTTP/1.1\r\nHost: x\r\nX-Api-Key: {KEY}\r\n"
               f"X-Pxt-State: {state}\r\nConnection: close\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        head += c.recv(4096)
    token = b""
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"x-pxt-udp:"):
            token = bytes.fromhex(line.split(b":", 1)[1].strip().decode())

    def drain():
        try:
            while c.recv(65536):
                pass
        except OSError:
            pass

    threading.Thread(target=drain, daemon=True).start()
    me = me or Boot(boot)
    me.token = token
    return me


def udp_sock(rcvbuf=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    s.bind(("127.0.0.1", 0))
    return s


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
    def __init__(self, me, clock=None):
        self.me = me          # the Boot whose nonces this panel holds
        self.clock = clock    # monotonic clock override; default: now
        self.live = 0
        self.dropped = 0
        self.fb = bytearray(FRAME)
        self.cur = 0
        self.mask = 0
        self.full = 0
        self.first = 0.0
        self.shown = False
        self.complete = False
        self.shows = []   # (seq, complete, fb copy)
        self.bad = 0

    def session(self, d):
        seq, boot = struct.unpack(">II", d[4:12])
        nonce = d[12:SESSION]
        mono = time.monotonic() if self.clock is None else self.clock
        return boot == self.me.boot and any(nonce == n and mono - t <= LIVE_S for n, t in self.me.sent)

    def feed(self, d, now=None):
        now = time.time() if now is None else now
        if len(d) < SESSION + MAC or not hmac.compare_digest(sign(d[:-MAC]), d[-MAC:]):
            self.bad += 1
            return
        if not self.session(d):
            self.dropped += 1
            return
        self.live += 1
        if d[:4] != b"PXTS" or len(d) < HEAD + MAC:
            return
        seq = int.from_bytes(d[4:8], "big")
        idx, count, row0, nrows, enc = d[SESSION:HEAD]
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


def strips_of(got):
    return [g for g in got if g[:4] == b"PXTS"]


def udp_ack_without_frame():
    b = register(1)
    s = udp_sock()
    b.hello(s)
    got = collect(s)
    p = Panel(b)
    p.take(got)
    check("a hello with no frame yet gets a signed ack that echoes its nonce",
          len(got) == 1 and got[0][:4] == b"PXTA" and p.live == 1, f"got={[g[:4] for g in got]}")
    s.close()


def udp_hello_auth():
    b = register(2)
    s = udp_sock()
    d = bytearray(b.hello(s))
    collect(s)
    d[6] ^= 1
    s.sendto(bytes(d), ("127.0.0.1", UDP_PORT))
    check("a hello with a bad HMAC gets no answer", collect(s) == [])
    Boot(3, b.token).hello(s)
    check("a hello from a boot that does not hold the control stream gets no answer", collect(s) == [])
    Boot(2, os.urandom(8)).hello(s, n=1000)
    check("a hello without the control stream's token gets no answer", collect(s) == [])
    b.hello(s, n=1)
    check("a hello whose counter does not go up gets no answer", collect(s) == [])
    good = b.hello(s)
    collect(s)
    t = udp_sock()
    t.sendto(good, ("127.0.0.1", UDP_PORT))
    check("a replayed hello from another address gets no answer", collect(t) == [])
    stale = Boot(2, b.token)
    stale.n = b.n
    register(2, b)
    stale.hello(s)
    check("after the control stream reconnects, a hello with the old token gets no answer", collect(s) == [])
    b.hello(s)
    check("the same boot's next hello, with the new token, is answered", len(collect(s)) > 0)
    s.close()
    t.close()


def udp_strips():
    b = register(3)
    s = udp_sock()
    b.hello(s)
    collect(s)
    for name, frame in (("noise", noise()), ("flat", bytes(FRAME))):
        seq = post_frame(frame)
        got = collect(s)
        p = Panel(b)
        p.take(got)
        biggest = max((len(g) for g in got), default=0)
        check(f"a posted {name} frame arrives as signed strips of 1400 bytes or less that rebuild it",
              got and biggest <= 1400 and p.shows and p.shows[-1][:2] == (seq, True)
              and p.shows[-1][2] == frame, f"n={len(got)} max={biggest}")
    s.close()


def udp_loss_order_tamper():
    b = register(4)
    s = udp_sock()
    b.hello(s)
    collect(s)
    frame = noise()
    seq = post_frame(frame)
    got = strips_of(collect(s))

    p = Panel(b)
    p.take(got[:5] + got[6:])
    time.sleep(PARTIAL_S + 0.05)
    p.settle()
    lost = got[5] if len(got) > 5 else bytes(HEAD)
    row0, nrows = lost[22], lost[23]
    keep = bytes(p.fb[:row0 * ROW_BYTES]) + bytes(p.fb[(row0 + nrows) * ROW_BYTES:])
    want = frame[:row0 * ROW_BYTES] + frame[(row0 + nrows) * ROW_BYTES:]
    check("a lost strip costs only its rows: the rest shows after the partial timeout",
          bool(p.shows) and p.shows[-1][:2] == (seq, False) and keep == want)
    b.hello(s, newest=p.cur)
    p.take(collect(s))
    check("the next hello resends the frame and fills the lost strip",
          bool(p.shows) and p.shows[-1][:2] == (seq, True) and bytes(p.fb) == frame)

    p = Panel(b)
    shuffled = list(got)
    random.shuffle(shuffled)
    p.take(shuffled)
    check("strips in any order rebuild the frame", bool(p.shows) and p.shows[-1][1] and bytes(p.fb) == frame)

    p = Panel(b)
    bad = bytearray(got[0] if got else bytes(64))
    bad[30] ^= 0xFF
    p.take([bytes(bad)])
    check("a strip with a bad HMAC is dropped", p.bad == 1 and p.mask == 0 and bytes(p.fb) == bytes(FRAME))

    newer = noise()
    seq2 = post_frame(newer)
    got2 = strips_of(collect(s))
    p = Panel(b)
    p.take(got2)
    p.take(got)
    check("strips of an older frame replayed after a newer one are dropped",
          p.cur == seq2 and bytes(p.fb) == newer and len(p.shows) == 1 and seq2 > seq)
    s.close()


def udp_replay():
    # The replay class: a captured reply or strip must never draw or count as
    # a live link once the panel has moved on, whatever its clocks say.
    b = register(11)
    s = udp_sock()
    post_frame(noise())
    b.hello(s)
    captured = collect(s)

    p = Panel(b)
    p.take(captured)
    check("a reply to a hello still outstanding is live and draws", p.live > 0 and bool(p.shows))

    b.hello(s)
    collect(s)
    p = Panel(b)
    p.take(captured)
    check("a reply that arrives after the next hello is still accepted", p.live > 0 and bool(p.shows))

    for _ in range(NONCES):
        b.hello(s)
        collect(s)
    p = Panel(b)
    p.take(captured)
    check("a captured reply replayed after later hellos is dropped and is not live",
          p.live == 0 and not p.shows and p.dropped == len(captured))

    b.hello(s)
    mine = collect(s)
    p = Panel(b, clock=time.monotonic() + LIVE_S + 1)
    p.take(mine)
    check("a reply to a hello sent more than 10 s ago (monotonic) is not live", p.live == 0)

    after = register(12)
    after.hello(s)
    collect(s)
    p = Panel(after)
    p.take(captured + mine)
    check("a reply from before a reboot is dropped by the new boot", p.live == 0 and not p.shows)
    s.close()


def no_wall_clock():
    # Clock steps, backward or forward, cannot reopen a replay window when no
    # wall clock feeds the checks at all.
    here = os.path.dirname(os.path.abspath(__file__))
    with open(RELAY) as f:
        relay = f.read()
    with open(os.path.join(here, "..", "src", "main.cpp")) as f:
        fw = f.read()
    relay_udp = relay[relay.index("def session("):relay.index("def start_udp(")]
    fw_udp = fw[fw.index("// ---------------------------------------------------------------- UDP frames"):
                fw.index("static const uint32_t STREAM_IDLE_MS")]
    check("the relay's UDP checks read no wall clock", "time.time(" not in relay_udp)
    check("the panel's UDP checks read no wall clock",
          "time(nullptr)" not in fw_udp and "clockReady" not in fw_udp)


def udp_slow_panel():
    # A panel that falls behind must come back to the newest frame, never play
    # out a backlog. The relay sends only the newest frame it holds.
    b = register(5)
    s = udp_sock(rcvbuf=1 << 20)
    b.hello(s)
    collect(s)
    frames = [noise() for _ in range(30)]
    seqs = [post_frame(f) for f in frames]
    time.sleep(1.0)
    backlog = collect(s)
    order = [int.from_bytes(g[4:8], "big") for g in strips_of(backlog)]
    check("the relay never sends an older frame after a newer one and skips superseded frames",
          bool(order) and order == sorted(order) and order[-1] == seqs[-1] and len(set(order)) < len(seqs),
          f"posted={len(seqs)} sent={len(set(order))}")
    p = Panel(b)
    p.take(backlog)
    b.hello(s, newest=p.cur)
    p.take(collect(s))
    check("a slow panel shows the newest frame after it drains, not a backlog",
          bool(p.shows) and p.shows[-1][0] == seqs[-1] and bytes(p.fb) == frames[-1])
    s.close()


def udp_restart_seq():
    # The relay's counter restarts from the clock. A panel that saw a higher
    # seq would drop every new frame as old, so its hello names that seq and
    # the relay moves past it.
    b = register(6)
    s = udp_sock()
    seq = post_frame(noise())
    collect(s)
    b.hello(s, newest=seq + 100000)
    p = Panel(b)
    p.cur = seq + 100000
    p.take(collect(s))
    check("after a relay restart the panel's higher seq is passed, so the current frame shows",
          bool(p.shows) and p.shows[-1][0] > seq + 100000)
    check("frames after that keep counting up from there", post_frame(noise()) > seq + 100000)
    s.close()


def udp_sent_counter():
    # _sent is what the panel says it showed whole, and only for the boot that
    # holds the control stream. A send the link dropped never counts.
    tok = register(777).token
    s = udp_sock()
    Boot(555, tok).hello(s, whole=5)
    collect(s)
    post_frame(noise())
    collect(s)
    dev = req("/device")
    check("a hello from another boot does not touch _sent",
          dev.get("boot") == 777 and dev.get("_sent") == 0 and dev.get("path") == "udp", json.dumps(dev))
    b = Boot(777, tok)
    b.hello(s, whole=2)
    collect(s)
    sent = req("/device").get("_sent")
    check("_sent takes the whole frames the current boot reports in its hello", sent == 2, f"_sent={sent}")
    b.hello(s, whole=1)
    collect(s)
    sent = req("/device").get("_sent")
    check("_sent never goes down", sent == 2, f"_sent={sent}")
    s.close()


def udp_partial_deadline():
    # Frames closer than the partial timeout that each lose a strip must still
    # show: the oldest unshown frame's deadline carries over.
    b = register(10)
    s = udp_sock()
    b.hello(s)
    collect(s)
    p = Panel(b)
    for _ in range(12):
        post_frame(noise())
        for d in collect(s, quiet=0.08):
            if d[:4] == b"PXTS" and d[SESSION] == 3:
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


RUNNING = []


def start_relay(key_file, fw_file):
    proc = subprocess.Popen([sys.executable, RELAY, "--port", str(PORT), "--key-file", key_file,
                             "--firmware", fw_file, "--udp-port", str(UDP_PORT)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    RUNNING.append(proc)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.1)


def stop_relay():
    proc = RUNNING.pop()
    proc.terminate()
    proc.wait()


def udp_relay_restart(key_file, fw_file):
    # A relay restart forgets every hello counter. A hello captured before the
    # restart, from the same boot, must still get nothing: the new control
    # stream hands out a new token.
    b = register(30)
    s = udp_sock()
    for _ in range(3):
        b.hello(s, whole=4)
        collect(s)
    captured = b.hello(s, whole=9)
    collect(s)
    stop_relay()
    start_relay(key_file, fw_file)
    post_frame(noise())
    register(30, b)
    t = udp_sock()
    t.sendto(captured, ("127.0.0.1", UDP_PORT))
    dev = req("/device")
    check("after a relay restart, a hello captured before it gets no answer and leaves _sent alone",
          collect(t) == [] and dev.get("_sent") == 0, json.dumps(dev))
    b.hello(s)
    check("after a relay restart, the panel's next hello is answered", len(collect(s)) > 0)
    s.close()
    t.close()


def main():
    with tempfile.NamedTemporaryFile("w", suffix=".key") as kf, tempfile.NamedTemporaryFile(suffix=".bin") as fwf:
        kf.write(KEY)
        kf.flush()
        fwf.write(FW)
        fwf.flush()
        start_relay(kf.name, fwf.name)
        try:
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
            udp_replay()
            no_wall_clock()
            udp_partial_deadline()
            control_stream()
            old_stream()
            op_too_big()
            udp_relay_restart(kf.name, fwf.name)
        finally:
            while RUNNING:
                stop_relay()
    print("ALL PASS" if all(results) else "SOME FAILED")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
