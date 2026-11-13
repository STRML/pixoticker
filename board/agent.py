#!/usr/bin/env python3
"""Node agent: reports one Mac's GPU, memory and mlx-serve token rates to the board.

    board/agent.py --name studio1 --board http://192.168.86.251:18101 --key-file ~/.pxt_ingest_key

Python 3.9, standard library only, so it runs on a stock macOS box. Every second it
reads the local /metrics.json (when a server answers), turns the token counters
into rates and POSTs one small JSON document. A box with no server still reports
GPU and memory, with null rates.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import re
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

LOOP_S = 1.0
TEMP_POLL_S = 10.0       # temperatures move slowly; `latch sensors` costs about 0.4 s of one core
TEMP_STALE_S = 60.0      # a figure older than this is not reported
TEMP_TIMEOUT_S = 3.0
LATCH = "~/.local/bin/latch"
TP_POLL_S = 5.0   # how often to look for a tensor-parallel worker process
NAMES = re.compile(r"^[a-z0-9._-]{1,16}$")


WORKER = re.compile(r"^\S*mlx-serve\S*\s+worker\b")
COORDINATOR = re.compile(r"--coordinator\s+(\d+)\.(\d+)\.(\d+)\.(\d+):\d+")
NOT_WORKER = {"worker": False, "follows": None}


def tp_role(commands, name=None):
    """Is this box a tensor-parallel worker, and whose? A worker is a process that is `mlx-serve worker ...
    --coordinator 10.254.<ab>.<n>:<port>`: it serves no HTTP, so it has no counters. Only the command line's
    first word counts (an ssh or sh line that carries the same words is a launcher, not a worker). On the
    Thunderbolt mesh the last octet of rank 0's cable address is rank 0's Studio number, which names the node
    whose rates it shares. A line that names this box as its own leader is not a worker's."""
    for line in commands:
        if not WORKER.search(line):
            continue
        m = COORDINATOR.search(line)
        mesh = m and (m.group(1), m.group(2)) == ("10", "254")
        follows = f"studio{m.group(4)}" if mesh else None
        if follows is not None and follows == name:
            continue
        return {"worker": True, "follows": follows}
    return dict(NOT_WORKER)


