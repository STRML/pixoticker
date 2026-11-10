#!/usr/bin/env python3
"""Machine stats (GPU temperature, system watts, CPU percent): python3 board/test_stats.py

The agent reads them (`latch sensors`, the SMC, the kernel's CPU counters), the board carries them per node, and
the layouts that have room draw them.
"""
import ctypes
import os
import stat
import struct
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
    def test_the_agent_posts_the_temperature_watts_and_cpu_it_reads(self):
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
        real_read, real_watts, real_ticks, real_argv = agent.read_gpu_temp, agent.read_watts, agent.cpu_ticks, sys.argv
        agent.read_gpu_temp = lambda: 51.5
        agent.read_watts = lambda: 182.5
        reads = iter([(0, 0, 100, 0), (30, 10, 160, 0)] + [(30, 10, 160, 0)] * 100)
        agent.cpu_ticks = lambda: next(reads)
        sys.argv = ["agent.py", "--name", "studio9", "--board", f"http://127.0.0.1:{srv.server_port}",
                    "--key-file", keyfile, "--metrics", "http://127.0.0.1:9/x"]
        try:
            threading.Thread(target=agent.main, daemon=True).start()
            deadline = time.time() + 6
            while len(posts) < 2 and time.time() < deadline:
                time.sleep(0.05)
        finally:
            agent.read_gpu_temp, agent.read_watts, agent.cpu_ticks, sys.argv = real_read, real_watts, real_ticks, real_argv
            srv.shutdown()
        self.assertGreaterEqual(len(posts), 2)
        self.assertEqual((posts[0]["temp"], posts[0]["watts"], posts[0]["cpu"]), (51.5, 182.5, None))
        self.assertEqual((posts[1]["temp"], posts[1]["watts"], posts[1]["cpu"]), (51.5, 182.5, 40.0))


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


class SmcAndCpu(unittest.TestCase):
    def test_the_smc_structures_have_the_layout_the_kernel_expects(self):
        self.assertEqual(ctypes.sizeof(agent.SmcKeyData), 80)
        offsets = {name: getattr(agent.SmcKeyData, name).offset for name, _ in agent.SmcKeyData._fields_}
        self.assertEqual((offsets["key"], offsets["vers"], offsets["plimit"], offsets["info"]), (0, 4, 12, 28))
        self.assertEqual((offsets["result"], offsets["data32"], offsets["bytes"]), (40, 44, 48))

    def test_a_float_key_decodes_little_endian_and_nothing_else_is_guessed(self):
        raw = struct.pack("<f", 182.5) + bytes(28)
        self.assertEqual(agent.smc_float("flt ", 4, raw), 182.5)
        self.assertIsNone(agent.smc_float("ui32", 4, raw))
        self.assertIsNone(agent.smc_float("flt ", 2, raw))
        self.assertIsNone(agent.smc_float("flt ", 4, struct.pack("<f", float("nan")) + bytes(28)))
        self.assertIsNone(agent.smc_float("flt ", 4, struct.pack("<f", float("inf")) + bytes(28)))

    def test_watts_are_the_whole_board_key_and_only_in_a_sane_range(self):
        asked = []
        self.assertEqual(agent.read_watts(lambda key: asked.append(key) or 182.5), 182.5)
        self.assertEqual(asked, ["PD0R"])
        for v in (0.0, -3.0, 1000.1, 5000.0, float("nan"), float("inf"), None):
            self.assertIsNone(agent.read_watts(lambda key, v=v: v), v)
        self.assertEqual(agent.read_watts(lambda key: 1000.0), 1000.0)

    def ticks(self, *seq):
        it = iter(seq)
        return lambda: next(it)

    def test_cpu_percent_is_busy_over_total_between_two_reads(self):
        c = agent.CpuLoad(self.ticks((100, 50, 800, 0), (120, 60, 870, 0)))
        self.assertIsNone(c.percent())
        self.assertEqual(c.percent(), 30.0)

    def test_nice_time_counts_as_busy(self):
        c = agent.CpuLoad(self.ticks((0, 0, 0, 0), (0, 0, 50, 50)))
        c.percent()
        self.assertEqual(c.percent(), 50.0)

    def test_a_counter_that_wrapped_is_still_read(self):
        top = 2 ** 32
        c = agent.CpuLoad(self.ticks((top - 10, top - 10, top - 10, 0), (10, 10, 20, 0)))
        c.percent()
        self.assertEqual(c.percent(), 57.1)     # deltas 20, 20, 30, 0: busy 40 of 70

    def test_no_time_passed_or_no_reading_is_none_and_the_next_read_starts_over(self):
        c = agent.CpuLoad(self.ticks((1, 1, 1, 0), (1, 1, 1, 0), None, (5, 5, 5, 0), (6, 6, 6, 0)))
        self.assertIsNone(c.percent())
        self.assertIsNone(c.percent())       # nothing elapsed
        self.assertIsNone(c.percent())       # the reader failed
        self.assertIsNone(c.percent())       # the first read after a failure has no baseline
        self.assertEqual(c.percent(), 66.7)

    @unittest.skipUnless(sys.platform == "darwin", "reads this Mac's own SMC and CPU counters")
    def test_the_real_readers_on_this_machine_give_a_figure_or_nothing_and_never_raise(self):
        watts = agent.read_watts()
        self.assertTrue(watts is None or 0 < watts <= 1000)
        c = agent.CpuLoad()
        self.assertIsNone(c.percent())
        cpu = c.percent()
        self.assertTrue(cpu is None or 0 <= cpu <= 100)


