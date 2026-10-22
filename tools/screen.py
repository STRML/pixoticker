#!/usr/bin/env python3
"""A shared 128x64 screen layout for renderers: a chart over a text band.

    y  0..28   chart: line with a gradient fill underneath
    y 29..63   text: five 7 px lines of the 4x6 font, 31 characters each

Draw through Screen so the bands never fight over rows; the frame that leaves
here is always 128x64 RGB565 with the panel's G/B swap applied.

    tools/screen.py --out preview.png    # render the demo frame to a PNG

Requires pillow (pip install pillow). The key comes from PXT_RELAY_KEY.
"""
import argparse
import base64
import json
import math
import os
import time
import http.client
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw

import font46 as font

W, H = 128, 64
LINE_H = font.H + 1
CHAT_LINES = 5  # the author line and four lines of body
CHAT_TOP = H - CHAT_LINES * LINE_H
CHART_H = CHAT_TOP
COLS = (W - 2) // font.ADVANCE  # 31 characters per line

BG = (0, 0, 0)
FG = (235, 235, 235)
DIM = (110, 110, 110)
UP = (0, 220, 120)
DOWN = (255, 70, 70)
RULE = (28, 28, 28)


def rgb565(r, g, b):
    # the panel's wiring swaps G and B
    return ((r & 0xF8) << 8) | ((b & 0xFC) << 3) | (g >> 3)


def pack(img):
    """RGB image -> RGB565 bytes, big endian, the order the relay expects."""
    out = bytearray(W * H * 2)
    px = img.load()
    i = 0
    for y in range(H):
        for x in range(W):
            r, g, b = px[x, y][:3]
            v = rgb565(r, g, b)
            out[i] = v >> 8
            out[i + 1] = v & 0xFF
            i += 2
    return bytes(out)


class Screen:
    def __init__(self):
        self.img = Image.new("RGB", (W, H), BG)
        self.d = ImageDraw.Draw(self.img)
        self.cols = COLS

    def price(self, symbol, value, change):
        """Header row: symbol and change on the left, the number on the right."""
        pct = f"{change:+.1f}%"
        color = UP if change >= 0 else DOWN
        font.draw(self.d, (1, 0), symbol, FG)
        font.draw(self.d, (1 + font.width(symbol + " "), 0), pct, color)
        num = f"${value:,.2f}"
        font.draw(self.d, (W - 1 - font.width(num), 0), num, FG)

    def series(self, values, color=None, glow=True, top=2):
        """Chart band. The last point decides the colour, so a rising series
        reads green even when it started lower than it ended."""
        n = len(values)
        if n < 2:
            return
        lo, hi = min(values), max(values)
        span = (hi - lo) or 1.0
        bottom = CHART_H - 3
        color = color or (UP if values[-1] >= values[0] else DOWN)

        pts = []
        for i, v in enumerate(values):
            x = round(i * (W - 1) / (n - 1))
            y = round(bottom - (v - lo) / span * (bottom - top))
            pts.append((x, y))

        if glow:
            # Depth under the trace: a vertical ramp from the line colour down
            # to black. Drawn per column so it follows the line, not the box.
            for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[-1:]):
                for x in range(x0, max(x0 + 1, x1)):
                    t = (x - x0) / max(1, x1 - x0)
                    y = round(y0 + (y1 - y0) * t)
                    span_px = max(1, bottom - y)
                    for yy in range(y, bottom + 1):
                        k = (yy - y) / span_px
                        c = tuple(int(v * (1.0 - 0.8 * k)) for v in color)
                        self.d.point((x, yy), fill=c)

        self.d.line(pts, fill=color, width=1)

    def chat(self, lines, color=FG):
        """Bottom band: up to CHAT_LINES lines, oldest at the top."""
        self.d.line([(0, CHAT_TOP - 1), (W - 1, CHAT_TOP - 1)], fill=RULE)
        for i, text in enumerate(lines[-CHAT_LINES:]):
            font.draw(self.d, (1, CHAT_TOP + i * LINE_H), self.clip(text), color)

    def message(self, author, text, color=FG, label=None):
        """One message held on screen: author dim, body wrapped under it."""
        self.d.line([(0, CHAT_TOP - 1), (W - 1, CHAT_TOP - 1)], fill=RULE)
        room = self.cols - (len(label) + 1 if label else 0)
        font.draw(self.d, (1, CHAT_TOP), self.clip("@" + author, room), DIM)
        if label:
            font.draw(self.d, (W - 1 - font.width(label), CHAT_TOP), label, DIM)
        for i, row in enumerate(self.wrap(text, CHAT_LINES - 1)):
            font.draw(self.d, (1, CHAT_TOP + (i + 1) * LINE_H), row, color)

    def text(self, s, xy=(0, 0), color=FG):
        font.draw(self.d, xy, s, color)

    def clip(self, text, limit=None):
        limit = limit or self.cols
        return text if len(text) <= limit else text[: limit - 3] + "..."

    def wrap(self, text, rows):
        """Fill `rows` lines greedily, then ellipsis whatever is left over."""
        words = text.split()
        out, i = [], 0
        for r in range(rows):
            line = ""
            while i < len(words):
                probe = f"{line} {words[i]}".strip()
                if font.width(probe) > W - 2 and line:
                    break
                line = probe
                i += 1
            out.append(line)
        if i < len(words):
            out[-1] = self.clip(out[-1] + " ...")
        return [row for row in out if row]

_relay_conn = {}


def _relay_connection(url):
    parts = urllib.parse.urlsplit(url)
    conn = _relay_conn.get(parts.netloc)
    if conn is None:
        cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = _relay_conn[parts.netloc] = cls(parts.netloc, timeout=20)
    return conn, parts.path


def push_relay(url, key, frame, fmt="565", tries=6):
    """POST one frame. The connection stays open between frames: a fresh TLS
    handshake per frame cost ~1.8 s, which capped the push rate."""
    delay = 1.0
    for attempt in range(tries):
        conn, base = _relay_connection(url)
        try:
            conn.request("POST", f"{base}/frame?fmt={fmt}", body=frame, headers={
                "Content-Type": "application/octet-stream", "X-Api-Key": key})
            r = conn.getresponse()
            body = r.read().decode()
            if r.status >= 400:
                raise urllib.error.HTTPError(url, r.status, body, None, None)
            return body
        except (http.client.HTTPException, urllib.error.URLError, OSError):
            conn.close()
            _relay_conn.pop(urllib.parse.urlsplit(url).netloc, None)
            if attempt == tries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30)


def push_panel(host, img, fmt="rgb565"):
    body = pack(img) if fmt == "rgb565" else bytes(
        c for y in range(H) for x in range(W) for c in img.load()[x, y][:3])
    # the ESP32 web server stores plain bodies in a NUL-terminated String
    req = urllib.request.Request(
        f"http://{host}/frame?fmt={fmt}", data=base64.b64encode(body), method="POST",
        headers={"Content-Type": "text/plain",
                 "X-Api-Key": os.environ.get("PXT_RELAY_KEY", "")})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode()


def demo():
    s = Screen()
    s.series([1.0 + math.sin(i / 6) + i / 20 for i in range(40)])
    s.message("pixoticker",
              "Five lines of the 4x6 font: an author line and up to four of body.")
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="write a PNG preview of the demo frame")
    ap.add_argument("--scale", type=int, default=4)
    args = ap.parse_args()
    s = demo()
    if args.out:
        s.img.resize((W * args.scale, H * args.scale), Image.NEAREST).save(args.out)
        print(args.out)
        return
    print(json.dumps({"bytes": len(pack(s.img))}))


if __name__ == "__main__":
    main()
