#!/usr/bin/env python3
"""Loopback checks for pxtrelay.py: start it on 127.0.0.1, talk to it the way
the panel and the tools do, and print one PASS/FAIL line per check.

    python3 server/test_relay.py            # exits 1 if any check fails

Stdlib only. Nothing leaves the machine; the key is random per run.
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

RELAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pxtrelay.py")
PORT = 18979
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


def stream(state):
    """Open a panel-style stream and drain it in the background."""
    s = socket.create_connection(("127.0.0.1", PORT))
    s.sendall((f"GET /stream?since=0 HTTP/1.1\r\nHost: x\r\nX-Api-Key: {KEY}\r\n"
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


def main():
    with tempfile.NamedTemporaryFile("w", suffix=".key") as kf:
        kf.write(KEY)
        kf.flush()
        proc = subprocess.Popen([sys.executable, RELAY, "--port", str(PORT), "--key-file", kf.name],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            device_state()
            sent_counter()
            http11()
        finally:
            proc.terminate()
            proc.wait()
    print("ALL PASS" if all(results) else "SOME FAILED")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
