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
TEMP_WARM = (255, 160, 0)
WATT_C = (190, 200, 215)    # system watts: neutral, so it never reads as the amber of prefill
CPU_C = (150, 170, 255)     # CPU percent
TEMP_WARN_C, TEMP_HOT_C = 70.0, 85.0   # white below, amber from 70, red from 85
DEC_C = (0, 225, 120)
PRE_C = (255, 190, 40)
BAD = (255, 80, 80)

DEC_FULL = 150.0    # tok/s floor of the decode history scale, and the focus bar's scale until a peak is learned
PRE_FULL = 3000.0   # the focus bar's prefill scale until a peak is learned
RATE_BAR_W = 60     # the focus view's decode and prefill bars
ROWS_BAR_W = 88     # rows: GPU and memory bars, leaving the right edge for watts and CPU
FOCUS_BAR_W = 66    # focus: the same, leaving room for temperature, watts and CPU
HIST = 36           # samples a sparkline shows
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
    hist: list = field(default_factory=list)   # [(dec, gpu, pre, share), ...] oldest first; share is its part of the cluster decode total
    tp: str | None = None   # a tensor-parallel worker: the node whose rates it shows
    temp: float | None = None   # GPU degrees C, from the node's agent
    watts: float | None = None   # whole-machine power draw in watts (SMC PD0R)
    cpu: float | None = None   # CPU busy percent, 0 to 100
    dec_peak: float | None = None   # the usual peak of each rate, learned by the board; None until it has watched enough
    pre_peak: float | None = None

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
    """At most five characters, so a focus number at scale 3 stays in its half of the panel: no real
    server nears a million tok/s, and a corrupt report is capped at 999K."""
    if v is None:
        return "--"
    v = min(v, 999_000)
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
    """(text, colour) for a rate. A busy node shows its live figure, and 0 when that phase has stopped:
    a request's average is history, and showing it while the node works reads as a live number. The zero is the
    metric's own colour dimmed, not grey: grey at the night-dimming level is close to black on the panel. An idle
    node shows its last request's average dimmed while it is fresh, else 0. Dashes mean no data: a node that is
    not reporting, or one whose agent sent no figure."""
    if not node.fresh:
        return "--", DIM
    live, last = getattr(node, key), getattr(node, key + "_last")
    if live:
        return rate(live), color
    if node.state != LIVE and last:
        return rate(last), dim(color, 0.6)
    return ("--", DIM) if live is None else ("0", dim(color, 0.6))


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


def temp_text(temp):
    """("T46", colour) for a GPU temperature, or None when there is none."""
    if temp is None:
        return None
    return f"T{temp:.0f}", MEM_HOT if temp >= TEMP_HOT_C else TEMP_WARM if temp >= TEMP_WARN_C else FG


def watts_text(watts):
    """("W182", colour), capped at W999 so it is never wider than four characters; None without a reading."""
    return None if watts is None else (f"W{min(watts, 999):.0f}", WATT_C)


def cpu_text(cpu):
    return None if cpu is None else (f"C{cpu:.0f}", CPU_C)


STAT_W = 4 * font.ADVANCE   # the widest figure: T130, W999, C100
STAT_GAP = 4


def stats_width(parts):
    return len(parts) * STAT_W + STAT_GAP * max(0, len(parts) - 1)


def draw_stats(c, x, y, parts):
    """The given (text, colour) parts left to right from x, each right-aligned in its own slot, so a figure gaining
    or losing a digit never moves its neighbours. An absent part leaves its slot empty."""
    for part in parts:
        if part:
            c.text_right(x + STAT_W, y, *part)
        x += STAT_W + STAT_GAP
    return x


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
            if t := temp_text(n.temp):
                c.text_right(54 + STAT_W, y + 1, *t)
            dt, dc = shown(n, "dec", DEC_C)
            pt, pc = shown(n, "pre", PRE_C)
            c.text_right(94, y + 1, "D" + dt, dc)
            c.text_right(126, y + 1, "P" + pt, pc)
        else:
            c.text(14, y + 1, status_text(n), BAD if n.state == STALE else DIM)
        c.hbar(1, y + 8, ROWS_BAR_W, 3, frac(n.gpu) if n.fresh else 0, gc)
        c.hbar(1, y + 12, ROWS_BAR_W, 3, frac(n.mem) if n.fresh else 0, mc)
        if n.fresh:
            parts = [watts_text(n.watts), cpu_text(n.cpu)]
            draw_stats(c, 126 - stats_width(parts), y + 9, parts)
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
        if len(dt) <= 4:
            c.text(x + 4 + len(dt) * 2 * font.ADVANCE, y + 9, "D", dim(dc, 0.6))
        c.text_right(x + 62, y + 9, "P" + pt, pc)
        draw_stats(c, x + 2, y + 22, [temp_text(n.temp), watts_text(n.watts), cpu_text(n.cpu)])
        c.hbar(x + 2, y + 28, 60, 1, frac(n.gpu), gc)
        c.hbar(x + 2, y + 30, 60, 1, frac(n.mem), mc)
    return c.img


