#!/usr/bin/env python3
"""GPU temperature: python3 board/test_temp.py (or `python3 -m unittest board/test_temp.py`)

The agent reads it from `latch sensors`, the board carries it per node, and the layouts that have room draw it.
"""
import os
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agent  # noqa: E402
import board as boardmod  # noqa: E402
import layouts  # noqa: E402

SENSORS = "CPU 47.3°C · GPU 45.9°C\nActivity CPU 0.4% · GPU 0.0%\nMemory 153.3 GiB available · pressure normal\nThermal nominal\n"


class ParseAndRead(unittest.TestCase):
    def test_the_gpu_figure_is_read_from_latch_sensors_output(self):
        self.assertEqual(agent.parse_gpu_temp(SENSORS), 45.9)
        self.assertEqual(agent.parse_gpu_temp("CPU 50°C · GPU 71°C"), 71.0)

    def test_a_missing_or_nonsense_figure_is_none(self):
        for text in ("", "Thermal nominal", "CPU 47.3°C", "GPU --°C", "GPU 0.0°C", "GPU 200°C", "GPU 5.0°C"):
            self.assertIsNone(agent.parse_gpu_temp(text), text)

    def fake_latch(self, body):
        d = tempfile.mkdtemp(prefix="pxt-latch-")
        path = os.path.join(d, "latch")
        with open(path, "w") as f:
            f.write(f"#!/bin/sh\n{body}\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def test_it_runs_latch_sensors_and_parses_what_it_prints(self):
        path = self.fake_latch(f'[ "$1" = sensors ] && printf %s "{SENSORS}"')
        self.assertEqual(agent.read_gpu_temp(path), 45.9)

    def test_a_missing_or_failing_latch_gives_none(self):
        self.assertIsNone(agent.read_gpu_temp("/nonexistent/latch"))
        self.assertIsNone(agent.read_gpu_temp(self.fake_latch("exit 3")))
        printed_then_failed = self.fake_latch(f'printf %s "{SENSORS}"; exit 1')
        self.assertIsNone(agent.read_gpu_temp(printed_then_failed))

    def test_a_slow_latch_is_given_up_on_at_the_timeout(self):
        slow = self.fake_latch(f'sleep 2; printf %s "{SENSORS}"')
        old, agent.TEMP_TIMEOUT_S = agent.TEMP_TIMEOUT_S, 0.3
        try:
            self.assertIsNone(agent.read_gpu_temp(slow))
        finally:
            agent.TEMP_TIMEOUT_S = old
        self.assertEqual(agent.read_gpu_temp(slow), 45.9)      # the same script, given time, answers


class TempWatchTests(unittest.TestCase):
    def reader(self, values):
        calls = []
        it = iter(values)

        def read():
            calls.append(1)
            return next(it)
        return read, calls

    def test_it_reads_at_most_once_per_poll_interval(self):
        read, calls = self.reader([50.0, 51.0, 52.0])
        w = agent.TempWatch(read)
        got = [w.check(t) for t in (0.0, 1.0, agent.TEMP_POLL_S - 0.1, agent.TEMP_POLL_S + 0.1)]
        self.assertEqual((got, len(calls)), ([50.0, 50.0, 50.0, 51.0], 2))

    def test_a_failed_read_keeps_the_last_figure_until_it_is_too_old(self):
        read, _ = self.reader([50.0, None, None])
        w = agent.TempWatch(read)
        self.assertEqual(w.check(0.0), 50.0)
        self.assertEqual(w.check(agent.TEMP_POLL_S), 50.0)
        self.assertIsNone(w.check(agent.TEMP_STALE_S + agent.TEMP_POLL_S + 1))

    def test_nothing_is_reported_before_the_first_good_read(self):
        read, _ = self.reader([None])
        self.assertIsNone(agent.TempWatch(read).check(0.0))


class AgentLoop(unittest.TestCase):
    def test_the_agent_posts_the_temperature_it_reads(self):
        import http.server
        import json
        import threading
        import time
        posts = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        keyfile = os.path.join(tempfile.mkdtemp(prefix="pxt-key-"), "key")
        with open(keyfile, "w") as f:
            f.write("k" * 20)
        real_read, real_argv = agent.read_gpu_temp, sys.argv
        agent.read_gpu_temp = lambda: 51.5
        sys.argv = ["agent.py", "--name", "studio9", "--board", f"http://127.0.0.1:{srv.server_port}",
                    "--key-file", keyfile, "--metrics", "http://127.0.0.1:9/x"]
        try:
            threading.Thread(target=agent.main, daemon=True).start()
            deadline = time.time() + 5
            while not posts and time.time() < deadline:
                time.sleep(0.05)
        finally:
            agent.read_gpu_temp, sys.argv = real_read, real_argv
            srv.shutdown()
        self.assertTrue(posts)
        self.assertEqual(posts[0]["temp"], 51.5)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def report(**kw):
    body = {"node": "studio1", "gpu": 80, "mem_gb": 128, "mem_total_gb": 256, "dec": 50.0, "pre": 0.0}
    body.update(kw)
    return body


class BoardCarriesIt(unittest.TestCase):
    def view(self, **kw):
        b = boardmod.Board(["studio1"], clock=Clock())
        b.ingest(report(**kw))
        return b.nodes()[0]

    def test_a_reported_temperature_reaches_the_node(self):
        self.assertEqual(self.view(temp=61.5).temp, 61.5)

    def test_missing_or_junk_is_none(self):
        for junk in (None, "hot", True, float("nan"), float("inf"), -3, 0, 500):
            self.assertIsNone(self.view(temp=junk).temp, junk)
        self.assertIsNone(self.view().temp)

    def test_a_node_that_has_gone_stale_shows_no_temperature(self):
        clock = Clock()
        b = boardmod.Board(["studio1"], clock=clock)
        b.ingest(report(temp=61.5))
        clock.t += 60
        self.assertIsNone(b.nodes()[0].temp)


class TempText(unittest.TestCase):
    def test_the_figure_is_a_rounded_number_and_a_c(self):
        self.assertEqual(layouts.temp_text(45.9)[0], "46C")
        self.assertEqual(layouts.temp_text(100.0)[0], "100C")

    def test_it_warms_up_with_the_temperature(self):
        self.assertEqual(layouts.temp_text(69.9)[1], layouts.FG)
        self.assertEqual(layouts.temp_text(70.0)[1], layouts.TEMP_WARM)
        self.assertEqual(layouts.temp_text(84.9)[1], layouts.TEMP_WARM)
        self.assertEqual(layouts.temp_text(85.0)[1], layouts.MEM_HOT)

    def test_no_temperature_is_no_text(self):
        self.assertIsNone(layouts.temp_text(None))


class Drawn(unittest.TestCase):
    def nodes(self, temp, state=layouts.LIVE, dec=118.4):
        first = layouts.Node("studio1", state, gpu=88.0, mem=71.0, dec=dec, pre=2400.0, temp=temp)
        return [first] + [layouts.Node(f"studio{i}", state, gpu=60.0, mem=40.0, dec=90.0, pre=0.0, temp=temp)
                          for i in (2, 3, 4)]

    def changed(self, name, temp, **kw):
        """Every pixel that differs between a render with the temperature and one without."""
        a = layouts.render(name, self.nodes(temp, **kw)).load()
        b = layouts.render(name, self.nodes(None, **kw)).load()
        return [(x, y) for x in range(layouts.W) for y in range(layouts.H) if a[x, y] != b[x, y]]

    def test_rows_draw_it_in_the_gap_between_memory_and_decode(self):
        diff = self.changed("rows", 100.0)
        self.assertTrue(diff)
        self.assertTrue(all(52 <= x <= 73 and y % 16 in range(1, 7) for x, y in diff), diff[:6])

    def test_focus_draws_it_left_of_the_gpu_and_memory_figures(self):
        diff = self.changed("focus", 100.0)
        self.assertTrue(diff)
        self.assertTrue(all(70 <= x <= 95 and 1 <= y <= 6 for x, y in diff), diff[:6])

    def test_quad_draws_it_above_the_prefill_label_in_each_cell(self):
        diff = self.changed("quad", 100.0)
        self.assertTrue(diff)
        self.assertTrue(all((x % 64) >= 44 and (y % 32) in range(9, 15) for x, y in diff), diff[:6])
        self.assertEqual({(x // 64, y // 32) for x, y in diff}, {(0, 0), (1, 0), (0, 1), (1, 1)})

    def test_the_other_layouts_are_unchanged(self):
        for name in ("cols", "spark"):
            self.assertEqual(self.changed(name, 100.0), [], name)

    def test_it_only_adds_ink_to_empty_space_even_beside_the_widest_neighbours(self):
        for name in ("rows", "focus", "quad"):
            for dec in (118.4, 9999.0):
                a = layouts.render(name, self.nodes(100.0, dec=dec)).load()
                b = layouts.render(name, self.nodes(None, dec=dec)).load()
                overwritten = [(x, y) for x in range(layouts.W) for y in range(layouts.H)
                               if a[x, y] != b[x, y] and b[x, y] != layouts.BG]
                self.assertEqual(overwritten, [], (name, dec))

    def test_a_node_that_is_not_reporting_shows_none(self):
        for name in ("rows", "focus", "quad"):
            self.assertEqual(self.changed(name, 70.0, state=layouts.STALE), [], name)


if __name__ == "__main__":
    unittest.main()
