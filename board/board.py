#!/usr/bin/env python3
"""Cluster board: node agents post stats, this draws them on the 128x64 panel.

    agents ──POST /ingest──▶ board ──POST /frame──▶ relay ──TLS──▶ panel
                               └── GET /  (every layout, live, as LED dots)

  GET  /                    gallery of every layout against the live data
  GET  /preview/<name>.png  one layout, LED style (?scale=N)
  GET  /state               the nodes as the layouts see them
  GET  /health              no auth
  POST /ingest              one node report (agent.py), X-Api-Key
  POST /layout              {"layout": "rows"} pins one, {"layout": "auto"} rotates, X-Api-Key

A node is live or idle while it reports, stale after TTL seconds of silence and
absent until its first report, so a dead agent never leaves old numbers on the panel.
"""
from __future__ import annotations

import argparse
import hmac
import http.client
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import layouts
import preview
from layouts import ABSENT, IDLE, LIVE, STALE, Node

TTL_S = 10
TICK_S = 1.0
ROTATE_S = 15
BUSY_TPS = 0.5
LAST_S = 30   # how long a request average shows, counted from when the agent measured it
MAX_BODY = 4096
RATE_FIELDS = ("dec", "pre", "dec_last", "pre_last", "dec_last_age", "pre_last_age")
LAST_FIELDS = ("dec_last", "pre_last")


def number(v, lo=0.0, hi=1e9):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return float(min(hi, max(lo, v)))