def totals(nodes):
    """Cluster-wide live decode and prefill tok/s, summed over the nodes that are reporting.
    A tensor-parallel worker shows its leader's rates, so it is left out: each token counts once."""
    live = [n for n in nodes[:4] if n.fresh and n.tp is None]
    return sum(n.dec or 0 for n in live), sum(n.pre or 0 for n in live)


def total_history(nodes):
    """Decode tok/s of all reporting nodes added up, newest sample last. A sample counts what its node
    added to the total when it was taken (a worker's mirrored rate adds nothing), so a node changing
    role later does not rewrite the past."""
    series = [n.hist for n in nodes[:4] if n.fresh]
    width = max((len(h) for h in series), default=0)
    out = [0.0] * width
    for h in series:
        for k, sample in enumerate(reversed(h)):
            out[width - 1 - k] += (sample[3] if len(sample) > 3 else sample[0]) or 0
    return out


def total_text(value, color):
    return ("D" if color == DEC_C else "P") + (rate(value) if value else "0"), color if value else dim(color, 0.6)


def area(c, x, y, w, h, samples, peak, color):
    """Decode history as filled columns, newest at the right edge."""
    c.rect(x, y, w, h, (10, 10, 14))
    for k, value in enumerate(samples[-w:]):
        bar = round((h - 1) * min(1.0, (value or 0) / peak))
        if bar:
            c.rect(x + w - len(samples[-w:]) + k, y + h - bar, 1, bar, dim(color, 0.6))


def spark(nodes):
    """A cluster row on top (total decode and prefill, total decode history), then one
    14 px band per node: GPU and memory as numbers with a 10 px bar each, a decode history (HIST samples) on a scale
    shared by all nodes, the GPU trace over it, amber ticks where a prefill ran, watts over CPU percent, and the rates."""
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
        for k, (_, gpu, pre, *_) in enumerate(samples):
            col = x0 + wid - len(samples) + k
            c.d.point((col, y + 12 - round(11 * frac(gpu))), fill=gc)
            if pre:
                c.rect(col, y + 11, 1, 2, PRE_C)
        draw_stats(c, x0 + wid + 3, y + 1, [watts_text(n.watts)])
        draw_stats(c, x0 + wid + 3, y + 7, [cpu_text(n.cpu)])
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
    x = 126 - font.width(tp)
    c.text(x, 1, tp, tpc)
    x -= 4 + font.width(td)
    c.text(x, 1, td, tdc)
    c.text(x - 4 - font.width("ALL"), 1, "ALL", DIM)
    if top.fresh:
        dt, dc = shown(top, "dec", DEC_C)
        pt, pc = shown(top, "pre", PRE_C)
        c.big(1, 10, dt, dc, scale=3)
        c.text(1, 30, "DECODE TOK/S", DIM)
        c.text(14, 1, f"G{pct(top.gpu)}", gc)
        c.text(34, 1, f"M{pct(top.mem)}", mc)
        c.big(66, 10, pt, pc, scale=3)
        c.text(66, 30, "PREFILL TOK/S", DIM)
        live_dec, live_pre = top.dec or 0, top.pre or 0
        c.hbar(1, 37, RATE_BAR_W, 2, live_dec / (top.dec_peak or DEC_FULL), DEC_C)
        c.hbar(66, 37, RATE_BAR_W, 2, live_pre / (top.pre_peak or PRE_FULL), PRE_C)
        c.hbar(1, 40, FOCUS_BAR_W, 2, frac(top.gpu), gc)
        c.hbar(1, 43, FOCUS_BAR_W, 2, frac(top.mem), mc)
        parts = [temp_text(top.temp), watts_text(top.watts), cpu_text(top.cpu)]
        draw_stats(c, 126 - stats_width(parts), 39, parts)
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
