"""128x64 layouts for a cluster of up to four nodes.

Each layout takes a list of Node views and returns a 128x64 RGB image. Colours
carry the metric so a glance works without reading: cyan GPU, violet memory,
green decode, amber prefill. A node with no fresh report is drawn dim.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import font46 as font  # noqa: E402

W, H = 128, 64

BG = (0, 0, 0)
FG = (235, 235, 235)
DIM = (95, 95, 95)
RULE = (30, 30, 30)
TRACK = (24, 24, 30)
GPU_C = (0, 200, 255)
MEM_C = (190, 100, 255)
MEM_HOT = (255, 90, 70)
DEC_C = (0, 225, 120)
PRE_C = (255, 190, 40)
BAD = (255, 80, 80)

DEC_FULL = 150.0    # tok/s that fills a decode tile
PRE_FULL = 3000.0   # tok/s that fills a prefill tile
HIST = 62           # samples a sparkline shows
MEM_WARN = 85.0

LIVE, IDLE, STALE, ABSENT = "live", "idle", "stale", "absent"


@dataclass
class Node:
    name: str
    state: str = ABSENT
    gpu: float | None = None
    mem: float | None = None          # percent
    dec: float | None = None
    pre: float | None = None
    dec_last: float | None = None
    pre_last: float | None = None
    age: int = 0
    hist: list = field(default_factory=list)   # [(dec, gpu), ...] oldest first

    @property
    def tag(self):
        digits = "".join(ch for ch in self.name if ch.isdigit())
        return "S" + digits if digits else self.name[:2].upper()

    @property
    def fresh(self):
        return self.state in (LIVE, IDLE)

    @property
    def activity(self):
        return (self.dec or 0) + (self.pre or 0) * 0.1 if self.fresh else -1.0


def rate(v):
    if v is None:
        return "--"
    if v < 10:
        return f"{v:.1f}"
    if v < 1000:
        return f"{v:.0f}"
    if v < 10000:
        return f"{v / 1000:.1f}K"
    return f"{v / 1000:.0f}K"


def pct(v):
    return "--" if v is None else f"{v:.0f}"


def dim(color, k=0.35):
    return tuple(int(c * k) for c in color)


def shown(node, key, color):
    """(text, colour) for a rate: the live figure while busy, else the last
    request's average dimmed, else dashes. A node that is not reporting shows dashes."""
    if not node.fresh:
        return "--", DIM
    live, last = getattr(node, key), getattr(node, key + "_last")
    if live:
        return rate(live), color
    return (rate(last), dim(color, 0.6)) if last else ("--", DIM)


