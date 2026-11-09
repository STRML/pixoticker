#!/usr/bin/env python3
"""Contact sheet of every layout against fixture clusters, drawn as LED dots.

    board/preview.py --out sheet.png          # layouts down, scenarios across

Fixtures cover what the panel really shows: all four busy, one prefilling, and the
degraded case (one node live, one gone silent, two never installed).
"""
from __future__ import annotations

import argparse
import math

from PIL import Image, ImageDraw

import layouts
from layouts import ABSENT, IDLE, LIVE, STALE, Node, W, H


def history(peak, phase=0.0, n=layouts.HIST, gpu=60):
    return [(max(0.0, peak * (0.55 + 0.45 * math.sin(i / 5 + phase)) * (0 if (i // 17) % 3 == 2 else 1)),
             min(100, max(0, gpu + 25 * math.sin(i / 7 + phase)))) for i in range(n)]


def busy():
    return [Node("studio1", LIVE, 92, 58.0, 118.4, 0, 120.0, 2400.0, hist=history(118)),
            Node("studio2", LIVE, 88, 71.0, 101.2, 0, 99.0, 2100.0, hist=history(100, 1.0, gpu=75)),
            Node("studio3", LIVE, 64, 49.0, 87.5, 0, 90.0, 1900.0, hist=history(88, 2.0, gpu=50)),
            Node("studio4", LIVE, 97, 90.0, 133.0, 0, 130.0, 2750.0, hist=history(133, 3.0, gpu=85))]


def prefilling():
    nodes = busy()
    nodes[0] = Node("studio1", LIVE, 100, 62.0, 0, 3180.0, 120.0, 2400.0, hist=history(118))
    nodes[1] = Node("studio2", IDLE, 3, 31.0, 0, 0, 99.0, 12400.0, hist=history(0))
    nodes[2] = Node("studio3", IDLE, 1, 28.0, 0, 0, None, None, hist=[])
    nodes[3] = Node("studio4", LIVE, 41, 55.0, 12.0, 0, 130.0, 2750.0, hist=history(40, 3.0, gpu=40))
    return nodes


def degraded():
    return [Node("studio1", LIVE, 78, 51.0, 84.7, 0, 85.0, 1800.0, hist=history(85)),
            Node("studio2", STALE, age=47),
            Node("studio3", ABSENT),
            Node("studio4", ABSENT)]


SCENARIOS = {"busy": busy, "prefill": prefilling, "degraded": degraded}


def leds(img, scale=5):
    """Each pixel a round lamp on a dark cell, close to how the panel reads."""
    out = Image.new("RGB", (W * scale, H * scale), (6, 6, 8))
    d = ImageDraw.Draw(out)
    px = img.load()
    pad = 0 if scale < 3 else max(1, scale // 6)
    shape = d.ellipse if scale >= 6 else d.rectangle
    for y in range(H):
        for x in range(W):
            if px[x, y] != (0, 0, 0):
                shape([x * scale + pad, y * scale + pad, (x + 1) * scale - pad - 1,
                       (y + 1) * scale - pad - 1], fill=px[x, y])
    return out


def sheet(scale=4, names=None):
    names = names or list(layouts.LAYOUTS)
    gap, label = 8, 14
    cell_w, cell_h = W * scale, H * scale
    out = Image.new("RGB", (label + len(SCENARIOS) * (cell_w + gap), len(names) * (cell_h + gap) + label),
                    (20, 20, 24))
    d = ImageDraw.Draw(out)
    for j, scen in enumerate(SCENARIOS):
        d.text((label + j * (cell_w + gap), 1), scen, fill=(200, 200, 200))
    for i, name in enumerate(names):
        for j, make in enumerate(SCENARIOS.values()):
            out.paste(leds(layouts.render(name, make()), scale), (label + j * (cell_w + gap),
                                                                  label + i * (cell_h + gap)))
        d.text((1, label + i * (cell_h + gap) + cell_h // 2), name[:1], fill=(200, 200, 200))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--layout", action="append")
    args = ap.parse_args()
    sheet(args.scale, args.layout).save(args.out)
    print(args.out)


if __name__ == "__main__":
    main()
