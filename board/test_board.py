#!/usr/bin/env python3
"""Loopback checks for the board and the node agent: python3 board/test_board.py

Runs the real relay and the real board on ephemeral ports. No hardware, no network.
"""
import base64
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server"))

import agent  # noqa: E402
import board as boardmod  # noqa: E402
import layouts  # noqa: E402
import peaks  # noqa: E402
import preview  # noqa: E402
import pxtrelay  # noqa: E402

KEY = "k" * 24
RELAY_KEY = "r" * 24


def call(url, key=None, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if key:
        req.add_header("X-Api-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def report(node, **kw):
    body = {"node": node, "gpu": 80, "mem_gb": 128, "mem_total_gb": 256, "dec": 50.0, "pre": 0.0,
            "dec_last": 50.0, "pre_last": 2000.0, "dec_last_age": 0.0, "pre_last_age": 0.0}
    body.update(kw)
    return body


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class BoardLogic(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.board = boardmod.Board(["studio1", "studio2", "studio3", "studio4"], clock=self.clock)

    def states(self):
        return [n.state for n in self.board.nodes()]

    def test_absent_until_first_report(self):
        self.assertEqual(self.states(), [layouts.ABSENT] * 4)

    def test_live_idle_and_memory_percent(self):
        self.board.ingest(report("studio1"))
        self.board.ingest(report("studio2", dec=0.0, pre=0.0))
        nodes = self.board.nodes()
        self.assertEqual([n.state for n in nodes[:2]], [layouts.LIVE, layouts.IDLE])
        self.assertAlmostEqual(nodes[0].mem, 50.0)

    def test_silent_node_goes_stale_and_drops_its_numbers(self):
        self.board.ingest(report("studio1"))
        self.clock.t += boardmod.TTL_S + 5
        n = self.board.nodes()[0]
        self.assertEqual(n.state, layouts.STALE)
        self.assertIsNone(n.dec)
        self.assertEqual(n.age, boardmod.TTL_S + 5)

    def test_reporting_again_revives_a_stale_node(self):
        self.board.ingest(report("studio1"))
        self.clock.t += 60
        self.board.ingest(report("studio1"))
        self.assertEqual(self.states()[0], layouts.LIVE)

    def test_unknown_node_and_junk_numbers_are_rejected_or_cleaned(self):
        self.assertEqual(self.board.ingest(report("intruder")), "unknown node")
        self.board.ingest(report("studio1", gpu="lots", dec=float("nan"), mem_gb=True, pre=-5))
        n = self.board.nodes()[0]
        self.assertIsNone(n.gpu)
        self.assertIsNone(n.dec)
        self.assertEqual(n.pre, 0.0)
        self.assertIsNone(n.mem)

    def test_no_rates_means_idle_with_dashes_not_a_crash(self):
        self.board.ingest(report("studio1", dec=None, pre=None, dec_last=None, pre_last=None))
        self.assertEqual(self.states()[0], layouts.IDLE)
        for name in layouts.LAYOUTS:
            self.assertEqual(layouts.render(name, self.board.nodes()).size, (128, 64))

    def last(self):
        n = self.board.nodes()[0]
        return n.dec_last, n.pre_last

    def ingest_last(self, dec=None, pre=None, dec_age=None, pre_age=None, running=0, live_pre=0.0):
        self.board.ingest(report("studio1", dec=0.0, pre=live_pre, running=running, dec_last=dec, pre_last=pre,
                                 dec_last_age=dec_age, pre_last_age=pre_age))

    def test_an_average_shows_until_its_age_passes_last_s_then_zero(self):
        self.ingest_last(50.0, 2000.0, 5.0, 5.0)
        self.assertEqual(self.last(), (50.0, 2000.0))
        self.ingest_last(50.0, 2000.0, boardmod.LAST_S + 1.0, boardmod.LAST_S + 1.0)
        self.assertEqual(self.last(), (None, None))
        self.assertEqual(layouts.shown(self.board.nodes()[0], "dec", layouts.DEC_C)[0], "0")

    def test_time_since_the_report_counts_toward_the_age(self):
        self.ingest_last(50.0, 2000.0, 25.0, 25.0)
        self.clock.t += 4
        self.assertEqual(self.last(), (50.0, 2000.0))
        self.clock.t += 2
        self.assertEqual(self.last(), (None, None))

    def test_a_request_shorter_than_a_sample_shows_its_average(self):
        self.ingest_last(120.0, 5000.0, 0.0, 0.0)
        self.assertEqual(self.last(), (120.0, 5000.0))

    def test_an_expired_average_stays_hidden_while_the_next_request_runs(self):
        self.ingest_last(120.0, 5000.0, 100.0, 100.0, running=1, live_pre=2800.0)
        n = self.board.nodes()[0]
        self.assertEqual((n.state, n.dec_last, n.pre_last), (layouts.LIVE, None, None))
        self.assertEqual(layouts.shown(n, "dec", layouts.DEC_C)[0], "0")

    def test_each_average_expires_on_its_own_age(self):
        self.ingest_last(120.0, 6000.0, boardmod.LAST_S + 1.0, 1.0)
        self.assertEqual(self.last(), (None, 6000.0))

    def test_an_average_with_no_age_is_not_shown(self):
        self.ingest_last(120.0, 6000.0)
        self.assertEqual(self.last(), (None, None))

    def test_history_only_grows_while_fresh(self):
        self.board.ingest(report("studio1"))
        self.board.tick()
        self.board.tick()
        self.clock.t += 60
        self.board.tick()
        self.assertEqual(len(self.board.hist["studio1"]), 2)

    def test_rotation_and_pin(self):
        seen = {self.board.layout(1000.0 + i * boardmod.ROTATE_S) for i in range(len(layouts.LAYOUTS))}
        self.assertEqual(seen, set(layouts.LAYOUTS))
        self.assertTrue(self.board.pin("quad"))
        self.assertEqual(self.board.layout(), "quad")
        self.assertFalse(self.board.pin("nope"))
        self.assertEqual(self.board.layout(), "quad")


class LayoutsFit(unittest.TestCase):
    def test_every_layout_draws_every_scenario_inside_the_panel(self):
        for name in layouts.LAYOUTS:
            for scen, make in preview.SCENARIOS.items():
                img = layouts.render(name, make())
                self.assertEqual(img.size, (128, 64), (name, scen))
                self.assertIsNotNone(img.getbbox(), f"{name}/{scen} drew nothing")

    def test_degraded_never_shows_numbers_for_the_silent_nodes(self):
        a = layouts.render("rows", preview.degraded())
        b = layouts.render("rows", preview.degraded()[:1] + [layouts.Node("studio2", layouts.STALE, age=47)] +
                           [layouts.Node("studio3"), layouts.Node("studio4")])
        self.assertEqual(a.tobytes(), b.tobytes())


class Hardening(unittest.TestCase):
    """The classes the first review found: unguarded loops, unchecked parses, unbounded waits."""

    def test_render_loop_survives_a_step_that_raises(self):
        calls = []

        def flaky(board, relay, dimmer=None):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("boom")

        real_step, real_tick = boardmod.step, boardmod.TICK_S
        boardmod.step, boardmod.TICK_S = flaky, 0.01
        try:
            threading.Thread(target=boardmod.render_loop, args=(None, None), daemon=True).start()
            deadline = time.time() + 3
            while len(calls) < 5 and time.time() < deadline:
                time.sleep(0.01)
        finally:
            boardmod.step, boardmod.TICK_S = real_step, real_tick
        self.assertGreaterEqual(len(calls), 5)

    def test_a_truncated_relay_reply_is_a_failed_push_not_a_crash(self):
        import http.client
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)

        def serve():
            conn, _ = srv.accept()
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort")
            conn.close()

        threading.Thread(target=serve, daemon=True).start()
        relay = boardmod.Relay(f"http://127.0.0.1:{srv.getsockname()[1]}", RELAY_KEY)
        img = layouts.render("rows", preview.busy())
        try:
            self.assertFalse(relay.push(img))
        except http.client.HTTPException:
            self.fail("IncompleteRead escaped Relay.push")
        finally:
            srv.close()
        self.assertTrue(relay.failing)

    def test_a_board_with_no_nodes_is_refused(self):
        with self.assertRaises(ValueError):
            boardmod.Board([])

    def test_scale_query_takes_numbers_and_ignores_junk(self):
        self.assertEqual(boardmod.scale_param("3"), 3)
        self.assertEqual(boardmod.scale_param("99"), 8)
        self.assertEqual(boardmod.scale_param("0"), 1)
        self.assertEqual(boardmod.scale_param("abc"), 5)
        self.assertEqual(boardmod.scale_param(""), 5)

    def test_handler_has_a_socket_timeout(self):
        self.assertGreater(boardmod.Handler.timeout, 0)

    def test_memory_with_unreadable_vm_stat_reports_nothing_instead_of_raising(self):
        import subprocess
        real = agent.subprocess.run

        def fake(cmd, **kw):
            out = "17179869184\n" if cmd[0] == "sysctl" else "vm_stat: command output changed shape\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=out)

        agent.subprocess.run = fake
        try:
            self.assertEqual(agent.memory(), (None, None))
        finally:
            agent.subprocess.run = real


class Totals(unittest.TestCase):
    def test_totals_sum_only_reporting_nodes(self):
        nodes = [layouts.Node("studio1", layouts.LIVE, dec=100.0, pre=0.0),
                 layouts.Node("studio2", layouts.LIVE, dec=50.5, pre=3000.0),
                 layouts.Node("studio3", layouts.STALE, dec=999.0, pre=999.0),
                 layouts.Node("studio4")]
        self.assertEqual(layouts.totals(nodes), (150.5, 3000.0))

    def test_spark_focus_cols_and_quad_draw_the_same_cluster_bar(self):
        bars = {name: layouts.render(name, preview.busy()).crop((0, 0, 128, 8)).tobytes()
                for name in ("spark", "focus", "cols", "quad")}
        self.assertEqual(len(set(bars.values())), 1)
        top = layouts.render("focus", preview.busy()).crop((0, 0, 128, 7))
        self.assertIn(layouts.DEC_C, [c for _, c in top.getcolors()])

    def test_rows_has_no_room_for_the_cluster_bar(self):
        self.assertNotEqual(layouts.render("rows", preview.busy()).crop((0, 0, 128, 8)).tobytes(),
                            layouts.render("focus", preview.busy()).crop((0, 0, 128, 8)).tobytes())

    def test_the_cluster_bar_averages_the_percentages_and_adds_the_rest(self):
        nodes = [layouts.Node("studio1", layouts.LIVE, gpu=100.0, mem=60.0, cpu=10.0, watts=300.0, dec=100.0, pre=2000.0),
                 layouts.Node("studio2", layouts.IDLE, gpu=50.0, mem=40.0, cpu=None, watts=250.0, dec=0.0, pre=1000.0),
                 layouts.Node("studio3", layouts.STALE, gpu=0.0, mem=0.0, cpu=99.0, watts=999.0, dec=500.0, pre=500.0),
                 layouts.Node("studio4")]
        texts = [part and part[0] for part in layouts.summary_parts(nodes)]
        self.assertEqual(texts, ["G75%", "C10%", "M50%", "550W", "D100", "P3.0K"])

    def test_the_cluster_bar_counts_a_tensor_parallel_job_once_but_every_machines_load(self):
        nodes = [layouts.Node("studio1", layouts.LIVE, gpu=90.0, mem=50.0, cpu=10.0, watts=200.0, dec=100.0, pre=3000.0),
                 layouts.Node("studio2", layouts.LIVE, gpu=70.0, mem=50.0, cpu=20.0, watts=200.0, dec=100.0, pre=3000.0, tp="studio1")]
        texts = [part and part[0] for part in layouts.summary_parts(nodes)]
        self.assertEqual(texts, ["G80%", "C15%", "M50%", "400W", "D100", "P3.0K"])

    def test_the_cluster_bar_switches_to_kilowatts_and_goes_grey_without_a_reporting_node(self):
        busy = [layouts.Node(f"studio{i}", layouts.LIVE, gpu=1.0, mem=1.0, watts=300.0) for i in (1, 2, 3, 4)]
        self.assertEqual(layouts.summary_parts(busy)[3][0], "1.2KW")
        absurd = [layouts.Node(f"studio{i}", layouts.LIVE, watts=9999.0) for i in (1, 2, 3, 4)]
        self.assertEqual(layouts.summary_parts(absurd)[3][0], "9.9KW")
        dark = layouts.summary_parts([layouts.Node("studio1"), layouts.Node("studio2", layouts.STALE)])
        self.assertEqual([part and part[0] for part in dark], ["G--", None, "M--", None, "D0", "P0"])
        self.assertEqual((dark[0][1], dark[2][1]), (layouts.DIM, layouts.DIM))


class EndToEnd(unittest.TestCase):
    """agent report → board → relay → the frame a panel would stream."""

    @classmethod
    def setUpClass(cls):
        pxtrelay.Handler.key = RELAY_KEY
        cls.relay = ThreadingHTTPServer(("127.0.0.1", 0), pxtrelay.Handler)
        threading.Thread(target=cls.relay.serve_forever, daemon=True).start()
        cls.relay_url = f"http://127.0.0.1:{cls.relay.server_address[1]}"
        cls.board = boardmod.Board(["studio1", "studio2", "studio3", "studio4"], pinned="rows")
        boardmod.Handler.board, boardmod.Handler.key = cls.board, KEY
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), boardmod.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.sender = boardmod.Relay(cls.relay_url, RELAY_KEY)

    @classmethod
    def tearDownClass(cls):
        cls.relay.shutdown()
        cls.srv.shutdown()

    def test_ingest_needs_the_key_and_a_known_node(self):
        self.assertEqual(call(self.url + "/ingest", body=report("studio1"))[0], 401)
        self.assertEqual(call(self.url + "/ingest", "wrong" * 5, body=report("studio1"))[0], 401)
        self.assertEqual(call(self.url + "/ingest", KEY, body=report("rogue"))[0], 400)
        self.assertEqual(call(self.url + "/ingest", KEY, raw=b"[1]")[0], 400)
        self.assertEqual(call(self.url + "/ingest", KEY, raw=b"x" * 5000)[0], 413)
        self.assertEqual(call(self.url + "/ingest", KEY, body=report("studio1"))[0], 200)

    def test_frame_reaches_the_relay_once_and_only_when_it_changes(self):
        call(self.url + "/ingest", KEY, body=report("studio1", gpu=91))
        self.board.tick()
        self.assertTrue(self.sender.push(self.board.frame()[1]))
        self.assertFalse(self.sender.push(self.board.frame()[1]))
        status, raw = call(self.relay_url + "/latest", RELAY_KEY)
        frame = base64.b64decode(json.loads(raw)["frame"])
        self.assertEqual(len(frame), 128 * 64 * 2)
        self.assertTrue(any(frame))

    def test_relay_down_is_survived_and_retried(self):
        down = boardmod.Relay("http://127.0.0.1:9", RELAY_KEY)
        self.assertFalse(down.push(self.board.frame()[1]))
        self.assertTrue(down.failing)
        self.assertIsNone(down.last)

    def test_layout_pin_preview_and_state(self):
        self.assertEqual(call(self.url + "/layout", KEY, body={"layout": "cols"})[0], 200)
        self.assertEqual(call(self.url + "/layout", KEY, body={"layout": "bogus"})[0], 400)
        self.assertEqual(call(self.url + "/layout", body={"layout": "rows"})[0], 401)
        status, raw = call(self.url + "/state")
        self.assertEqual(json.loads(raw)["layout"], "cols")
        status, body = call(self.url + "/preview/cols.png?scale=2")
        self.assertEqual((status, body[:4]), (200, b"\x89PNG"))
        self.assertEqual(call(self.url + "/preview/none.png")[0], 404)
        self.assertEqual(call(self.url + "/")[0], 200)
        self.board.pin("rows")


class LiveNumbers(unittest.TestCase):
    def node(self, state, **kw):
        base = dict(name="studio1", state=state, gpu=80.0, mem=50.0, dec=0.0, pre=0.0, dec_last=120.0, pre_last=2400.0)
        base.update(kw)
        return layouts.Node(**base)

    def test_a_busy_node_with_a_stopped_phase_shows_zero_not_the_last_average(self):
        n = self.node(layouts.LIVE, dec=44.4, pre=0.0)
        self.assertEqual(layouts.shown(n, "pre", layouts.PRE_C), ("0", layouts.dim(layouts.PRE_C, 0.6)))
        n = self.node(layouts.LIVE, dec=0.0, pre=1289.5)
        self.assertEqual(layouts.shown(n, "dec", layouts.DEC_C), ("0", layouts.dim(layouts.DEC_C, 0.6)))

    def test_the_cluster_total_zero_keeps_its_metric_colour(self):
        self.assertEqual(layouts.total_text(0.0, layouts.PRE_C), ("P0", layouts.dim(layouts.PRE_C, 0.6)))
        self.assertEqual(layouts.total_text(0.0, layouts.DEC_C), ("D0", layouts.dim(layouts.DEC_C, 0.6)))

    def zero_pixels(self, name, dim_by=1.0):
        """Pixels of the zero's colour that the rendered zero glyph adds. The baseline render is the same
        node with the zero's text blanked, so the `P` label keeps its colour in both and cancels out."""
        import board as boardmod
        from unittest import mock
        from PIL import Image
        swatch = Image.new("RGB", (1, 1), layouts.dim(layouts.PRE_C, 0.6))
        want = boardmod.dim_image(swatch, dim_by).getpixel((0, 0))
        real = layouts.shown

        def blanked(node, key, color):
            text, col = real(node, key, color)
            return ("" if key == "pre" and text == "0" else text), col

        counts = []
        for patch in (None, blanked):
            nodes = [self.node(layouts.LIVE, dec=44.4, pre=0.0)] + [layouts.Node(f"studio{i}") for i in (2, 3, 4)]
            with mock.patch.object(layouts, "shown", patch or real):
                img = boardmod.dim_image(layouts.render(name, nodes), dim_by)
            counts.append(sum(n for n, c in img.getcolors() if c == want))
        return counts[0] - counts[1], want

    def test_every_layout_draws_a_zero_prefill_in_a_colour_the_panel_can_show(self):
        for name in layouts.LAYOUTS:
            added, _ = self.zero_pixels(name)
            self.assertGreater(added, 0, name)

    def test_a_zero_survives_the_night_dimming_as_something_the_panel_can_show(self):
        for name in layouts.LAYOUTS:
            added, want = self.zero_pixels(name, 0.25)
            self.assertGreater(added, 0, name)
        self.assertGreaterEqual(max(want), 30)

    def test_a_busy_node_with_no_live_figure_shows_dashes(self):
        n = self.node(layouts.LIVE, dec=None, pre=None)
        self.assertEqual(layouts.shown(n, "dec", layouts.DEC_C)[0], "--")

    def test_an_idle_node_still_shows_its_last_average_dimmed(self):
        n = self.node(layouts.IDLE)
        text, colour = layouts.shown(n, "dec", layouts.DEC_C)
        self.assertEqual((text, colour), ("120", layouts.dim(layouts.DEC_C, 0.6)))

    def test_an_idle_node_with_no_fresh_average_reads_zero_and_with_no_figure_dashes(self):
        zero = self.node(layouts.IDLE, dec_last=None, pre_last=None)
        self.assertEqual(layouts.shown(zero, "pre", layouts.PRE_C), ("0", layouts.dim(layouts.PRE_C, 0.6)))
        nofig = self.node(layouts.IDLE, dec=None, pre=None, dec_last=None, pre_last=None)
        self.assertEqual(layouts.shown(nofig, "pre", layouts.PRE_C), ("--", layouts.DIM))

    def test_a_live_figure_shows_in_the_metric_colour(self):
        n = self.node(layouts.LIVE, dec=44.4, pre=1289.5)
        self.assertEqual(layouts.shown(n, "pre", layouts.PRE_C), ("1.3K", layouts.PRE_C))

    def box(self, img, colour, x0, x1, y0, y1):
        px = img.load()
        pts = [(x, y) for x in range(x0, x1) for y in range(y0, y1) if px[x, y] == colour]
        self.assertTrue(pts, "no pixels of that colour")
        return (min(p[0] for p in pts), max(p[0] for p in pts), min(p[1] for p in pts), max(p[1] for p in pts))

    def test_focus_draws_decode_and_prefill_numbers_at_one_size_and_height(self):
        nodes = [self.node(layouts.LIVE, dec=118.0, pre=2400.0)] + [layouts.Node(f"studio{i}") for i in (2, 3, 4)]
        img = layouts.render("focus", nodes)
        dec = self.box(img, layouts.DEC_C, 0, 64, 8, 29)
        pre = self.box(img, layouts.PRE_C, 64, 128, 8, 29)
        self.assertEqual((dec[2], dec[3]), (pre[2], pre[3]))

    def test_a_rate_never_prints_more_than_four_characters(self):
        for v in (0, 0.04, 9.95, 999.5, 9949.9, 9950.0, 9999.5, 9999.99, 99999.5, 999_000, 9_999_500, 1e6, 1e9):
            self.assertLessEqual(len(layouts.rate(float(v))), 4, v)
        self.assertEqual(layouts.rate(1e9), "999K")

    def test_the_widest_focus_numbers_stay_in_their_own_half(self):
        nodes = [self.node(layouts.LIVE, dec=9999.0, pre=9999.0)] + [layouts.Node(f"studio{i}") for i in (2, 3, 4)]
        self.assertEqual(layouts.rate(9999.0), "10K")
        img = layouts.render("focus", nodes)
        dec = self.box(img, layouts.DEC_C, 0, 128, 8, 29)
        pre = self.box(img, layouts.PRE_C, 0, 128, 8, 29)
        self.assertLess(dec[1], 66)
        self.assertGreaterEqual(pre[0], 66)
        self.assertEqual(dec[1] - dec[0], pre[1] - pre[0])    # the same text at the same size: neither is clipped


class NightDim(unittest.TestCase):
    NY = "America/New_York"

    def at(self, dimmer, iso):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return dimmer.factor(datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo(self.NY)).timestamp())

    def test_dim_from_the_start_until_the_end_the_end_is_not_dimmed(self):
        d = boardmod.Dimmer("20:00-07:30@0.25", self.NY)
        got = [self.at(d, f"2026-11-10T{h}") for h in
               ("19:59", "20:00", "23:59", "00:00", "03:15", "07:29", "07:30", "12:00")]
        self.assertEqual(got, [1.0, 0.25, 0.25, 0.25, 0.25, 0.25, 1.0, 1.0])

    def test_a_window_inside_one_day_works_too(self):
        d = boardmod.Dimmer("12:00-13:00@0.5", self.NY)
        self.assertEqual([self.at(d, f"2026-11-10T{h}") for h in ("11:59", "12:00", "12:59", "13:00")],
                         [1.0, 0.5, 0.5, 1.0])

    def test_the_window_follows_local_time_through_daylight_saving(self):
        d = boardmod.Dimmer("20:00-07:30@0.25", self.NY)
        self.assertEqual(self.at(d, "2026-07-01T20:00"), 0.25)    # EDT, UTC-4
        self.assertEqual(self.at(d, "2026-07-01T19:59"), 1.0)
        self.assertEqual(self.at(d, "2026-12-01T20:00"), 0.25)    # EST, UTC-5
        self.assertEqual(self.at(d, "2026-12-01T19:59"), 1.0)

    def test_a_bad_spec_or_zone_is_refused_with_a_reason(self):
        for spec in ("", "20:00", "20:00-07:30", "25:00-07:30@0.25", "20:60-07:30@0.25", "20:00-20:00@0.25",
                     "20:00-07:30@0", "20:00-07:30@1.5", "20:00-07:30@dim"):
            with self.assertRaises(ValueError, msg=spec):
                boardmod.Dimmer(spec, self.NY)
        with self.assertRaises(ValueError):
            boardmod.Dimmer("20:00-07:30@0.25", "Mars/Olympus")

    def test_dimming_scales_every_channel_and_leaves_black_black(self):
        from PIL import Image
        img = Image.new("RGB", (2, 1))
        img.putpixel((0, 0), (200, 100, 50))
        dimmed = boardmod.dim_image(img, 0.25)
        self.assertEqual((dimmed.getpixel((0, 0)), dimmed.getpixel((1, 0))), ((59, 29, 15), (0, 0, 0)))
        self.assertEqual(img.getpixel((0, 0)), (200, 100, 50))

    def test_full_brightness_sends_the_frame_untouched(self):
        img = layouts.render("rows", preview.busy())
        self.assertEqual(boardmod.dim_image(img, 1.0).tobytes(), img.tobytes())

    def test_a_step_pushes_the_dimmed_frame_and_pushes_again_when_the_level_changes(self):
        class Spy:
            def __init__(self):
                self.sent = []

            def push(self, img):
                self.sent.append(img.tobytes())

        board = boardmod.Board(["studio1"], clock=Clock())
        board.ingest(report("studio1"))
        d, spy = boardmod.Dimmer("20:00-07:30@0.25", self.NY), Spy()
        from datetime import datetime
        from zoneinfo import ZoneInfo
        epoch = lambda iso: datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo(self.NY)).timestamp()
        boardmod.step(board, spy, d, epoch("2026-11-10T19:59"))
        boardmod.step(board, spy, d, epoch("2026-11-10T20:00"))
        bright, dim = spy.sent
        self.assertEqual(dim, boardmod.dim_image(board.frame()[1], 0.25).tobytes())
        self.assertGreater(sum(bright), sum(dim) * 2)

    def test_the_render_loop_hands_the_dimmer_to_every_step(self):
        class Spy:
            sent = []

            def push(self, img):
                self.sent.append(img.tobytes())

        class Half:
            floor = 0

            def factor(self, now):
                return 0.5

        board = boardmod.Board(["studio1"], clock=Clock())
        board.ingest(report("studio1"))
        spy, real_tick = Spy(), boardmod.TICK_S
        boardmod.TICK_S = 0.01
        try:
            threading.Thread(target=boardmod.render_loop, args=(board, spy, Half()), daemon=True).start()
            deadline = time.time() + 3
            while not spy.sent and time.time() < deadline:
                time.sleep(0.01)
        finally:
            boardmod.TICK_S = real_tick
        self.assertEqual(spy.sent[0], boardmod.dim_image(board.frame()[1], 0.5).tobytes())
        self.assertNotEqual(spy.sent[0], board.frame()[1].tobytes())