def process_commands():
    """Every process's command line, or None when ps could not be read: no answer is not a "no"."""
    try:
        out = subprocess.run(["ps", "-Ao", "command"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.splitlines() if out.returncode == 0 else None


class TpWatch:
    """The box's tensor-parallel role, looked up at most every TP_POLL_S seconds. A box with its own
    server is never a worker; a ps that fails keeps the last answer."""

    def __init__(self, name):
        self.name, self.role, self.next = name, dict(NOT_WORKER), 0.0

    def check(self, now, serving, commands=process_commands):
        if serving:
            return dict(NOT_WORKER)
        if now >= self.next:
            self.next = now + TP_POLL_S
            lines = commands()
            if lines is not None:
                self.role = tp_role(lines, self.name)
        return self.role


def read_key(path):
    with open(os.path.expanduser(path)) as f:
        key = f.read().strip()
    if len(key) < 16:
        raise SystemExit(f"key in {path} is shorter than 16 characters")
    return key


def gpu_percent():
    """System-wide GPU busy %, from the accelerator's own counter; no sudo, ~20 ms."""
    try:
        out = subprocess.run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    hit = re.search(r'"Device Utilization %"\s*=\s*(\d+)', out)
    return int(hit.group(1)) if hit else None


WATTS_KEY = "PD0R"       # the SMC's whole-board power; PSTR reads 0 on a Studio
WATTS_MAX = 1000.0       # no Mac draws more; anything above is not a reading


class SmcVers(ctypes.Structure):
    _fields_ = [("major", ctypes.c_char), ("minor", ctypes.c_char), ("build", ctypes.c_char),
                ("reserved", ctypes.c_char), ("release", ctypes.c_ushort)]


class SmcPLimit(ctypes.Structure):
    _fields_ = [("version", ctypes.c_ushort), ("length", ctypes.c_ushort), ("cpu", ctypes.c_uint),
                ("gpu", ctypes.c_uint), ("mem", ctypes.c_uint)]


class SmcKeyInfo(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint), ("type", ctypes.c_uint), ("attr", ctypes.c_char)]


class SmcKeyData(ctypes.Structure):
    """AppleSMC's call structure, 80 bytes; the layout is the kernel's (see tools/apple-power/smc.h in local-llm)."""
    _fields_ = [("key", ctypes.c_uint), ("vers", SmcVers), ("plimit", SmcPLimit), ("info", SmcKeyInfo),
                ("result", ctypes.c_ubyte), ("status", ctypes.c_ubyte), ("data8", ctypes.c_ubyte),
                ("data32", ctypes.c_uint), ("bytes", ctypes.c_ubyte * 32)]


def smc_float(type_code, size, raw):
    """A little-endian float from an SMC key; nothing else is decoded, and NaN or infinity is no reading."""
    if type_code != "flt " or size != 4 or len(raw) < 4:
        return None
    value = struct.unpack("<f", bytes(raw[:4]))[0]
    return value if math.isfinite(value) else None


def load_iokit():
    iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
    iokit.IOServiceMatching.restype = ctypes.c_void_p
    iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
    iokit.IOServiceGetMatchingServices.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    iokit.IOIteratorNext.argtypes = [ctypes.c_uint]
    iokit.IOIteratorNext.restype = ctypes.c_uint
    iokit.IOServiceOpen.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
    iokit.IOServiceClose.argtypes = [ctypes.c_uint]
    iokit.IOObjectRelease.argtypes = [ctypes.c_uint]
    iokit.IOConnectCallStructMethod.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t,
                                               ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
    return iokit


class Smc:
    """AppleSMC through IOKit, as a normal user (no sudo). Opened on first use and again after a failure; every
    handle it takes (the iterator, the service, the connection) is released the moment it is no longer needed."""

    READ_KEYINFO, READ_BYTES, KERNEL_INDEX = 9, 5, 2

    def __init__(self, load=load_iokit):
        self.load, self.iokit, self.conn = load, None, None

    def _open(self):
        iokit = self.load()
        it = ctypes.c_uint()
        if iokit.IOServiceGetMatchingServices(0, iokit.IOServiceMatching(b"AppleSMC"), ctypes.byref(it)):
            return
        device = iokit.IOIteratorNext(it.value)
        iokit.IOObjectRelease(it.value)
        if not device:
            return
        conn = ctypes.c_uint()
        task = ctypes.c_uint.in_dll(ctypes.CDLL(None), "mach_task_self_").value
        status = iokit.IOServiceOpen(device, task, 0, ctypes.byref(conn))
        iokit.IOObjectRelease(device)
        if not status:
            self.iokit, self.conn = iokit, conn.value

    def close(self):
        if self.conn is not None:
            self.iokit.IOServiceClose(self.conn)
        self.conn = None

    def _call(self, request):
        out, size = SmcKeyData(), ctypes.c_size_t(ctypes.sizeof(SmcKeyData))
        rc = self.iokit.IOConnectCallStructMethod(self.conn, self.KERNEL_INDEX, ctypes.byref(request),
                                                  ctypes.sizeof(SmcKeyData), ctypes.byref(out), ctypes.byref(size))
        return out if rc == 0 and out.result == 0 else None

    def read(self, key):
        """(type, size, bytes) of one key, or None. A failed call closes the connection; the next read reopens it."""
        if self.conn is None:
            self._open()
        if self.conn is None:
            return None
        code = int.from_bytes(key.encode(), "big")
        info = self._call(SmcKeyData(key=code, data8=self.READ_KEYINFO))
        if info is None:
            self.close()
            return None
        ask = SmcKeyData(key=code, data8=self.READ_BYTES)
        ask.info.size = info.info.size
        data = self._call(ask)
        if data is None:
            self.close()
            return None
        return info.info.type.to_bytes(4, "big").decode("latin-1"), info.info.size, bytes(data.bytes)


_SMC = Smc()


def smc_read_float(key):
    try:
        found = _SMC.read(key)
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError):
        return None
    return smc_float(*found) if found else None


def read_watts(read=None):
    """The whole machine's watts, or None when the SMC is unreadable or the figure is not credible."""
    value = (read or smc_read_float)(WATTS_KEY)
    return float(value) if value is not None and 0 < value <= WATTS_MAX else None


_HOST = []


def host_port():
    """The kernel's host port, taken once: every mach_host_self() call adds a send right nothing releases."""
    if not _HOST:
        libc = ctypes.CDLL(None)
        libc.mach_host_self.restype = ctypes.c_uint
        _HOST.append((libc, libc.mach_host_self()))
    return _HOST[0]


