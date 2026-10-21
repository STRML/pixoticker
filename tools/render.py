#!/usr/bin/env python3
"""Server-side 2x2 chart renderer: Yahoo closes -> RGB565 frame -> POST /frame.

  tools/render.py 192.168.4.1                       # one frame
  tools/render.py 192.168.4.1 --watch 60            # refresh every 60 s
  tools/render.py 192.168.4.1 --symbols BTC-USD ETH-USD NVDA '^GSPC'

Charts are drawn here, so the ESP32 only needs to receive pixels.
"""
import argparse
import base64
import json
import time
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) PixoTicker/1.0"


def fetch_closes(symbol, interval, rng):
    q = urllib.parse.urlencode({"interval": interval, "range": rng})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    res = data["chart"]["result"][0]
    closes = res["indicators"]["quote"][0]["close"]
    return [c for c in closes if c is not None]


def rgb565(r, g, b):
    # panel wiring swaps G and B
    return ((r & 0xF8) << 8) | ((b & 0xFC) << 3) | (g >> 3)


def draw_line(px, w, h, x0, y0, x1, y1, color):
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        if 0 <= x0 < w and 0 <= y0 < h:
            px[y0 * w + x0] = color
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def quad(px, w, h, x0, y0, qw, qh, series):
    border = rgb565(40, 40, 40)
    for x in range(x0, x0 + qw):
        px[y0 * w + x] = border
        px[(y0 + qh - 1) * w + x] = border
    for y in range(y0, y0 + qh):
        px[y * w + x0] = border
        px[y * w + x0 + qw - 1] = border
    if len(series) < 2:
        return
    lo, hi = min(series), max(series)
    span = (hi - lo) or 1.0
    up = series[-1] >= series[0]
    color = rgb565(0, 220, 120) if up else rgb565(255, 70, 70)
    pad = 3
    iw, ih = qw - 2 * pad, qh - 2 * pad
    pts = []
    n = len(series)
    for i, v in enumerate(series):
        x = x0 + pad + round(i * (iw - 1) / (n - 1))
        y = y0 + qh - 1 - pad - round((v - lo) / span * (ih - 1))
        pts.append((x, y))
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        draw_line(px, w, h, ax, ay, bx, by, color)


def render(w, h, symbols, interval, rng):
    px = [0] * (w * h)
    qw, qh = w // 2, h // 2
    for i, sym in enumerate(symbols[:4]):
        try:
            series = fetch_closes(sym, interval, rng)
        except Exception as e:  # a dead symbol must not blank the others
            print(f"{sym}: {e}")
            series = []
        quad(px, w, h, (i % 2) * qw, (i // 2) * qh, qw, qh, series)
    return px


def push(host, px):
    body = bytearray()
    for c in px:
        body += bytes(((c >> 8) & 0xFF, c & 0xFF))
    # base64: a raw RGB565 body is NUL terminated by the ESP32 web server
    payload = base64.b64encode(bytes(body))
    req = urllib.request.Request(
        f"http://{host}/frame?fmt=rgb565", data=payload, method="POST",
        headers={"Content-Type": "text/plain"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host", nargs="?", default="192.168.4.1")
    ap.add_argument("--symbols", nargs="+", default=["BTC-USD", "ETH-USD", "NVDA", "^GSPC"])
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--range", default="1d")
    ap.add_argument("--watch", type=int, default=0)
    args = ap.parse_args()

    with urllib.request.urlopen(f"http://{args.host}/state", timeout=10) as r:
        st = json.load(r)
    w, h = st["width"], st["height"]
    print(f"panel {w}x{h} at {args.host}, panel_active={st['panel']}")

    while True:
        px = render(w, h, args.symbols, args.interval, args.range)
        print(push(args.host, px))
        if not args.watch:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