class TpMirror(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.board = boardmod.Board(["studio1", "studio2", "studio3", "studio4"], clock=self.clock)

    def leader(self, **kw):
        self.board.ingest(report("studio1", running=1, **kw))

    def worker(self, name, follows="studio1", **kw):
        body = report(name, dec=0.0, pre=0.0, dec_last=None, pre_last=None, dec_last_age=None,
                      pre_last_age=None, worker=True, follows=follows)
        body.update(kw)
        self.board.ingest(body)

    def view(self, name):
        return {n.name: n for n in self.board.nodes()}[name]

    def test_a_worker_shows_its_leaders_live_rates_and_state(self):
        self.leader(dec=118.4, pre=2400.0)
        self.worker("studio2")
        n = self.view("studio2")
        self.assertEqual((n.dec, n.pre, n.state, n.tp), (118.4, 2400.0, layouts.LIVE, "studio1"))
        self.assertEqual((n.dec_last, n.pre_last), (50.0, 2000.0))

    def test_the_cluster_total_counts_each_token_once(self):
        self.leader(dec=100.0, pre=3000.0)
        self.worker("studio2")
        self.worker("studio3")
        self.assertEqual(layouts.totals(self.board.nodes()), (100.0, 3000.0))

    def test_without_a_named_leader_the_busiest_serving_node_leads(self):
        self.leader(dec=90.0)
        self.board.ingest(report("studio3", dec=0.0, pre=0.0, running=0, dec_last_age=None, pre_last_age=None))
        self.worker("studio2", follows=None)
        self.assertEqual((self.view("studio2").dec, self.view("studio2").tp), (90.0, "studio1"))

    def test_a_stale_leader_leaves_the_worker_on_its_own_numbers(self):
        self.leader(dec=90.0)
        self.clock.t += 60
        self.worker("studio2")
        n = self.view("studio2")
        self.assertEqual((n.dec, n.tp), (0.0, None))

    def test_a_worker_never_follows_a_worker_or_itself(self):
        self.worker("studio2", follows="studio3")
        self.worker("studio3", follows="studio2")
        self.worker("studio4", follows="studio4")
        self.assertEqual([self.view(n).tp for n in ("studio2", "studio3", "studio4")], [None, None, None])

    def test_a_node_that_is_not_a_worker_ignores_follows(self):
        self.leader(dec=90.0)
        self.board.ingest(report("studio2", dec=7.0, running=1, follows="studio1"))
        n = self.view("studio2")
        self.assertEqual((n.dec, n.tp), (7.0, None))

    def test_junk_follows_is_dropped(self):
        self.leader(dec=90.0)
        self.worker("studio2", follows="../../etc")
        self.worker("studio3", follows=7)
        self.assertEqual((self.view("studio2").tp, self.view("studio3").tp), ("studio1", "studio1"))


class TpHistory(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.board = boardmod.Board(["studio1", "studio2", "studio3", "studio4"], clock=self.clock)

    def ingest(self, name, worker=False, dec=100.0, running=1):
        self.board.ingest(report(name, dec=dec, pre=0.0, running=running, worker=worker, follows="studio1" if worker else None))

    def step(self, leader=True, workers=True):
        self.ingest("studio1", dec=100.0)
        self.ingest("studio2", worker=workers, dec=0.0 if workers else 0.0, running=0)
        self.ingest("studio3", worker=workers, dec=0.0, running=0)
        self.board.tick()
        self.clock.t += 1

    def test_every_layout_draws_history_the_board_recorded(self):
        for _ in range(3):
            self.step(workers=True)
        for name in layouts.LAYOUTS:
            self.assertEqual(layouts.render(name, self.board.nodes()).size, (128, 64), name)

    def test_the_board_records_decode_gpu_and_prefill_samples(self):
        self.step(workers=False)
        self.assertEqual(self.board.nodes()[0].hist[-1], (100.0, 80.0, 0.0))


class TpWatchTests(unittest.TestCase):
    WORKER = ("/Users/s/opt/mlx-serve-main/zig-out/bin/mlx-serve-main worker --rank 1 --size 2 "
              "--coordinator 10.254.12.1:7000 --device rdma_en5 --model-dir /Users/s/models-big")

    def test_a_box_that_serves_is_never_a_worker(self):
        watch = agent.TpWatch("studio2")
        self.assertEqual(watch.check(10.0, True, lambda: [self.WORKER]), {"worker": False, "follows": None})
        self.assertEqual(watch.check(20.0, False, lambda: [self.WORKER])["worker"], True)

    def test_a_failed_ps_keeps_the_last_role(self):
        watch = agent.TpWatch("studio2")
        self.assertTrue(watch.check(10.0, False, lambda: [self.WORKER])["worker"])
        self.assertTrue(watch.check(20.0, False, lambda: None)["worker"])

    def test_ps_runs_at_most_once_per_poll_interval(self):
        calls = []
        watch = agent.TpWatch("studio2")
        for t in (10.0, 10.5, 12.0, 15.1):
            watch.check(t, False, lambda: calls.append(t) or [])
        self.assertEqual(calls, [10.0, 15.1])

    def test_a_launcher_line_that_carries_a_workers_argv_is_not_a_worker(self):
        for line in ("ssh studio2 " + self.WORKER, "sh -c '" + self.WORKER + "'", "/usr/bin/env HOME=/x " + self.WORKER):
            self.assertFalse(agent.tp_role([line])["worker"], line)

    def test_a_box_never_follows_itself(self):
        self.assertEqual(agent.tp_role([self.WORKER], "studio1"), {"worker": False, "follows": None})


class AgentTp(unittest.TestCase):
    WORKER = ("/Users/s/opt/mlx-serve-main/zig-out/bin/mlx-serve-main worker --rank 1 --size 2 "
              "--coordinator 10.254.12.1:7000 --device rdma_en5 --model-dir /Users/s/models-big")

    def test_a_worker_process_names_the_leader_by_its_cable_address(self):
        self.assertEqual(agent.tp_role([self.WORKER]), {"worker": True, "follows": "studio1"})

    def test_the_last_octet_of_the_coordinator_is_the_leaders_studio_number(self):
        line = self.WORKER.replace("10.254.12.1", "10.254.34.3")
        self.assertEqual(agent.tp_role([line])["follows"], "studio3")

    def test_a_coordinator_off_the_mesh_still_marks_a_worker(self):
        line = self.WORKER.replace("10.254.12.1", "192.168.86.151")
        self.assertEqual(agent.tp_role([line]), {"worker": True, "follows": None})

    def test_a_server_or_unrelated_process_is_not_a_worker(self):
        serve = "/Users/s/.local/bin/mlx-serve --serve --host 0.0.0.0 --port 11234 --metrics"
        other = "/usr/bin/python3 worker.py --coordinator 10.254.12.1:7000"
        self.assertEqual(agent.tp_role([serve, other, ""]), {"worker": False, "follows": None})


class PeakLearning(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.p = peaks.Peaks(clock=self.clock)
        self.tick = 0

    def observe(self, key, metric, value, skip=0, wall=1.0):
        """One observation on the next tick, or `skip` ticks later; `wall` seconds pass."""
        self.tick += 1 + skip
        self.p.observe(key, metric, value, self.tick)
        self.clock.t += wall

    def feed(self, key, metric, values, **kw):
        for v in values:
            self.observe(key, metric, v, **kw)

    def assertPeak(self, key, metric, seed, want):
        self.assertAlmostEqual(self.p.get(key, metric, seed), want, delta=0.1)

    def test_the_seed_applies_until_enough_has_been_watched(self):
        self.feed("studio1/1", "dec", [80.0] * (peaks.LEARN_N + peaks.HOLD - 2))
        self.assertPeak("studio1/1", "dec", 150.0, 150.0)
        self.feed("studio1/1", "dec", [80.0])
        self.assertPeak("studio1/1", "dec", 150.0, 80.0)

    def test_the_learned_peak_is_the_highest_value_that_held(self):
        self.feed("a/1", "dec", [50.0] * 70 + [90.0, 90.0, 90.0] + [60.0] * 5)
        self.assertPeak("a/1", "dec", 150.0, 90.0)

    def test_a_spike_shorter_than_the_hold_does_not_set_the_peak(self):
        self.feed("a/1", "dec", [50.0] * 70 + [400.0, 400.0] + [50.0] * 5)
        self.assertPeak("a/1", "dec", 150.0, 50.0)

    def test_a_pause_breaks_the_hold(self):
        self.feed("a/1", "dec", [50.0] * 70 + [400.0, 400.0, 0.0, 400.0, 400.0, 0.0, 400.0])
        self.assertPeak("a/1", "dec", 150.0, 50.0)

    def test_a_skipped_tick_breaks_the_hold_however_little_time_passed(self):
        self.feed("a/1", "dec", [50.0] * 70)
        self.feed("a/1", "dec", [400.0, 400.0], wall=0.1)
        self.observe("a/1", "dec", 400.0, skip=1, wall=0.1)
        self.assertPeak("a/1", "dec", 150.0, 50.0)

    def test_consecutive_ticks_hold_however_much_time_passes_between_them(self):
        self.feed("a/1", "dec", [50.0] * 70)
        self.feed("a/1", "dec", [90.0] * 3, wall=1.7)         # a slow relay push
        self.assertPeak("a/1", "dec", 150.0, 90.0)
        self.feed("a/1", "dec", [120.0] * 3, wall=45.0)       # a very slow one
        self.assertPeak("a/1", "dec", 150.0, 120.0)

    def test_a_change_of_key_ends_the_run_even_if_it_comes_back_the_next_tick(self):
        self.feed("a/1", "dec", [50.0] * 70)
        self.feed("a/1", "dec", [400.0, 400.0])
        self.observe("a/2", "dec", 400.0)                     # a worker joined for one tick
        self.observe("a/1", "dec", 400.0)
        self.assertPeak("a/1", "dec", 150.0, 50.0)

    def test_keys_and_metrics_keep_separate_runs(self):
        self.feed("a/1", "dec", [50.0] * 70)
        self.feed("b/1", "dec", [50.0] * 70)
        for tick_of in range(3):
            self.tick += 1
            self.p.observe("a/1", "dec", 90.0, self.tick)
            self.p.observe("b/1", "dec", 400.0 if tick_of != 1 else 0.0, self.tick)
            self.clock.t += 1
        self.assertPeak("a/1", "dec", 150.0, 90.0)
        self.assertPeak("b/1", "dec", 150.0, 50.0)

    def test_the_peak_halves_every_half_life(self):
        self.feed("a/1", "dec", [100.0] * 70)
        self.clock.t += peaks.HALF_LIFE_S
        self.assertAlmostEqual(self.p.get("a/1", "dec", 150.0), 50.0, delta=0.2)

    def test_a_new_high_replaces_a_decayed_peak(self):
        self.feed("a/1", "dec", [100.0] * 70)
        self.clock.t += peaks.HALF_LIFE_S
        self.feed("a/1", "dec", [80.0] * 4)
        self.assertPeak("a/1", "dec", 150.0, 80.0)

    def test_metrics_and_groups_learn_separately(self):
        self.feed("a/1", "dec", [40.0] * 70)
        self.feed("a/4", "dec", [150.0] * 70)
        self.feed("a/1", "pre", [900.0] * 70)
        self.assertPeak("a/1", "dec", 1.0, 40.0)
        self.assertPeak("a/4", "dec", 1.0, 150.0)
        self.assertPeak("a/1", "pre", 1.0, 900.0)


class PeakPersistence(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="pxt-peaks-")
        self.path = os.path.join(self.dir, "peaks.json")
        self.clock = Clock()

    def learned(self):
        p = peaks.Peaks(self.path, clock=self.clock)
        for tick in range(1, peaks.LEARN_N + 6):
            p.observe("studio1/4", "dec", 77.0, tick)
            self.clock.t += 1
        return p

    def test_a_saved_peak_survives_a_restart(self):
        p = self.learned()
        self.clock.t += peaks.SAVE_EVERY_S
        self.assertTrue(p.save_if_due())
        again = peaks.Peaks(self.path, clock=self.clock)
        self.assertAlmostEqual(again.get("studio1/4", "dec", 150.0), 77.0, delta=0.1)

    def test_saving_is_rate_limited_and_only_when_something_changed(self):
        p = self.learned()
        self.clock.t += peaks.SAVE_EVERY_S
        self.assertTrue(p.save_if_due())
        self.assertFalse(p.save_if_due())
        for tick in range(1000, 1000 + peaks.HOLD):       # a held run changes the peak
            p.observe("studio1/4", "dec", 77.0, tick)
            self.clock.t += 1
        self.assertFalse(p.save_if_due())     # changed, but saved only a moment ago
        self.clock.t += peaks.SAVE_EVERY_S
        self.assertTrue(p.save_if_due())
        self.clock.t += peaks.SAVE_EVERY_S
        self.assertFalse(p.save_if_due())     # nothing since: no write

    def test_a_corrupt_or_foreign_file_is_ignored_not_fatal(self):
        bodies = ("not json", "[]", '{"peaks": {"a|dec": "x", "b": [1,2,3]}}',
                  '{"peaks": {"a|dec": [-5, 0, 99]}}', '{"peaks": {"a|dec": [true, 0, 99]}}',
                  '{"peaks": {"a|dec": [0, 0, 99]}}', '{"peaks": {"a|dec": [1, 0, Infinity]}}',
                  '{"peaks": {"a|dec": [NaN, 0, 99]}}', '{"peaks": {"a|dec": [Infinity, 0, 99]}}',
                  '{"peaks": {"a|dec": [1, NaN, 99]}}', '{"peaks": {"a|dec": [1, 0, ' + "9" * 400 + ']}}',
                  '{"peaks": {"a|dec": [' + "9" * 400 + ', 0, 99]}}')
        for body in bodies:
            with open(self.path, "w") as f:
                f.write(body)
            self.assertEqual(peaks.Peaks(self.path, clock=self.clock).get("a", "dec", 150.0), 150.0, body)

    def test_an_unwritable_path_is_logged_and_does_not_raise(self):
        p = peaks.Peaks(os.path.join(self.dir, "no", "such", "dir", "peaks.json"), clock=self.clock)
        for tick in range(1, peaks.HOLD + 1):
            p.observe("a", "dec", 5.0, tick)
        self.clock.t += peaks.SAVE_EVERY_S
        self.assertFalse(p.save_if_due())

    def test_the_file_is_replaced_atomically(self):
        p = self.learned()
        self.clock.t += peaks.SAVE_EVERY_S
        p.save_if_due()
        self.assertEqual(sorted(os.listdir(self.dir)), ["peaks.json"])


class BoardPeaks(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.board = boardmod.Board(["studio1", "studio2", "studio3", "studio4"], clock=self.clock,
                                    peaks=peaks.Peaks(clock=self.clock))

    def run_job(self, dec, seconds, workers=(), idle=()):
        for _ in range(seconds):
            self.board.ingest(report("studio1", dec=dec, pre=0.0, running=1, dec_last=None, pre_last=None,
                                     dec_last_age=None, pre_last_age=None))
            for w in workers:
                self.board.ingest(report(w, dec=0.0, pre=0.0, running=None, worker=True, follows="studio1"))
            for w in idle:      # the same boxes once the job has ended: still reporting, no longer workers
                self.board.ingest(report(w, dec=0.0, pre=0.0, running=0, dec_last=None, pre_last=None,
                                         dec_last_age=None, pre_last_age=None))
            self.board.tick()
            self.clock.t += 1

    def view(self, name):
        return {n.name: n for n in self.board.nodes()}[name]

    def test_a_solo_node_learns_its_own_peak(self):
        self.run_job(60.0, peaks.LEARN_N + 10)
        self.assertAlmostEqual(self.view("studio1").dec_peak, 60.0, delta=0.1)

    def test_a_prefill_peak_is_learned_beside_the_decode_peak(self):
        for _ in range(peaks.LEARN_N + 10):
            self.board.ingest(report("studio1", dec=60.0, pre=1800.0, running=1, dec_last=None, pre_last=None,
                                     dec_last_age=None, pre_last_age=None))
            self.board.tick()
            self.clock.t += 1
        n = self.view("studio1")
        self.assertAlmostEqual(n.pre_peak, 1800.0, delta=2.0)
        self.assertAlmostEqual(n.dec_peak, 60.0, delta=0.5)

    def test_the_peak_is_unknown_until_learned(self):
        self.run_job(60.0, 5)
        self.assertIsNone(self.view("studio1").dec_peak)

    def test_an_idle_tick_between_two_busy_stretches_does_not_join_them(self):
        self.run_job(50.0, peaks.LEARN_N + 10)
        for dec, running in ((400.0, 1), (400.0, 1), (0.0, 0), (400.0, 1)):
            self.board.ingest(report("studio1", dec=dec, pre=0.0, running=running, dec_last=None, pre_last=None,
                                     dec_last_age=None, pre_last_age=None))
            self.board.tick()
            self.clock.t += 1
        self.assertAlmostEqual(self.view("studio1").dec_peak, 50.0, delta=0.5)

    def test_a_delayed_observe_does_not_join_the_runs_either_side_of_an_idle_tick(self):
        self.run_job(50.0, peaks.LEARN_N + 10)
        live = dict(pre=0.0, running=1, dec_last=None, pre_last=None, dec_last_age=None, pre_last_age=None)
        idle = dict(live, running=0)
        for dt, dec, body in ((0.0, 400.0, live), (0.8, 400.0, live), (0.2, 0.0, idle), (1.0, 400.0, live)):
            self.clock.t += dt
            self.board.ingest(report("studio1", dec=dec, **body))
            self.board.tick()
        self.assertAlmostEqual(self.view("studio1").dec_peak, 50.0, delta=0.5)

    def test_a_worker_joining_for_one_tick_does_not_join_the_solo_runs_either_side(self):
        self.run_job(50.0, peaks.LEARN_N + 10)
        for workers in ((), (), ("studio2",), ()):
            self.run_job(400.0, 1, workers=workers, idle=() if workers else ("studio2",))
        self.assertAlmostEqual(self.view("studio1").dec_peak, 50.0, delta=0.5)

    def test_a_slow_relay_push_between_ticks_does_not_stop_a_peak_being_learned(self):
        for _ in range(peaks.LEARN_N + 10):
            self.board.ingest(report("studio1", dec=60.0, pre=0.0, running=1, dec_last=None, pre_last=None,
                                     dec_last_age=None, pre_last_age=None))
            self.board.tick()
            self.clock.t += 1.7
            self.board.ingest(report("studio1", dec=60.0, pre=0.0, running=1, dec_last=None, pre_last=None,
                                     dec_last_age=None, pre_last_age=None))
        self.assertAlmostEqual(self.view("studio1").dec_peak, 60.0, delta=0.5)

    def test_a_node_that_turns_into_a_worker_for_a_tick_does_not_join_its_runs(self):
        self.run_job(50.0, peaks.LEARN_N + 10)
        steps = [("studio1", 400.0, False), ("studio1", 400.0, False), ("studio1", 400.0, True), ("studio1", 400.0, False)]
        for _, dec, worker in steps:
            self.board.ingest(report("studio3", dec=400.0, pre=0.0, running=1, dec_last=None, pre_last=None,
                                     dec_last_age=None, pre_last_age=None))
            self.board.ingest(report("studio1", dec=dec, pre=0.0, running=1, dec_last=None, pre_last=None,
                                     dec_last_age=None, pre_last_age=None, worker=worker, follows="studio3" if worker else None))
            self.board.tick()
            self.clock.t += 1
        self.assertAlmostEqual(self.view("studio1").dec_peak, 50.0, delta=0.5)

    def test_workers_add_no_observations_of_their_own(self):
        self.run_job(44.0, peaks.LEARN_N // 2 + peaks.HOLD, workers=("studio2", "studio3", "studio4"))
        self.assertIsNone(self.view("studio1").dec_peak)

    def test_a_stale_node_carries_no_peak(self):
        self.run_job(60.0, peaks.LEARN_N + 10)
        self.clock.t += 60
        self.assertEqual(self.view("studio1").state, layouts.STALE)
        self.assertIsNone(self.view("studio1").dec_peak)

    def test_the_render_loop_step_saves_the_peaks_when_due(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(prefix="pxt-state-"), "peaks.json")
        board = boardmod.Board(["studio1"], clock=self.clock, peaks=peaks.Peaks(path, clock=self.clock))
        for _ in range(peaks.LEARN_N + 10):
            board.ingest(report("studio1", dec=50.0, pre=0.0, running=1))
            boardmod.step(board, None)
            self.clock.t += 1
        with open(path) as f:
            self.assertIn("studio1/1|dec", json.load(f)["peaks"])

    def test_a_tp_group_is_learned_under_its_size_and_workers_share_it(self):
        self.run_job(44.0, peaks.LEARN_N + 10, workers=("studio2", "studio3", "studio4"))
        for n in ("studio1", "studio2", "studio3", "studio4"):
            self.assertAlmostEqual(self.view(n).dec_peak, 44.0, delta=0.1)
        self.run_job(120.0, peaks.LEARN_N + 10, idle=("studio2", "studio3", "studio4"))
        self.assertAlmostEqual(self.view("studio1").dec_peak, 120.0, delta=0.1)
        self.run_job(44.0, 1, workers=("studio2", "studio3", "studio4"))
        self.assertAlmostEqual(self.view("studio2").dec_peak, 44.0, delta=0.1)


class RangeBars(unittest.TestCase):
    def node(self, dec, pre, dec_peak=None, pre_peak=None, state=layouts.LIVE):
        return layouts.Node("studio1", state, gpu=80.0, mem=50.0, dec=dec, pre=pre, dec_peak=dec_peak, pre_peak=pre_peak)

    def bar(self, img, colour, x0, x1, y=37):
        px = img.load()
        return sum(1 for x in range(x0, x1) if px[x, y] == colour)

    def render(self, node):
        return layouts.render("focus", [node] + [layouts.Node(f"studio{i}") for i in (2, 3, 4)])

    def test_the_bars_show_the_rate_as_a_fraction_of_its_peak(self):
        img = self.render(self.node(40.0, 1500.0, dec_peak=200.0, pre_peak=3000.0))
        self.assertEqual(self.bar(img, layouts.DEC_C, 0, 64), round(layouts.RATE_BAR_W * 0.2))
        self.assertEqual(self.bar(img, layouts.PRE_C, 64, 128), round(layouts.RATE_BAR_W * 0.5))

    def test_a_rate_above_its_peak_fills_the_bar_and_zero_leaves_it_empty(self):
        img = self.render(self.node(300.0, 0.0, dec_peak=200.0, pre_peak=3000.0))
        self.assertEqual(self.bar(img, layouts.DEC_C, 0, 64), layouts.RATE_BAR_W)
        self.assertEqual(self.bar(img, layouts.PRE_C, 64, 128), 0)

    def test_an_unlearned_peak_uses_the_default_scale(self):
        img = self.render(self.node(75.0, 1500.0))
        self.assertEqual(self.bar(img, layouts.DEC_C, 0, 64), round(layouts.RATE_BAR_W * 75.0 / layouts.DEC_FULL))
        self.assertEqual(self.bar(img, layouts.PRE_C, 64, 128), round(layouts.RATE_BAR_W * 1500.0 / layouts.PRE_FULL))

    def test_the_gpu_and_memory_bars_still_draw_below_the_rate_bars(self):
        img = self.render(self.node(40.0, 0.0, dec_peak=200.0, pre_peak=3000.0))
        px = img.load()
        self.assertEqual(px[2, 40], layouts.GPU_C)
        self.assertEqual(px[2, 43], layouts.MEM_C)

    def test_an_idle_node_draws_empty_rate_bars(self):
        n = self.node(0.0, 0.0, dec_peak=200.0, pre_peak=3000.0, state=layouts.IDLE)
        n.dec_last, n.pre_last = 120.0, 2400.0
        img = self.render(n)
        self.assertEqual(self.bar(img, layouts.DEC_C, 0, 64) + self.bar(img, layouts.PRE_C, 64, 128), 0)


class AgentRates(unittest.TestCase):
    def metrics(self, gen, pl, gen_total, pre_total, dec_s, pre_s, start=1, running=1, prefilling=None):
        gauges = {"process_start_time_seconds": start, "generation_tokens_live": gen,
                  "prefill_tokens_live": pl, "requests_running": running}
        if prefilling is not None:
            gauges["requests_prefilling"] = prefilling
        return {"gauges": gauges,
                "counters": {"generation_tokens_total": gen_total, "prefill_tokens_total": pre_total},
                "histograms": {"decode_time_seconds": {"sum": dec_s}, "prefill_time_seconds": {"sum": pre_s}}}

    def feed(self, rates, *steps):
        out = None
        for t, m in steps:
            out = rates.update(m, t)
        return out

    def test_decode_rate_is_the_live_counter_growth(self):
        out = self.feed(agent.Rates(), (10.0, self.metrics(100, 0, 100, 0, 1, 0)),
                        (11.0, self.metrics(190, 0, 190, 0, 2, 0)))
        self.assertEqual(out["dec"], 90.0)
        self.assertTrue(out["serving"])

    def test_lumpy_counters_are_smoothed_not_flickering(self):
        r = agent.Rates()
        gen = [0, 0, 300, 300, 600, 600, 900]
        rates = [r.update(self.metrics(g, 0, g, 0, 1, 0), 10.0 + i)["dec"] for i, g in enumerate(gen)]
        self.assertTrue(all(v > 0 for v in rates[2:]), rates)

    def test_prefill_progress_reset_counts_only_increases(self):
        r = agent.Rates()
        out = self.feed(r, (10.0, self.metrics(0, 0, 0, 0, 0, 0)), (11.0, self.metrics(0, 4000, 0, 0, 0, 0)),
                        (12.0, self.metrics(0, 0, 0, 6000, 0, 2.0)))
        self.assertEqual(out["pre"], 2000.0)
        self.assertEqual(out["pre_last"], 3000.0)

    def test_prefill_reads_zero_the_moment_nothing_is_prefilling(self):
        r = agent.Rates()
        pre = lambda t, pl, total, s, prefilling: r.update(self.metrics(500, pl, 500, total, 5.0, s, running=1, prefilling=prefilling), t)
        pre(10.0, 0, 0, 0, 1)
        out = pre(11.0, 3000, 0, 0, 1)
        self.assertEqual(out["pre"], 3000.0)
        out = pre(12.0, 3000, 3000, 2.0, 0)
        self.assertEqual((out["pre"], out["pre_last"]), (0.0, 1500.0))
        out = pre(13.0, 3000, 3000, 2.0, 0)
        self.assertEqual(out["pre"], 0.0)

    def test_without_the_prefilling_gauge_the_windowed_rate_is_unchanged(self):
        out = self.feed(agent.Rates(), (10.0, self.metrics(0, 0, 0, 0, 0, 0)), (11.0, self.metrics(0, 4000, 0, 0, 0, 0)))
        self.assertEqual(out["pre"], 4000.0)

    def test_nothing_running_zeroes_live_rates_at_once(self):
        r = agent.Rates()
        out = self.feed(r, (10.0, self.metrics(0, 0, 0, 0, 0, 0)), (11.0, self.metrics(500, 0, 500, 0, 5.0, 0)),
                        (12.0, self.metrics(500, 0, 500, 0, 5.0, 0, running=0)))
        self.assertEqual((out["dec"], out["pre"]), (0.0, 0.0))
        self.assertEqual(out["dec_last"], 100.0)
        self.assertEqual(out["dec_last_age"], 1.0)

    def test_a_restarted_server_resets_the_baseline(self):
        out = self.feed(agent.Rates(), (10.0, self.metrics(9000, 0, 9000, 0, 90, 0, start=1)),
                        (11.0, self.metrics(30, 0, 30, 0, 0.3, 0, start=2)))
        self.assertEqual(out["dec"], 0.0)
        self.assertIsNone(out["dec_last"])

    def test_no_server_reports_null_rates_but_keeps_last(self):
        r = agent.Rates()
        self.feed(r, (10.0, self.metrics(0, 0, 0, 0, 0, 0)), (11.0, self.metrics(200, 0, 200, 0, 2.0, 0)))
        out = r.update(None, 12.0)
        self.assertEqual((out["dec"], out["pre"], out["serving"], out["dec_last"]), (None, None, False, 100.0))
        self.assertEqual((out["dec_last_age"], out["pre_last_age"]), (1.0, None))

    def test_a_second_request_with_the_same_average_refreshes_the_age(self):
        r = agent.Rates()
        self.feed(r, (10.0, self.metrics(0, 0, 0, 0, 0, 0)), (11.0, self.metrics(200, 0, 200, 0, 2.0, 0)))
        out = r.update(self.metrics(200, 0, 200, 0, 2.0, 0, running=0), 50.0)
        self.assertEqual((out["dec_last"], out["dec_last_age"]), (100.0, 39.0))
        out = r.update(self.metrics(400, 0, 400, 0, 4.0, 0), 51.0)
        self.assertEqual((out["dec_last"], out["dec_last_age"]), (100.0, 0.0))

    def test_an_average_never_measured_has_no_age(self):
        out = agent.Rates().update(self.metrics(0, 0, 0, 0, 0, 0), 10.0)
        self.assertEqual((out["dec_last"], out["dec_last_age"], out["pre_last_age"]), (None, None, None))

    def test_running_request_is_live_even_between_counter_lumps(self):
        board = boardmod.Board(["studio1"])
        board.ingest(report("studio1", dec=0.0, pre=0.0, running=1))
        self.assertEqual(board.nodes()[0].state, layouts.LIVE)

    def test_report_shape_matches_what_the_board_ingests(self):
        board = boardmod.Board(["studio1"])
        body = agent.report("studio1", agent.Rates().update(None, time.time()), 100.0, 256.0, 12)
        self.assertIsNone(board.ingest(body))
        self.assertEqual(board.nodes()[0].state, layouts.IDLE)


if __name__ == "__main__":
    unittest.main()