def cpu_ticks():
    """(user, system, idle, nice) tick counters from the kernel; None when it cannot be read."""
    try:
        libc, host = host_port()
        buf, count = (ctypes.c_uint * 4)(), ctypes.c_uint(4)
        if libc.host_statistics(host, 3, buf, ctypes.byref(count)):   # HOST_CPU_LOAD_INFO
            return None
        return tuple(buf)
    except (OSError, AttributeError):
        return None


class CpuLoad:
    """CPU busy percent between two successive calls; the first call, or the first after a failed read, has none."""

    def __init__(self, reader=None):
        self.reader, self.prev = reader or cpu_ticks, None

    def percent(self):
        cur = self.reader()
        prev, self.prev = self.prev, cur
        if cur is None or prev is None:
            return None
        user, system, idle, nice = ((c - p) % 2 ** 32 for c, p in zip(cur, prev))
        busy = user + system + nice
        total = busy + idle
        return round(100.0 * busy / total, 1) if total else None


GPU_TEMP = re.compile(r"GPU\s+(\d+(?:\.\d+)?)\s*\u00b0C")


def parse_gpu_temp(text):
    """The GPU's degrees C from `latch sensors` ("CPU 47.3\u00b0C \u00b7 GPU 45.9\u00b0C"), or None when the line is
    missing or outside 10 to 130, which no working GPU reads."""
    hit = GPU_TEMP.search(text or "")
    value = float(hit.group(1)) if hit else None
    return value if value is not None and 10.0 <= value <= 130.0 else None


def read_gpu_temp(latch=LATCH):
    """Run `latch sensors` (no sudo; Latch is installed on every Studio); None when it is missing, slow or fails."""
    try:
        out = subprocess.run([os.path.expanduser(latch), "sensors"], capture_output=True, text=True,
                             timeout=TEMP_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_gpu_temp(out.stdout) if out.returncode == 0 else None


class TempWatch:
    """The GPU temperature, read at most every TEMP_POLL_S seconds. A failed read keeps the last figure
    until it is TEMP_STALE_S old, so one slow `latch sensors` does not blank the panel."""

    def __init__(self, reader=None):
        self.reader, self.value, self.at, self.next = reader or read_gpu_temp, None, None, 0.0

    def check(self, now):
        if now >= self.next:
            self.next = now + TEMP_POLL_S
            value = self.reader()
            if value is not None:
                self.value, self.at = value, now
        return self.value if self.at is not None and now - self.at <= TEMP_STALE_S else None


def memory():
    """(used GiB, total GiB). Used is total minus free, inactive, speculative and
    purgeable pages, which is what Activity Monitor calls Memory Used."""
    try:
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                   text=True, timeout=5).stdout)
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, None
    size = re.search(r"page size of (\d+)", out)
    if not size:
        return None, None
    page = int(size.group(1))
    pages = {k: int(v) for k, v in re.findall(r"Pages ([a-z ]+):\s+(\d+)\.", out)}
    spare = sum(pages.get(k, 0) for k in ("free", "inactive", "speculative", "purgeable"))
    return (total - spare * page) / 2 ** 30, total / 2 ** 30


WINDOW_S = 8.0


