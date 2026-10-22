#!/usr/bin/env python3
"""Loopback checks for tools/publish_fw.sh: a real relay, a fake panel, stub
pio/ssh/scp, and one run of the script per scenario (about 40 s, in parallel).

    python3 tools/test_publish_fw.py [scenario ...]   # good rollback crashloop blip

Copies the script into a temp tree, stubs pio/ssh/scp, runs the real relay with
HOME pointed at the temp tree, and plays a panel that takes the ota op and then
behaves per scenario. Prints the script's exit code and last line per scenario.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENARIOS = sys.argv[1:] or ["good", "rollback", "crashloop", "blip"]
WANT = {"good": 0, "rollback": 3, "crashloop": 3, "blip": 0}
KEY = os.urandom(16).hex()

STUB_PIO = """#!/bin/sh
mkdir -p .pio/build/esp32dev
printf 'FIRMWARE %s %s' "$PXT_RELAY_KEY" "$$" > .pio/build/esp32dev/firmware.bin
"""
STUB_SCP = """#!/bin/sh
# scp -q SRC HOST:firmware/pxt.bin -> the relay's FW_PATH under the fake HOME
mkdir -p "$FAKE_HOME/firmware"
cp "$2" "$FAKE_HOME/firmware/pxt.bin"
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


def run(scenario, port):
    t = tempfile.mkdtemp(prefix="pubfw-")
    try:
        os.makedirs(f"{t}/tools")
        os.makedirs(f"{t}/bin")
        shutil.copy(f"{ROOT}/tools/publish_fw.sh", f"{t}/tools/publish_fw.sh")
        for name, body in (("pio", STUB_PIO), ("scp", STUB_SCP), ("ssh", "#!/bin/sh\n")):
            with open(f"{t}/bin/{name}", "w") as f:
                f.write(body)
            os.chmod(f"{t}/bin/{name}", 0o755)
        with open(f"{t}/relay.key", "w") as f:
            f.write(KEY)
        env = dict(os.environ, HOME=t, FAKE_HOME=t)
        relay = subprocess.Popen([sys.executable, f"{ROOT}/server/pxtrelay.py", "--port", str(port),
                                  "--key-file", f"{t}/relay.key"], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.8)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/frame", data=bytes(16384),
                                     headers={"X-Api-Key": KEY})
        urllib.request.urlopen(req, timeout=5).read()
        panel = Panel(port, scenario)
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