class Board:
    def __init__(self, names, pinned="auto", rotation=None, clock=time.time):
        self.names = list(names)
        if not self.names:
            raise ValueError("a board needs at least one node")
        self.clock = clock
        self.lock = threading.Lock()
        self.reports = {}
        self.seen = {}
        self.hist = {n: deque(maxlen=layouts.HIST) for n in self.names}
        self.pinned = pinned
        self.rotation = rotation or list(layouts.LAYOUTS)
        self.started = clock()

    def ingest(self, body):
        """Store one agent report; returns an error string or None."""
        name = body.get("node")
        if name not in self.names:
            return "unknown node"
        report = {"gpu": number(body.get("gpu"), 0, 100), "mem_gb": number(body.get("mem_gb")),
                  "mem_total_gb": number(body.get("mem_total_gb")),
                  "running": number(body.get("running"))}
        report.update({k: number(body.get(k)) for k in RATE_FIELDS})
        follows = body.get("follows")
        report["worker"] = body.get("worker") is True
        report["follows"] = follows if follows in self.names else None
        with self.lock:
            self.reports[name] = report
            self.seen[name] = self.clock()
        return None

    def nodes(self):
        now = self.clock()
        with self.lock:
            views = [self._view(n, now) for n in self.names]
            self._mirror(views)
            return views

    def _mirror(self, views):
        """A tensor-parallel worker has no server of its own, so it reports no tokens, yet every rank
        decodes every token. Its view shows its leader's rates and state instead: the node it names, or
        else the busiest fresh node that is not a worker. A worker never follows a worker or itself."""
        by_name = {v.name: v for v in views}
        is_worker = {n: bool((self.reports.get(n) or {}).get("worker")) for n in self.names}
        servers = [v for v in views if v.fresh and not is_worker[v.name]]
        for v in views:
            if not is_worker[v.name] or not v.fresh:
                continue
            named = self.reports[v.name]["follows"]
            lead = by_name.get(named) if named else max(servers, key=lambda s: s.activity, default=None)
            if lead is None or not lead.fresh or is_worker[lead.name]:
                continue
            v.dec, v.pre, v.dec_last, v.pre_last = lead.dec, lead.pre, lead.dec_last, lead.pre_last
            v.state, v.tp = lead.state, lead.name

    def _view(self, name, now):
        node = Node(name, hist=list(self.hist[name]))
        if name not in self.seen:
            return node
        node.age = int(now - self.seen[name])
        if node.age > TTL_S:
            node.state = STALE
            return node
        r = self.reports[name]
        node.gpu, node.dec, node.pre = r["gpu"], r["dec"], r["pre"]
        node.dec_last, node.pre_last = r["dec_last"], r["pre_last"]
        if r["mem_gb"] is not None and r["mem_total_gb"]:
            node.mem = 100.0 * r["mem_gb"] / r["mem_total_gb"]
        busy = (r["running"] or 0) > 0 or (node.dec or 0) > BUSY_TPS or (node.pre or 0) > BUSY_TPS
        node.state = LIVE if busy else IDLE
        for k in LAST_FIELDS:
            measured_ago = r[k + "_age"]
            if measured_ago is None or measured_ago + (now - self.seen[name]) > LAST_S:
                setattr(node, k, None)
        return node

    def tick(self):
        """Append every fresh node's sample to its history; call once a second."""
        for n in self.nodes():
            if n.fresh:
                self.hist[n.name].append((n.dec or 0.0, n.gpu or 0.0, n.pre or 0.0, 0.0 if n.tp else n.dec or 0.0))

    def layout(self, now=None):
        if self.pinned != "auto":
            return self.pinned
        now = self.clock() if now is None else now
        return self.rotation[int((now - self.started) // ROTATE_S) % len(self.rotation)]

    def pin(self, name):
        if name != "auto" and name not in layouts.LAYOUTS:
            return False
        self.pinned = name
        return True

    def frame(self):
        name = self.layout()
        return name, layouts.render(name, self.nodes())


class Relay:
    """Pushes a frame to the relay when it differs from the last one sent."""

    def __init__(self, url, key):
        self.url, self.key, self.last, self.failing = url.rstrip("/"), key, None, False

    def push(self, img):
        body = img.tobytes()
        if body == self.last:
            return False
        req = urllib.request.Request(self.url + "/frame?fmt=888", data=body, method="POST", headers={
            "Content-Type": "application/octet-stream", "X-Api-Key": self.key})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
        except (OSError, urllib.error.URLError, http.client.HTTPException) as err:
            if not self.failing:
                print(f"relay push failed: {err}", flush=True)
            self.failing = True
            return False
        if self.failing:
            print("relay reachable again", flush=True)
        self.failing, self.last = False, body
        return True


def scale_param(text, default=5):
    """The preview scale from a query value, 1 to 8; anything that is not a number gets the default."""
    try:
        return min(8, max(1, int(text)))
    except ValueError:
        return default


def png(img, scale):
    out = io.BytesIO()
    preview.leds(img, scale).save(out, "PNG")
    return out.getvalue()


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Pixoticker board</title>
<style>
:root{color-scheme:dark}body{margin:0;padding:16px;background:#0d0d10;color:#ccc;font:14px/1.4 system-ui,sans-serif}
h1{font-size:16px;margin:0 0 4px}p{margin:0 0 16px;color:#888}code{color:#9ad}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}
figure{margin:0}figcaption{margin-top:4px;font-weight:600}figcaption span{color:#7c7;font-weight:400}
img{width:100%;display:block;border-radius:4px;image-rendering:pixelated}
</style>
<h1>Pixoticker board</h1>
<p>Showing on the panel: <b id=now>?</b>. Pin one: <code>curl -H "X-Api-Key: $KEY" -d '{"layout":"rows"}' HOST/layout</code></p>
<div class=grid>@FIGURES@</div>
<script>
function tick(){document.querySelectorAll('img').forEach(i=>{i.src='/preview/'+i.dataset.n+'.png?scale=5&t='+Date.now()});
fetch('/state').then(r=>r.json()).then(s=>{document.getElementById('now').textContent=s.layout+(s.pinned==='auto'?' (rotating)':' (pinned)');
document.querySelectorAll('figcaption span').forEach(e=>{e.textContent=e.dataset.n===s.layout?' on the panel':''})})}
tick();setInterval(tick,2000);
</script>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30  # a client that stalls mid-request frees its thread instead of holding it forever
    board = None
    key = ""

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _authed(self):
        supplied = self.headers.get("X-Api-Key", "")
        return bool(self.key) and hmac.compare_digest(supplied.encode(), self.key.encode())

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            return self._json(200, {"ok": True, "layout": self.board.layout()})
        if u.path == "/":
            figs = "".join(f'<figure><img data-n="{n}" alt="{n}"><figcaption>{n}<span data-n="{n}"></span>'
                           f"</figcaption></figure>" for n in layouts.LAYOUTS)
            return self._send(200, PAGE.replace("@FIGURES@", figs).encode(), "text/html; charset=utf-8")
        if u.path == "/state":
            return self._json(200, {"layout": self.board.layout(), "pinned": self.board.pinned,
                                    "nodes": [vars(n) | {"hist": len(n.hist)} for n in self.board.nodes()]})
        if u.path.startswith("/preview/") and u.path.endswith(".png"):
            name = u.path[len("/preview/"):-4]
            if name not in layouts.LAYOUTS:
                return self._json(404, {"error": "no such layout"})
            scale = scale_param(q.get("scale", [""])[0])
            return self._send(200, png(layouts.render(name, self.board.nodes()), scale), "image/png")
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authed():
            self.close_connection = True
            return self._json(401, {"error": "unauthorized"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            self.close_connection = True
            return self._json(413, {"error": "bad size"})
        try:
            body = json.loads(self.rfile.read(n))
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json(400, {"error": "bad json"})
        if u.path == "/ingest":
            err = self.board.ingest(body)
            return self._json(400, {"error": err}) if err else self._json(200, {"ok": True})
        if u.path == "/layout":
            if not self.board.pin(str(body.get("layout", ""))):
                return self._json(400, {"error": "unknown layout", "known": ["auto"] + list(layouts.LAYOUTS)})
            return self._json(200, {"ok": True, "layout": self.board.pinned})
        return self._json(404, {"error": "not found"})


NIGHT_FLOOR = 40    # the dimmest a pixel that carries content may be while dimmed; below it the LEDs read as off
FLOOR_SLOPE = 0.5   # above the floor, brightness still climbs at this share of the dim level, so two colours never meet
MIN_SLOPE = 0.025   # but never slower than this, so 40 levels of source brightness are still a whole level apart at any dim level
CONTENT_MIN = 50    # a pixel whose brightest channel is below this is structure (a bar track, a rule) and may fade out


class Dimmer:
    """Scales what goes to the panel by `level` from `start` until `end` (local time in `tz`, the end
    itself not dimmed; a window may run past midnight), never below `floor` for anything that carries
    content. Spec: HH:MM-HH:MM@LEVEL[:FLOOR], e.g. 20:00-07:30@0.25 or 20:00-07:30@0.25:40."""

    SPEC = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})@(\d(?:\.\d+)?)(?::(\d{1,3}))?$")

    def __init__(self, spec, tz):
        m = self.SPEC.match(spec or "")
        if not m:
            raise ValueError(f"dim spec {spec!r} is not HH:MM-HH:MM@LEVEL[:FLOOR], for example 20:00-07:30@0.25")
        h1, m1, h2, m2 = (int(g) for g in m.groups()[:4])
        if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
            raise ValueError(f"dim spec {spec!r} has a time that is not on the clock")
        self.floor = NIGHT_FLOOR if m.group(6) is None else int(m.group(6))
        if self.floor > 255:
            raise ValueError(f"dim floor {self.floor} must be 0 to 255")
        self.start, self.end, self.level = h1 * 60 + m1, h2 * 60 + m2, float(m.group(5))
        if self.start == self.end:
            raise ValueError(f"dim spec {spec!r} starts and ends at the same minute")
        if not 0 < self.level <= 1:
            raise ValueError(f"dim level {self.level} must be above 0 and at most 1")
        try:
            self.tz = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {tz!r}") from None

    def factor(self, now):
        local = datetime.fromtimestamp(now, self.tz)
        minute = local.hour * 60 + local.minute
        inside = self.start <= minute < self.end if self.start < self.end else minute >= self.start or minute < self.end
        return self.level if inside else 1.0


def dim_image(img, factor, floor=NIGHT_FLOOR):
    """Dim by the pixel's brightest channel, so its hue survives, and give content a floor. Scaling every
    channel by 0.25 takes a grey label from 95 to 24, which the LEDs barely show: a pixel at CONTENT_MIN or
    above is never dimmed below `floor`, while the faint structure (tracks, rules) is free to fade. The floor
    is a ramp, not a clamp: a brighter source stays brighter, so a live colour and its dimmed idle variant differ
    at any level. A pixel is never made brighter than it was."""
    if factor >= 1.0:
        return img
    out = img.copy()
    px = out.load()
    for y in range(out.height):
        for x in range(out.width):
            r, g, b = px[x, y]
            top = max(r, g, b)
            if not top:
                continue
            target = factor * top
            if top >= CONTENT_MIN:
                target = max(target, floor + (top - CONTENT_MIN) * max(factor * FLOOR_SLOPE, MIN_SLOPE))
            k = min(1.0, target / top)
            px[x, y] = (min(255, int(r * k + 0.5)), min(255, int(g * k + 0.5)), min(255, int(b * k + 0.5)))
    return out


def step(board, relay, dimmer=None, now=None):
    board.tick()
    if relay:
        frame = board.frame()[1]
        if dimmer:
            frame = dim_image(frame, dimmer.factor(time.time() if now is None else now), dimmer.floor)
        relay.push(frame)


def render_loop(board, relay, dimmer=None):
    """One step a second for the life of the process. A step that raises is logged once and the
    loop carries on, because a dead thread would leave the panel showing its last numbers."""
    failing = False
    while True:
        started = time.time()
        try:
            step(board, relay, dimmer)
            failing = False
        except Exception as err:
            if not failing:
                print(f"render step failed: {err!r}", flush=True)
            failing = True
        time.sleep(max(0.0, TICK_S - (time.time() - started)))


def read_key(path):
    with open(os.path.expanduser(path)) as f:
        key = f.read().strip()
    if len(key) < 16:
        raise SystemExit(f"key in {path} is shorter than 16 characters")
    return key


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18101)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--nodes", default=os.environ.get("BOARD_NODES", "studio1,studio2,studio3,studio4"))
    ap.add_argument("--layout", default=os.environ.get("BOARD_LAYOUT", "auto"))
    ap.add_argument("--ingest-key-file", default=os.environ.get("BOARD_INGEST_KEY_FILE", "~/.pxt_ingest_key"))
    ap.add_argument("--relay", default=os.environ.get("BOARD_RELAY_URL", ""), help="relay base URL; empty draws previews only")
    ap.add_argument("--relay-key-file", default=os.environ.get("BOARD_RELAY_KEY_FILE", "~/.pxt_relay_key"))
    ap.add_argument("--dim", default=os.environ.get("BOARD_DIM", ""),
                    help="HH:MM-HH:MM@LEVEL[:FLOOR]: scale the panel's frames by LEVEL in that window, content never "
                         "below FLOOR (default 40), e.g. 20:00-07:30@0.25")
    ap.add_argument("--tz", default=os.environ.get("BOARD_TZ", "UTC"), help="time zone of --dim, e.g. America/New_York")
    args = ap.parse_args()

    names = [n.strip() for n in args.nodes.split(",") if n.strip()]
    if not names:
        raise SystemExit("--nodes names no node")
    board = Board(names)
    if not board.pin(args.layout):
        raise SystemExit(f"unknown layout {args.layout!r}")
    Handler.board, Handler.key = board, read_key(args.ingest_key_file)
    relay = Relay(args.relay, read_key(args.relay_key_file)) if args.relay else None
    try:
        dimmer = Dimmer(args.dim, args.tz) if args.dim else None
    except ValueError as err:
        raise SystemExit(str(err))
    threading.Thread(target=render_loop, args=(board, relay, dimmer), daemon=True).start()
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"board on {args.bind}:{args.port}, nodes {board.names}, relay {args.relay or 'off'}, "
          f"dim {args.dim or 'off'} {args.tz if args.dim else ''}".rstrip(), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
