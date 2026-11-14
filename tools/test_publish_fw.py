#!/usr/bin/env python3
"""Loopback checks for tools/publish_fw.sh: a real relay, a fake panel, stub
pio/ssh, and one run of the script per scenario (about 40 s, in parallel).

    python3 tools/test_publish_fw.py [scenario ...]   # good rollback crashloop blip udp

Copies the script into a temp tree, stubs pio/ssh, runs the real relay with
HOME pointed at the temp tree, and plays a panel that takes the ota op and then
behaves per scenario. Prints the script's exit code and last line per scenario.
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import struct
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENARIOS = sys.argv[1:] or ["good", "rollback", "crashloop", "blip", "udp"]
WANT = {"good": 0, "rollback": 3, "crashloop": 3, "blip": 0, "udp": 0}
# src/pxt_rules.h otaIsCurrent: an op for the image waiting for its first
# whole frame is skipped, like one for the confirmed image.
OTA_SKIPS_PENDING = True
KEY = os.urandom(16).hex()

STUB_PIO = """#!/bin/sh
mkdir -p .pio/build/esp32dev
printf 'FIRMWARE %s %s' "$PXT_RELAY_KEY" "$$" > .pio/build/esp32dev/firmware.bin
"""
STUB_SSH = """#!/bin/sh
# ssh HOST 'mkdir -p DIR && cat > PATH' < image -> the relay's FW_PATH under the fake HOME
mkdir -p "$FAKE_HOME/firmware"
cat > "$FAKE_HOME/firmware/pxt.bin.new" && mv "$FAKE_HOME/firmware/pxt.bin.new" "$FAKE_HOME/firmware/pxt.bin"
"""


class Panel(threading.Thread):
    """Old build on a stream; on an ota op it 'reboots' into the new build,
    then follows the scenario: stay, roll back, or crash and restart."""

    def __init__(self, port, scenario):
        super().__init__(daemon=True)
        self.port, self.scenario = port, scenario
        self.sock = None
        self.done = False

    def connect(self, build, uptime):
        if self.sock:
            self.sock.close()
        s = socket.create_connection(("127.0.0.1", self.port))
        state = json.dumps({"build": build, "uptime": uptime, "frames": 0, "tls": "verified"})
        s.sendall((f"GET /stream?since=0 HTTP/1.1\r\nHost: x\r\nX-Api-Key: {KEY}\r\n"
                   f"X-Pxt-State: {state}\r\nConnection: close\r\n\r\n").encode())
        s.settimeout(0.5)
        self.sock = s

    def run(self):
        self.connect("old", 1000)
        buf = b""
        while not self.done:
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            buf += data
            if b'"type":"ota"' not in buf:
                continue
            # took the op: reboot into the new image
            time.sleep(1)
            self.connect("new", 5)
            time.sleep(15)
            if self.scenario == "rollback":
                self.connect("old", 4)      # bootloader put the old image back
            elif self.scenario == "crashloop":
                self.connect("new", 3)      # same image, fresh boot
            elif self.scenario == "blip":
                self.connect("new", 20)     # same boot, uptime kept counting
            while not self.done:            # ignore any further ops
                try:
                    self.sock.recv(65536)
                except socket.timeout:
                    pass
                except OSError:
                    return
            return


class UdpPanel(threading.Thread):
    """A panel on the new firmware: a frames=0 control stream, frames by UDP,
    and whole frames reported in the hello. After the ota op it reboots into
    the new build, which takes 15 s to show its first whole frame, so
    publish_fw.sh re-posts the op meanwhile. The op must not reinstall."""

    def __init__(self, port, udp_port):
        super().__init__(daemon=True)
        self.port, self.udp_port = port, udp_port
        self.sock = None
        self.done = False
        self.installs = 0

    def connect(self, build, uptime):
        if self.sock:
            self.sock.close()
        self.boot = int.from_bytes(os.urandom(4), "big")
        self.n = 0
        self.whole = 0
        state = json.dumps({"build": build, "uptime": uptime, "frames": 0, "tls": "verified",
                            "boot": self.boot})
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall((f"GET /stream?since=0&frames=0 HTTP/1.1\r\nHost: x\r\nX-Api-Key: {KEY}\r\n"
                   f"X-Pxt-State: {state}\r\nConnection: close\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += s.recv(4096)
        head, self.buf = head.split(b"\r\n\r\n", 1)
        self.token = next(bytes.fromhex(line.split(b":", 1)[1].strip().decode())
                          for line in head.split(b"\r\n") if line.lower().startswith(b"x-pxt-udp:"))
        s.settimeout(0.2)
        self.sock = s

    def hello(self, u):
        self.n += 1
        d = b"PXTH" + os.urandom(8) + self.token + struct.pack(">IIII", self.boot, self.n, 0, self.whole)
        u.sendto(d + hmac.new(KEY.encode(), d, hashlib.sha256).digest()[:16], ("127.0.0.1", self.udp_port))

    def run(self):
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.bind(("127.0.0.1", 0))
        u.settimeout(0.05)
        installed, pending, draw_at = "", "", 0.0
        self.connect("old", 1000)
        self.whole = 1
        said = 0.0
        while not self.done:
            if time.time() - said > 1:
                self.hello(u)
                said = time.time()
            try:
                if u.recv(2048)[:4] == b"PXTS" and time.time() >= draw_at:
                    self.whole += 1
                    if pending:
                        installed, pending = pending, ""
            except socket.timeout:
                pass
            try:
                self.buf += self.sock.recv(65536)
            except socket.timeout:
                pass
            except OSError:
                return
            m = re.search(rb'"type":"ota"[^}]*"sha256":"([0-9a-f]+)"', self.buf)
            if not m:
                continue
            self.buf = b""
            sha = m.group(1).decode()
            if sha == installed or (OTA_SKIPS_PENDING and sha == pending):
                continue
            self.installs += 1
            pending = sha
            time.sleep(1)
            self.connect("new", 5)
            draw_at = time.time() + 15


def run(scenario, port):
    t = tempfile.mkdtemp(prefix="pubfw-")
    try:
        os.makedirs(f"{t}/tools")
        os.makedirs(f"{t}/bin")
        shutil.copy(f"{ROOT}/tools/publish_fw.sh", f"{t}/tools/publish_fw.sh")
        for name, body in (("pio", STUB_PIO), ("ssh", STUB_SSH)):
            with open(f"{t}/bin/{name}", "w") as f:
                f.write(body)
            os.chmod(f"{t}/bin/{name}", 0o755)
        with open(f"{t}/relay.key", "w") as f:
            f.write(KEY)
        env = dict(os.environ, HOME=t, FAKE_HOME=t)
        udp_port = port + 100 if scenario == "udp" else 0
        relay = subprocess.Popen([sys.executable, f"{ROOT}/server/pxtrelay.py", "--port", str(port),
                                  "--key-file", f"{t}/relay.key", "--udp-port", str(udp_port)], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.8)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/frame", data=bytes(16384),
                                     headers={"X-Api-Key": KEY})
        urllib.request.urlopen(req, timeout=5).read()
        panel = UdpPanel(port, udp_port) if scenario == "udp" else Panel(port, scenario)
        panel.start()
        time.sleep(0.5)
        env.update(PATH=f"{t}/bin:" + os.environ["PATH"], PXT_RELAY_URL=f"http://127.0.0.1:{port}",
                   PXT_RELAY_SSH="fake", PXT_RELAY_KEY=KEY, PXT_RELAY_HOST="relay.test",
                   PXT_SETTLE="20")
        p = subprocess.run(["bash", f"{t}/tools/publish_fw.sh"], env=env,
                           capture_output=True, text=True, timeout=400)
        panel.done = True
        relay.terminate()
        last = (p.stdout.strip().splitlines() or [""])[-1]
        ok = p.returncode == WANT[scenario]
        if scenario == "udp":
            last += f" (installs={panel.installs})"
            ok = ok and panel.installs == 1
        print(f"{'PASS' if ok else 'FAIL'}  {scenario:9} exit={p.returncode} want={WANT[scenario]}  {last}")
        return ok
    finally:
        shutil.rmtree(t, ignore_errors=True)


results = []
threads = []
for i, sc in enumerate(SCENARIOS):
    th = threading.Thread(target=lambda sc=sc, i=i: results.append(run(sc, 18990 + i)))
    th.start()
    threads.append(th)
for th in threads:
    th.join()
ok = all(results) and len(results) == len(SCENARIOS)
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