class BoardCarriesStats(unittest.TestCase):
    def view(self, **kw):
        b = boardmod.Board(["studio1"], clock=Clock())
        b.ingest(report(**kw))
        return b.nodes()[0]

    def test_watts_and_cpu_reach_the_node(self):
        n = self.view(watts=182.5, cpu=9.1)
        self.assertEqual((n.watts, n.cpu), (182.5, 9.1))

    def test_zero_cpu_is_a_figure_and_zero_watts_is_not(self):
        n = self.view(watts=0, cpu=0)
        self.assertEqual((n.watts, n.cpu), (None, 0.0))

    def test_junk_is_none(self):
        for junk in (None, "hot", True, float("nan"), float("inf"), -3):
            n = self.view(watts=junk, cpu=junk)
            self.assertEqual((n.watts, n.cpu), (None, None), junk)
        self.assertIsNone(self.view(watts=1000.5).watts)
        self.assertIsNone(self.view(cpu=100.5).cpu)
        self.assertEqual(self.view(watts=1000.0, cpu=100.0).cpu, 100.0)

    def test_stats_are_a_nodes_own_even_when_it_mirrors_a_leader(self):
        b = boardmod.Board(["studio1", "studio2"], clock=Clock())
        b.ingest(report(running=1, watts=180.0, cpu=10.0, temp=60.0))
        b.ingest({"node": "studio2", "gpu": 90, "worker": True, "follows": "studio1", "watts": 175.0, "cpu": 4.0, "temp": 58.0})
        views = {n.name: n for n in b.nodes()}
        self.assertEqual((views["studio2"].watts, views["studio2"].cpu, views["studio2"].temp), (175.0, 4.0, 58.0))


class TextAndGeometry(unittest.TestCase):
    def test_watts_text_is_whole_watts_and_never_wider_than_four_characters(self):
        self.assertEqual(layouts.watts_text(182.4)[0], "182W")
        self.assertEqual(layouts.watts_text(7.2)[0], "7W")
        self.assertEqual(layouts.watts_text(999.9)[0], "999W")
        self.assertEqual(layouts.watts_text(1000.0)[0], "999W")
        self.assertIsNone(layouts.watts_text(None))

    def test_cpu_text_is_whole_percent(self):
        self.assertEqual(layouts.cpu_text(9.4)[0], "9%")
        self.assertEqual(layouts.cpu_text(100.0)[0], "100%")
        self.assertIsNone(layouts.cpu_text(None))

    def lit_columns(self, canvas, colour):
        px = canvas.img.load()
        return sorted({x for x in range(layouts.W) for y in range(layouts.H) if px[x, y] == colour})

    def test_stats_are_drawn_a_space_apart_and_the_width_helper_agrees(self):
        parts = [layouts.watts_text(182.0), layouts.cpu_text(9.0)]
        self.assertEqual(layouts.stats_width(parts), 4 * 4 + 4 + 2 * 4)
        alone = layouts.Canvas()
        layouts.draw_stats(alone, 0, 0, [parts[1]])
        offset = self.lit_columns(alone, layouts.CPU_C)[0]
        c = layouts.Canvas()
        end = layouts.draw_stats(c, 10, 0, parts)
        self.assertEqual(self.lit_columns(c, layouts.CPU_C)[0], 10 + 4 * 4 + 4 + offset)
        self.assertEqual(end, 10 + layouts.stats_width(parts) + 4)

    def test_absent_stats_are_skipped_without_a_gap(self):
        c = layouts.Canvas()
        layouts.draw_stats(c, 10, 0, [None, layouts.cpu_text(9.0), None])
        self.assertEqual(layouts.stats_width([None, layouts.cpu_text(9.0), None]), 2 * 4)
        self.assertEqual(self.lit_columns(c, layouts.CPU_C)[0] - 10, self.lit_columns_offset())

    def lit_columns_offset(self):
        c = layouts.Canvas()
        layouts.draw_stats(c, 0, 0, [layouts.cpu_text(9.0)])
        return self.lit_columns(c, layouts.CPU_C)[0]

    def test_the_stat_colours_are_distinct_from_each_other_and_from_the_metric_colours(self):
        colours = {layouts.WATT_C, layouts.CPU_C, layouts.GPU_C, layouts.MEM_C, layouts.DEC_C, layouts.PRE_C,
                   layouts.TEMP_WARM}
        self.assertEqual(len(colours), 7)


