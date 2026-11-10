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

DEC_FULL = 150.0    # tok/s floor of the decode history scale
HIST = 62           # samples a sparkline shows
MEM_WARN = 85.0
BAR_X, BAR_W = 32, 10   # spark: the GPU and memory bars beside their numbers

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


def totals(nodes):
    """Cluster-wide live decode and prefill tok/s, summed over the nodes that are reporting."""
    live = [n for n in nodes[:4] if n.fresh]
    return sum(n.dec or 0 for n in live), sum(n.pre or 0 for n in live)


def total_history(nodes):
    """Decode tok/s of all reporting nodes added up, newest sample last."""
    series = [n.hist for n in nodes[:4] if n.fresh]
    width = max((len(h) for h in series), default=0)
    out = [0.0] * width
    for h in series:
        for k, sample in enumerate(reversed(h)):
            out[width - 1 - k] += sample[0] or 0
    return out


def total_text(value, color):
    return ("D" if color == DEC_C else "P") + (rate(value) if value else "0"), color if value else DIM


def area(c, x, y, w, h, samples, peak, color):
    """Decode history as filled columns, newest at the right edge."""
    c.rect(x, y, w, h, (10, 10, 14))
    for k, value in enumerate(samples[-w:]):
        bar = round((h - 1) * min(1.0, (value or 0) / peak))
        if bar:
            c.rect(x + w - len(samples[-w:]) + k, y + h - bar, 1, bar, dim(color, 0.6))


def spark(nodes):
    """A cluster row on top (total decode and prefill, total decode history), then one
    14 px band per node: GPU and memory as numbers with a 10 px bar each, a decode history on a scale shared by all
    nodes, the GPU trace over it, amber ticks where a prefill ran, and the rates."""
    c = Canvas()
    x0, wid = 43, HIST
    dec_all, pre_all = totals(nodes)
    td, tdc = total_text(dec_all, DEC_C)
    tp, tpc = total_text(pre_all, PRE_C)
    c.text(1, 1, "ALL", FG)
    c.text(14, 1, td, tdc)
    c.text_right(126, 1, tp, tpc)
    history = total_history(nodes)
    area(c, x0, 0, wid, 7, history, max([DEC_FULL] + history), DEC_C)
    c.rect(0, 7, W, 1, RULE)
    shared = max([DEC_FULL / 2] + [s[0] or 0 for n in nodes[:4] for s in n.hist])
    for i, n in enumerate(nodes[:4]):
        y = 8 + i * 14
        gc, mc = bar_colors(n)
        c.text(1, y + 4, n.tag, FG if n.fresh else DIM)
        if not n.fresh:
            c.text(14, y + 4, status_text(n), BAD if n.state == STALE else DIM)
            continue
        c.text(14, y + 1, f"G{pct(n.gpu)}", gc)
        c.text(14, y + 7, f"M{pct(n.mem)}", mc)
        c.hbar(BAR_X, y + 2, BAR_W, 3, frac(n.gpu), gc)
        c.hbar(BAR_X, y + 8, BAR_W, 3, frac(n.mem), mc)
        pt, pc = shown(n, "pre", PRE_C)
        dt, dc = shown(n, "dec", DEC_C)
        samples = n.hist[-wid:]
        area(c, x0, y + 1, wid, 12, [s[0] for s in samples], shared, DEC_C)
        for k, (_, gpu, pre) in enumerate(samples):
            col = x0 + wid - len(samples) + k
            c.d.point((col, y + 12 - round(11 * frac(gpu))), fill=gc)
            if pre:
                c.rect(col, y + 11, 1, 2, PRE_C)
        c.text_right(126, y + 1, "D" + dt, dc)
        c.text_right(126, y + 7, "P" + pt, pc)
    return c.img


def focus(nodes):
    """The busiest node large on top, all four as thin strips below."""
    c = Canvas()
    live = [n for n in nodes[:4] if n.fresh]
    top = max(live, key=lambda n: n.activity) if live else nodes[0]
    gc, mc = bar_colors(top)
    c.text(1, 1, top.tag, FG if top.fresh else DIM)
    dec_all, pre_all = totals(nodes)
    td, tdc = total_text(dec_all, DEC_C)
    tp, tpc = total_text(pre_all, PRE_C)
    c.text(14, 1, "ALL", DIM)
    x = c.text(30, 1, td, tdc)
    c.text(x + 4, 1, tp, tpc)
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


LAYOUTS = {"rows": rows, "cols": cols, "quad": quad, "spark": spark, "focus": focus}


def render(name, nodes):
    return LAYOUTS[name](nodes)