class Canvas:
    def __init__(self):
        self.img = Image.new("RGB", (W, H), BG)
        self.d = ImageDraw.Draw(self.img)

    def text(self, x, y, s, color=FG):
        return font.draw(self.d, (x, y), s, color)

    def text_right(self, x_right, y, s, color=FG):
        return self.text(x_right - font.width(s), y, s, color)

    def text_center(self, x_mid, y, s, color=FG):
        return self.text(x_mid - font.width(s) // 2, y, s, color)

    def big(self, x, y, s, color, scale=2):
        """The 4x6 font with every pixel grown to scale x scale."""
        for i, ch in enumerate(s):
            for row, bits in enumerate(font.glyph(ch)):
                for col, bit in enumerate(bits):
                    if bit == "1":
                        px, py = x + (i * font.ADVANCE + col) * scale, y + row * scale
                        self.d.rectangle([px, py, px + scale - 1, py + scale - 1], fill=color)
        return x + len(s) * font.ADVANCE * scale

    def rect(self, x, y, w, h, color):
        if w > 0 and h > 0:
            self.d.rectangle([x, y, x + w - 1, y + h - 1], fill=color)

    def hbar(self, x, y, w, h, frac, color):
        self.rect(x, y, w, h, TRACK)
        self.rect(x, y, round(w * min(1.0, max(0.0, frac))), h, color)

    def vbar(self, x, y, w, h, frac, color):
        self.rect(x, y, w, h, TRACK)
        fill = round(h * min(1.0, max(0.0, frac)))
        self.rect(x, y + h - fill, w, fill, color)


def mem_color(node):
    return MEM_HOT if (node.mem or 0) >= MEM_WARN else MEM_C


def bar_colors(node):
    if not node.fresh:
        return DIM, DIM
    return GPU_C, mem_color(node)


def status_text(node):
    if node.state == STALE:
        return f"STALE {node.age}s" if node.age < 100 else "STALE"
    return "--" if node.state == ABSENT else ""


def frac(v):
    return (v or 0) / 100.0


def rows(nodes):
    """Four 16 px bands: tag, GPU and memory numbers, decode and prefill; two bars beneath."""
    c = Canvas()
    for i, n in enumerate(nodes[:4]):
        y = i * 16
        gc, mc = bar_colors(n)
        c.text(1, y + 1, n.tag, FG if n.fresh else DIM)
        if n.fresh:
            c.text(14, y + 1, f"G{pct(n.gpu)}", gc)
            c.text(34, y + 1, f"M{pct(n.mem)}", mc)
            dt, dc = shown(n, "dec", DEC_C)
            pt, pc = shown(n, "pre", PRE_C)
            c.text_right(94, y + 1, "D" + dt, dc)
            c.text_right(126, y + 1, "P" + pt, pc)
        else:
            c.text(14, y + 1, status_text(n), BAD if n.state == STALE else DIM)
        c.hbar(1, y + 8, 126, 3, frac(n.gpu) if n.fresh else 0, gc)
        c.hbar(1, y + 12, 126, 3, frac(n.mem) if n.fresh else 0, mc)
    return c.img


def cols(nodes):
    """Four 32 px columns: tag, a tall GPU bar beside a tall memory bar, then the numbers."""
    c = Canvas()
    for i, n in enumerate(nodes[:4]):
        x = i * 32
        gc, mc = bar_colors(n)
        c.text_center(x + 16, 1, n.tag, FG if n.fresh else DIM)
        c.vbar(x + 4, 9, 10, 26, frac(n.gpu) if n.fresh else 0, gc)
        c.vbar(x + 18, 9, 10, 26, frac(n.mem) if n.fresh else 0, mc)
        if i:
            c.rect(x - 1, 0, 1, H, RULE)
        if not n.fresh:
            word = status_text(n).split()
            for k, part in enumerate(word):
                c.text_center(x + 16, 44 + k * 7, part, BAD if n.state == STALE else DIM)
            continue
        dt, dc = shown(n, "dec", DEC_C)
        pt, pc = shown(n, "pre", PRE_C)
        c.text(x + 3, 37, f"G{pct(n.gpu)}", gc)
        c.text(x + 3, 44, f"M{pct(n.mem)}", mc)
        c.text(x + 3, 51, "D" + dt, dc)
        c.text(x + 3, 58, "P" + pt, pc)
    return c.img


def quad(nodes):
    """2x2 quadrants of 64x32: a big decode figure, prefill beside it, two bars under."""
    c = Canvas()
    c.rect(63, 0, 1, H, RULE)
    c.rect(0, 31, W, 1, RULE)
    for i, n in enumerate(nodes[:4]):
        x, y = (i % 2) * 64, (i // 2) * 32
        gc, mc = bar_colors(n)
        c.text(x + 2, y + 1, n.tag, FG if n.fresh else DIM)
        if not n.fresh:
            c.text(x + 16, y + 1, status_text(n), BAD if n.state == STALE else DIM)
            continue
        c.text(x + 16, y + 1, f"G{pct(n.gpu)}", gc)
        c.text(x + 38, y + 1, f"M{pct(n.mem)}", mc)
        dt, dc = shown(n, "dec", DEC_C)
        pt, pc = shown(n, "pre", PRE_C)
        c.big(x + 2, y + 9, dt, dc, scale=2)
        c.text(x + 2, y + 22, "TOK/S", DIM)
        c.text_right(x + 62, y + 15, "P", dim(PRE_C, 0.6))
        c.text_right(x + 62, y + 22, pt, pc)
        c.hbar(x + 2, y + 28, 60, 1, frac(n.gpu), gc)
        c.hbar(x + 2, y + 30, 60, 1, frac(n.mem), mc)
    return c.img


def spark(nodes):
    """Four bands: numbers on the left, a decode-rate history with the GPU trace over it."""
    c = Canvas()
    x0, wid = 43, HIST
    for i, n in enumerate(nodes[:4]):
        y = i * 16
        gc, mc = bar_colors(n)
        c.text(1, y + 1, n.tag, FG if n.fresh else DIM)
        if not n.fresh:
            c.text(14, y + 1, status_text(n), BAD if n.state == STALE else DIM)
            continue
        c.text(14, y + 1, f"G{pct(n.gpu)}", gc)
        c.text(14, y + 8, f"M{pct(n.mem)}", mc)
        pt, pc = shown(n, "pre", PRE_C)
        dt, dc = shown(n, "dec", DEC_C)
        samples = n.hist[-wid:]
        peak = max([DEC_FULL / 2] + [s[0] or 0 for s in samples])
        c.rect(x0, y + 1, wid, 14, (10, 10, 14))
        for k, (dec, gpu) in enumerate(samples):
            h = round(13 * min(1.0, (dec or 0) / peak))
            if h:
                c.rect(x0 + wid - len(samples) + k, y + 14 - h, 1, h, dim(DEC_C, 0.6))
            gy = y + 14 - round(13 * frac(gpu))
            c.d.point((x0 + wid - len(samples) + k, gy), fill=gc)
        c.text_right(126, y + 1, "D" + dt, dc)
        c.text_right(126, y + 8, "P" + pt, pc)
    return c.img


def focus(nodes):
    """The busiest node large on top, all four as thin strips below."""
    c = Canvas()
    live = [n for n in nodes[:4] if n.fresh]
    top = max(live, key=lambda n: n.activity) if live else nodes[0]
    gc, mc = bar_colors(top)
    c.text(1, 1, top.tag, FG if top.fresh else DIM)
    if top.fresh:
        dt, dc = shown(top, "dec", DEC_C)
        pt, pc = shown(top, "pre", PRE_C)
        c.big(1, 10, dt, dc, scale=3)
        c.text(1, 30, "DECODE TOK/S", DIM)
        c.text_right(126, 1, f"G{pct(top.gpu)} M{pct(top.mem)}", FG)
        c.big(66, 10, pt, pc, scale=2)
        c.text(66, 24, "PREFILL TOK/S", DIM)
        c.hbar(1, 38, 126, 2, frac(top.gpu), gc)
        c.hbar(1, 41, 126, 2, frac(top.mem), mc)
    else:
        c.text(14, 1, status_text(top), BAD if top.state == STALE else DIM)
    c.rect(0, 45, W, 1, RULE)
    for i, n in enumerate(nodes[:4]):
        x = i * 32
        gcn, mcn = bar_colors(n)
        c.text(x + 2, 48, n.tag, FG if n.fresh else DIM)
        if n.fresh:
            dt, dc = shown(n, "dec", DEC_C)
            c.text_right(x + 30, 48, dt, dc)
        c.hbar(x + 2, 56, 28, 2, frac(n.gpu) if n.fresh else 0, gcn)
        c.hbar(x + 2, 60, 28, 2, frac(n.mem) if n.fresh else 0, mcn)
    return c.img


def tile(c, x, y, w, h, text, base, level):
    """A filled tile: the metric hue, brighter as `level` (0..1) rises, number centred."""
    c.rect(x, y, w, h, tuple(int(ch * (0.18 + 0.62 * min(1.0, max(0.0, level)))) for ch in base))
    c.text_center(x + w // 2, y + (h - font.H) // 2, text, FG)


def matrix(nodes):
    """A table of heat tiles: one row per node, columns GPU, memory, decode, prefill."""
    c = Canvas()
    heads = (("GPU", GPU_C), ("MEM", MEM_C), ("DEC", DEC_C), ("PRE", PRE_C))
    for j, (label, color) in enumerate(heads):
        c.text_center(16 + j * 28 + 14, 1, label, color)
    for i, n in enumerate(nodes[:4]):
        y = 8 + i * 14
        c.text(1, y + 4, n.tag, FG if n.fresh else DIM)
        if not n.fresh:
            c.text(16, y + 4, status_text(n), BAD if n.state == STALE else DIM)
            continue
        dt, _ = shown(n, "dec", DEC_C)
        pt, _ = shown(n, "pre", PRE_C)
        tile(c, 16, y, 27, 13, pct(n.gpu), GPU_C, frac(n.gpu))
        tile(c, 44, y, 27, 13, pct(n.mem), mem_color(n), frac(n.mem))
        tile(c, 72, y, 27, 13, dt, DEC_C, (n.dec or n.dec_last or 0) / DEC_FULL)
        tile(c, 100, y, 27, 13, pt, PRE_C, (n.pre or n.pre_last or 0) / PRE_FULL)
    return c.img


LAYOUTS = {"rows": rows, "cols": cols, "quad": quad, "spark": spark, "focus": focus, "matrix": matrix}


def render(name, nodes):
    return LAYOUTS[name](nodes)