class Drawn(unittest.TestCase):
    WIDEST = dict(temp=100.0, watts=9999.0, cpu=100.0)

    def nodes(self, state=layouts.LIVE, dec=118.4, **stats):
        first = layouts.Node("studio1", state, gpu=88.0, mem=71.0, dec=dec, pre=2400.0, **stats)
        return [first] + [layouts.Node(f"studio{i}", state, gpu=60.0, mem=40.0, dec=90.0, pre=0.0, **stats)
                          for i in (2, 3, 4)]

    def changed(self, name, state=layouts.LIVE, dec=118.4, **stats):
        """Every pixel that differs between a render with the stats and one without."""
        a = layouts.render(name, self.nodes(state, dec, **stats)).load()
        b = layouts.render(name, self.nodes(state, dec)).load()
        return [(x, y) for x in range(layouts.W) for y in range(layouts.H) if a[x, y] != b[x, y]]

    def test_rows_draw_temperature_in_the_gap_between_memory_and_decode(self):
        diff = self.changed("rows", temp=100.0)
        self.assertTrue(diff)
        self.assertTrue(all(52 <= x <= 73 and y % 16 in range(1, 7) for x, y in diff), diff[:6])

    def test_rows_draw_watts_and_cpu_right_aligned_beside_the_bars(self):
        diff = self.changed("rows", watts=9999.0, cpu=100.0)
        self.assertTrue(diff)
        self.assertTrue(all(x >= 90 and y % 16 in range(9, 15) for x, y in diff), diff[:6])

    def test_rows_bars_stop_short_of_the_stats(self):
        img = layouts.render("rows", self.nodes()).load()
        bars = [(x, y) for x in range(layouts.W) for y in range(layouts.H)
                if y % 16 >= 8 and img[x, y] in (layouts.GPU_C, layouts.MEM_C)]
        self.assertTrue(bars)
        self.assertLess(max(x for x, _ in bars), 90)

    def test_spark_draws_watts_over_cpu_in_the_column_after_a_shorter_history(self):
        self.assertEqual(layouts.HIST, 36)
        diff = self.changed("spark", watts=9999.0, cpu=100.0)
        self.assertTrue(diff)
        self.assertTrue(all(80 <= x <= 99 and (y - 8) % 14 in range(1, 13) for x, y in diff), diff[:6])
        ys = {(y - 8) % 14 for _, y in diff}
        self.assertTrue(any(v <= 6 for v in ys) and any(v >= 7 for v in ys))

    def test_quad_draws_watts_and_cpu_where_the_unit_label_was(self):
        diff = self.changed("quad", watts=9999.0, cpu=100.0)
        self.assertTrue(diff)
        self.assertTrue(all(2 <= x % 64 <= 39 and y % 32 in range(22, 28) for x, y in diff), diff[:6])
        self.assertEqual({(x // 64, y // 32) for x, y in diff}, {(0, 0), (1, 0), (0, 1), (1, 1)})
        base = layouts.render("quad", self.nodes()).load()
        self.assertTrue(all(base[x, y] == layouts.BG for x in range(2, 40) for y in range(22, 28)), "the TOK/S label is gone")

    def test_quad_draws_temperature_above_the_prefill_label_in_each_cell(self):
        diff = self.changed("quad", temp=100.0)
        self.assertTrue(diff)
        self.assertTrue(all((x % 64) >= 44 and (y % 32) in range(9, 15) for x, y in diff), diff[:6])

    def test_focus_draws_all_three_right_of_shorter_gpu_and_memory_bars_not_in_the_header(self):
        diff = self.changed("focus", **self.WIDEST)
        self.assertTrue(diff)
        self.assertTrue(all(x >= 68 and 39 <= y <= 44 for x, y in diff), diff[:6])
        img = layouts.render("focus", self.nodes()).load()
        bars = [(x, y) for x in range(layouts.W) for y in (40, 41, 43, 44) if img[x, y] in (layouts.GPU_C, layouts.MEM_C)]
        self.assertLess(max(x for x, _ in bars), 68)

    def test_each_stat_alone_is_drawn_where_it_has_room(self):
        for name in ("rows", "focus", "quad", "spark"):
            for stat in (dict(temp=70.0), dict(watts=180.0), dict(cpu=9.0)):
                drawn = bool(self.changed(name, **stat))
                expected = not (name == "spark" and "temp" in stat)
                self.assertEqual(drawn, expected, (name, stat))

    def test_cols_has_no_room_and_is_unchanged(self):
        self.assertEqual(self.changed("cols", **self.WIDEST), [])

    def test_the_stats_only_add_ink_to_empty_space_even_beside_the_widest_neighbours(self):
        for name in ("rows", "spark", "focus", "quad"):
            for dec in (118.4, 9999.0):
                a = layouts.render(name, self.nodes(dec=dec, **self.WIDEST)).load()
                b = layouts.render(name, self.nodes(dec=dec)).load()
                overwritten = [(x, y) for x in range(layouts.W) for y in range(layouts.H)
                               if a[x, y] != b[x, y] and b[x, y] != layouts.BG]
                self.assertEqual(overwritten, [], (name, dec))

    def test_a_node_that_is_not_reporting_shows_none(self):
        for name in ("rows", "spark", "focus", "quad"):
            self.assertEqual(self.changed(name, state=layouts.STALE, **self.WIDEST), [], name)


if __name__ == "__main__":
    unittest.main()