class Rates:
    """Token rates from successive /metrics.json reads.

    live: the server's counters move in lumps (a prefill chunk, a round of drafted
    tokens), so a one-second delta flickers between 0 and double the true rate. The
    rate is the growth over the last WINDOW_S seconds. generation_tokens_live is
    cumulative; prefill_tokens_live is progress inside one request and resets, so
    only its increases count. With nothing running the live rates are 0 at once.
    last: when a request finished in the interval, its average (tokens over the
    phase's own seconds), and how long ago that was (*_last_age). The agent keeps
    reporting both until the next request replaces them; the board shows an average
    dim until its age passes LAST_S. The age is relative, so the agent's and the
    board's clocks need not agree.
    """

    def __init__(self):
        self.prev = None
        self.window = []
        self.last_dec = self.last_pre = None
        self.dec_at = self.pre_at = None

    def update(self, m, now):
        if m is None:
            self.prev, self.window = None, []
            return {"dec": None, "pre": None, **self._last(now), "running": None, "serving": False}
        g, c, h = m.get("gauges") or {}, m.get("counters") or {}, m.get("histograms") or {}
        cur = {"t": now, "start": g.get("process_start_time_seconds"),
               "gen": g.get("generation_tokens_live") or 0, "pl": g.get("prefill_tokens_live") or 0,
               "gen_total": c.get("generation_tokens_total") or 0,
               "pre_total": c.get("prefill_tokens_total") or 0,
               "dec_s": (h.get("decode_time_seconds") or {}).get("sum") or 0,
               "pre_s": (h.get("prefill_time_seconds") or {}).get("sum") or 0}
        prev, self.prev = self.prev, cur
        if prev and prev["start"] != cur["start"]:
            prev, self.window = None, []
        running = (g.get("requests_running") or 0) + (g.get("requests_prefilling") or 0)
        self.window = [s for s in self.window + [cur] if now - s["t"] <= WINDOW_S] if running else []
        if prev:
            dec = self._average(cur, prev, "gen_total", "dec_s")
            pre = self._average(cur, prev, "pre_total", "pre_s")
            if dec:
                self.last_dec, self.dec_at = dec, now
            if pre:
                self.last_pre, self.pre_at = pre, now
        prefilling = g.get("requests_prefilling")
        pre = 0.0 if prefilling == 0 else self._rate("pl")   # the window averages 8 s; nothing prefilling is 0 at once
        return {"dec": self._rate("gen"), "pre": pre, **self._last(now),
                "running": g.get("requests_running"), "serving": True}

    def _last(self, now):
        def age(at):
            return None if at is None else round(max(0.0, now - at), 1)
        return {"dec_last": self.last_dec, "pre_last": self.last_pre,
                "dec_last_age": age(self.dec_at), "pre_last_age": age(self.pre_at)}

    def _rate(self, key):
        w = self.window
        span = w[-1]["t"] - w[0]["t"] if len(w) > 1 else 0
        if span <= 0:
            return 0.0
        grown = sum(max(0, b[key] - a[key]) for a, b in zip(w, w[1:]))
        return round(grown / span, 1)

    @staticmethod
    def _average(cur, prev, tokens, seconds):
        dt, dn = cur[seconds] - prev[seconds], cur[tokens] - prev[tokens]
        return round(dn / dt, 1) if dt > 0 and dn > 0 else None


REFUSAL_LOG_S = 60.0


class RefusalLog:
    """Names a server's error answer in the log at most once a minute per status. A router that alternates
    between two groups, one of them refusing, would otherwise print every second."""

    def __init__(self, out=None):
        self.out = out or (lambda line: print(line, file=sys.stderr, flush=True))
        self.code, self.next = None, 0.0

    def note(self, now, err):
        if err.code == self.code and now < self.next:
            return
        self.code, self.next = err.code, now + REFUSAL_LOG_S
        self.out(f"metrics refused: HTTP {err.code} from {err.filename}")


def fetch_metrics(url, timeout=2.0, refused=None):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as err:
        if refused:
            refused(err)
        return None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def report(name, rates, used, total, gpu):
    body = {"node": name, "gpu": gpu, "mem_gb": None if used is None else round(used, 1),
            "mem_total_gb": None if total is None else round(total), "t": int(time.time())}
    body.update(rates)
    return body


def post(board, key, body):
    req = urllib.request.Request(board.rstrip("/") + "/ingest", data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="node label on the board, e.g. studio1")
    ap.add_argument("--board", required=True, help="board base URL")
    ap.add_argument("--key-file", default="~/.pxt_ingest_key")
    ap.add_argument("--metrics", default="http://127.0.0.1:11234/metrics.json")
    args = ap.parse_args()
    if not NAMES.match(args.name):
        raise SystemExit("--name must be 1-16 characters of a-z 0-9 . _ -")
    key = read_key(args.key_file)
    rates = Rates()
    watch = TpWatch(args.name)
    temps, cpu = TempWatch(), CpuLoad()
    failing = False
    refusals = RefusalLog()
    while True:
        started = time.time()
        try:
            used, total = memory()
            fetched = fetch_metrics(args.metrics, refused=lambda err: refusals.note(started, err))
            measured = rates.update(fetched, started)
            body = report(args.name, measured, used, total, gpu_percent())
            body.update(watch.check(started, measured["serving"]))
            body["temp"] = temps.check(started)
            body["watts"] = read_watts()
            body["cpu"] = cpu.percent()
            post(args.board, key, body)
            if failing:
                print("board reachable again", flush=True)
            failing = False
        except Exception as err:
            if not failing:
                print(f"report failed: {err!r}", file=sys.stderr, flush=True)
            failing = True
        time.sleep(max(0.0, LOOP_S - (time.time() - started)))


if __name__ == "__main__":
    main()
