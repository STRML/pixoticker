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

    def test_an_average_shows_until_its_age_passes_last_s_then_dashes(self):
        self.ingest_last(50.0, 2000.0, 5.0, 5.0)
        self.assertEqual(self.last(), (50.0, 2000.0))
        self.ingest_last(50.0, 2000.0, boardmod.LAST_S + 1.0, boardmod.LAST_S + 1.0)
        self.assertEqual(self.last(), (None, None))
        self.assertEqual(layouts.shown(self.board.nodes()[0], "dec", layouts.DEC_C)[0], "--")

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

        def flaky(board, relay):
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

    def test_total_history_adds_samples_newest_aligned_and_skips_silent_nodes(self):
        a = layouts.Node("studio1", layouts.LIVE, hist=[(10, 0, 0), (20, 0, 0), (30, 0, 0)])
        b = layouts.Node("studio2", layouts.IDLE, hist=[(1, 0, 0), (2, 0, 0)])
        gone = layouts.Node("studio3", layouts.STALE, hist=[(500, 0, 0)] * 3)
        self.assertEqual(layouts.total_history([a, b, gone]), [10, 21, 32])

    def test_spark_and_focus_draw_the_total_row(self):
        for name in ("spark", "focus"):
            img = layouts.render(name, preview.busy())
            top = img.crop((0, 0, 128, 7))
            self.assertIn(layouts.DEC_C, [c for _, c in top.getcolors()], name)


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
        self.assertEqual(layouts.shown(n, "pre", layouts.PRE_C), ("0", layouts.DIM))
        n = self.node(layouts.LIVE, dec=0.0, pre=1289.5)
        self.assertEqual(layouts.shown(n, "dec", layouts.DEC_C), ("0", layouts.DIM))

    def test_a_busy_node_with_no_live_figure_shows_dashes(self):
        n = self.node(layouts.LIVE, dec=None, pre=None)
        self.assertEqual(layouts.shown(n, "dec", layouts.DEC_C)[0], "--")

    def test_an_idle_node_still_shows_its_last_average_dimmed(self):
        n = self.node(layouts.IDLE)
        text, colour = layouts.shown(n, "dec", layouts.DEC_C)
        self.assertEqual((text, colour), ("120", layouts.dim(layouts.DEC_C, 0.6)))

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

    def test_the_widest_focus_numbers_fit_beside_each_other(self):
        nodes = [self.node(layouts.LIVE, dec=9999.0, pre=9999.0)] + [layouts.Node(f"studio{i}") for i in (2, 3, 4)]
        img = layouts.render("focus", nodes)
        dec = self.box(img, layouts.DEC_C, 0, 64, 8, 29)
        pre = self.box(img, layouts.PRE_C, 64, 128, 8, 29)
        self.assertLess(dec[1], 64)
        self.assertLess(pre[1], 128)


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
