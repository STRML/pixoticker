#!/usr/bin/env python3
"""Claude Code usage (ccusage) on the PixoTicker panel.

  tools/ccusage.py 192.168.4.1            # one frame
  tools/ccusage.py 192.168.4.1 --watch 300
  tools/ccusage.py --relay https://relay.example.com/pxt --watch 300

Layout, 128x64: today's cost in large digits, seven daily cost bars, tokens
today. Costs and tokens come from `ccusage daily --json`; the panel only shows
pixels. The key comes from PXT_RELAY_KEY, for the panel and the relay alike.
"""
import argparse
import base64
import datetime
import os
import json
import subprocess
import sys
import time
import urllib.request

W, H = 128, 64

# 5x7 glyphs, rows top to bottom, 5 bits per row
FONT = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "$": ["00100", "01111", "10100", "01110", "00101", "11110", "00100"],
    ".": ["00000", "00000", "00000", "00000", "00000", "00110", "00110"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
}

COL_DIM = (60, 60, 60)
COL_COST = (255, 200, 40)
COL_BAR = (60, 170, 255)
COL_TODAY = (255, 255, 255)
COL_TEXT = (150, 150, 150)


def rgb565(r, g, b):
    # panel wiring swaps G and B
    return ((r & 0xF8) << 8) | ((b & 0xFC) << 3) | (g >> 3)


class Canvas:
    def __init__(self):
        self.px = [rgb565(0, 0, 0)] * (W * H)

    def set(self, x, y, col):
        if 0 <= x < W and 0 <= y < H:
            self.px[y * W + x] = rgb565(*col)

    def rect(self, x, y, w, h, col):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.set(xx, yy, col)

    def text(self, x, y, s, col, scale=1):
        cx = x
        for ch in s:
            g = FONT.get(ch)
            if not g:
                cx += 6 * scale
                continue
            for ry, row in enumerate(g):
                for rx, bit in enumerate(row):
                    if bit == "1":
                        self.rect(cx + rx * scale, y + ry * scale, scale, scale, col)
            cx += 6 * scale
        return cx


def run_ccusage():
    out = subprocess.run(
        ["ccusage", "daily", "--json"], capture_output=True, text=True, timeout=180
    )
    if out.returncode != 0:
        sys.exit("ccusage failed: " + out.stderr.strip()[:200])
    return json.loads(out.stdout)


def human_tokens(n):
    if n >= 1_000_000_000:
        return f"{n / 1e9:.1f}B".replace(".0", "")
    if n >= 1_000_000:
        return f"{n / 1e6:.0f}M"
    if n >= 1_000:
        return f"{n / 1e3:.0f}K"
    return str(n)


def human_cost(v):
    if v >= 1000:
        return f"${v / 1000:.1f}K"
    return f"${v:.2f}"


def render(days):
    c = Canvas()
    today = max(days, key=lambda d: d["period"])
    last7 = sorted(days, key=lambda d: d["period"])[-7:]

    # today's cost, scale 3 (5+1 glyph columns * 3 = 18 px per char)
    big = human_cost(today["totalCost"])
    c.text(4, 2, big, COL_COST, scale=3)

    # tokens today, small, right aligned on the same top band
    tok = human_tokens(today["totalTokens"])
    c.text(W - 4 - len(tok) * 6, 24, tok, COL_TEXT, scale=1)

    # seven daily cost bars
    peak = max((d["totalCost"] for d in last7), default=1) or 1
    base = H - 6
    bar_w, gap = 14, 4
    x0 = (W - (bar_w * len(last7) + gap * (len(last7) - 1))) // 2
    for i, d in enumerate(last7):
        h = max(1, round((d["totalCost"] / peak) * 22))
        x = x0 + i * (bar_w + gap)
        col = COL_TODAY if d is today else COL_BAR
        c.rect(x, base - h, bar_w, h, col)

    # baseline
    c.rect(0, base + 1, W, 1, COL_DIM)
    return c.px


def push(host, px, relay=None):
    body = bytearray()
    for v in px:
        body += bytes((v >> 8, v & 0xFF))
    # the device takes base64; the relay stores raw bytes
    payload = bytes(body) if relay else base64.b64encode(bytes(body))
    url = f"{relay}/frame?fmt=565" if relay else f"http://{host}/frame?fmt=rgb565"
    headers = {"Content-Type": "text/plain", "X-Api-Key": os.environ.get("PXT_RELAY_KEY", "")}
    req = urllib.request.Request(url, data=payload, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host", nargs="?", default="192.168.4.1")
    ap.add_argument("--watch", type=int, default=0, help="seconds between refreshes")
    ap.add_argument("--relay", help="relay base URL; push through it instead of direct")
    args = ap.parse_args()

    while True:
        data = run_ccusage()
        days = [d for d in data["daily"] if d["period"]]
        px = render(days)
        stamp = datetime.datetime.now().strftime("%H:%M")
        print(f"{stamp} pushed {push(args.host, px, args.relay)}")
        if not args.watch:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
