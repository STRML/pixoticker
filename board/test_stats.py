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


class FakeIokit:
    """Records every handle the SMC code takes and gives back, so a leak shows as a handle never released."""

    def __init__(self, no_device=False, open_fails=False, calls_fail=False):
        self.no_device, self.open_fails, self.calls_fail = no_device, open_fails, calls_fail
        self.next, self.taken, self.released, self.closed, self.opened = 100, set(), set(), set(), []

    def handle(self):
        self.next += 1
        self.taken.add(self.next)
        return self.next

    def IOServiceMatching(self, name):
        return 1

    def IOServiceGetMatchingServices(self, master, matching, ref):
        ref._obj.value = self.handle()
        return 0

    def IOIteratorNext(self, it):
        return 0 if self.no_device else self.handle()

    def IOObjectRelease(self, handle):
        self.released.add(handle)

    def IOServiceOpen(self, device, task, kind, ref):
        if self.open_fails:
            return 1
        ref._obj.value = self.handle()
        self.opened.append(ref._obj.value)
        return 0

    def IOServiceClose(self, conn):
        self.closed.add(conn)

    def IOConnectCallStructMethod(self, conn, index, request, size, out, out_size):
        return 1 if self.calls_fail else 0

    def leaked(self):
        return (self.taken - self.released) - set(self.opened) | (set(self.opened) - self.closed)


class SmcHandles(unittest.TestCase):
    def smc(self, **kw):
        fake = FakeIokit(**kw)
        return fake, agent.Smc(load=lambda: fake)

    def test_a_good_open_releases_the_iterator_and_the_service_and_keeps_only_the_connection(self):
        fake, smc = self.smc()
        smc._open()
        self.assertEqual(smc.conn, fake.opened[0])
        self.assertEqual(fake.taken - fake.released, {fake.opened[0]})
        smc.close()
        self.assertEqual(fake.leaked(), set())

    def test_no_device_releases_the_iterator(self):
        fake, smc = self.smc(no_device=True)
        self.assertIsNone(smc.read("PD0R"))
        self.assertEqual(fake.leaked(), set())

    def test_a_refused_open_releases_the_iterator_and_the_service(self):
        fake, smc = self.smc(open_fails=True)
        self.assertIsNone(smc.read("PD0R"))
        self.assertIsNone(smc.conn)
        self.assertEqual(fake.leaked(), set())

    def test_a_key_that_always_fails_closes_every_connection_it_opens_once_a_second_for_a_long_time(self):
        fake, smc = self.smc(calls_fail=True)
        for _ in range(500):
            self.assertIsNone(smc.read("PD0R"))
        self.assertEqual(len(fake.opened), 500)
        self.assertEqual(fake.leaked(), set())

    def test_a_key_that_works_keeps_one_connection_open_across_reads(self):
        fake, smc = self.smc()
        for _ in range(5):
            smc.read("PD0R")
        self.assertEqual(len(fake.opened), 1)
        self.assertEqual(fake.closed, set())


class HostPort(unittest.TestCase):
    class Libc:
        def __init__(self):
            self.host_calls, self.taken = [], 0

            def take():
                self.taken += 1
                return 7

            self.mach_host_self = take

        def host_statistics(self, host, flavor, buf, count):
            self.host_calls.append(host)
            return 0

    def test_the_host_port_is_taken_once_however_often_the_cpu_is_read(self):
        libc, saved = self.Libc(), list(agent._HOST)
        agent._HOST[:] = []
        real = ctypes.CDLL
        ctypes.CDLL = lambda name, *a, **k: libc
        try:
            for _ in range(50):
                self.assertIsNotNone(agent.cpu_ticks())
        finally:
            ctypes.CDLL = real
            agent._HOST[:] = saved
        self.assertEqual(libc.taken, 1)
        self.assertEqual(libc.host_calls, [7] * 50)


class BoardRanges(unittest.TestCase):
    def view(self, **kw):
        b = boardmod.Board(["studio1"], clock=Clock())
        b.ingest(report(**kw))
        return b.nodes()[0]

    def test_the_documented_ranges_are_the_accepted_ones(self):
        self.assertEqual(self.view(temp=10).temp, 10.0)
        self.assertEqual(self.view(temp=130).temp, 130.0)
        for outside in (9.9, 5, 0, -1, 130.1):
            self.assertIsNone(self.view(temp=outside).temp, outside)
        self.assertEqual(self.view(cpu=0).cpu, 0.0)
        for outside in (-0.5, -1, 100.5):
            self.assertIsNone(self.view(cpu=outside).cpu, outside)
        for outside in (0, -1, 1000.5):
            self.assertIsNone(self.view(watts=outside).watts, outside)


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

    def test_cpu_text_is_a_c_the_whole_percent_and_its_sign(self):
        self.assertEqual(layouts.cpu_text(9.4)[0], "C09%")
        self.assertEqual(layouts.cpu_text(100.0)[0], "C99%")
        self.assertIsNone(layouts.cpu_text(None))

    def test_percent_figures_carry_a_sign_pad_to_two_digits_and_stop_at_99(self):
        self.assertEqual(layouts.pct(93.4), "93%")
        self.assertEqual(layouts.pct(8.0), "08%")
        self.assertEqual(layouts.pct(0.0), "00%")
        self.assertEqual(layouts.pct(99.6), "99%")
        self.assertEqual(layouts.pct(100.0), "99%")
        self.assertEqual(layouts.pct(None), "--")

    def lit_columns(self, canvas, colour):
        px = canvas.img.load()
        return sorted({x for x in range(layouts.W) for y in range(layouts.H) if px[x, y] == colour})

    def test_stats_sit_in_fixed_five_character_slots_and_the_width_helper_agrees(self):
        parts = [layouts.watts_text(182.0), layouts.cpu_text(9.0)]
        self.assertEqual(layouts.stats_width(parts), 2 * 5 * 4)
        c = layouts.Canvas()
        end = layouts.draw_stats(c, 10, 0, parts)
        self.assertEqual(end, 10 + layouts.stats_width(parts))
        self.assertEqual(max(self.lit_columns(c, layouts.CPU_C)), end - 1 - 1)

    def test_a_five_character_figure_fills_its_slot_and_the_figure_before_it_stays_clear(self):
        kilowatts = layouts.watts_total([layouts.Node("studio1", layouts.LIVE, watts=1200.0)])
        self.assertEqual(kilowatts[0], "1.2KW")
        c = layouts.Canvas()
        layouts.draw_stats(c, 10, 0, [layouts.cpu_text(99.0), kilowatts])
        cpu, watts = self.lit_columns(c, layouts.CPU_C), self.lit_columns(c, layouts.WATT_C)
        self.assertEqual(min(watts), 10 + 20)
        self.assertLess(max(cpu), min(watts) - 1)

    def test_a_figure_gaining_a_digit_does_not_move_its_neighbours(self):
        def drawn(watts, cpu):
            c = layouts.Canvas()
            layouts.draw_stats(c, 10, 0, [layouts.watts_text(watts), layouts.cpu_text(cpu)])
            return self.lit_columns(c, layouts.WATT_C), self.lit_columns(c, layouts.CPU_C)
        self.assertEqual(max(drawn(7, 9)[0]), max(drawn(182, 100)[0]))
        self.assertEqual(max(drawn(7, 9)[1]), max(drawn(182, 100)[1]))
        self.assertEqual(drawn(7, 9)[1], drawn(182, 9)[1])
        self.assertEqual(drawn(7, 9)[0], drawn(7, 100)[0])

    def test_an_absent_stat_leaves_its_slot_empty_so_the_others_stay_put(self):
        both = layouts.Canvas()
        layouts.draw_stats(both, 10, 0, [layouts.watts_text(182.0), layouts.cpu_text(9.0)])
        alone = layouts.Canvas()
        layouts.draw_stats(alone, 10, 0, [None, layouts.cpu_text(9.0)])
        self.assertEqual(self.lit_columns(alone, layouts.CPU_C), self.lit_columns(both, layouts.CPU_C))
        self.assertEqual(self.lit_columns(alone, layouts.WATT_C), [])

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

    def below_the_bar(self, name, **stats):
        """The same, without the cluster bar's rows (the first eight)."""
        return [(x, y) for x, y in self.changed(name, **stats) if y >= 8]

    def test_rows_draw_temperature_and_watts_right_aligned_beside_the_bars(self):
        diff = self.changed("rows", temp=100.0, watts=9999.0)
        self.assertTrue(diff)
        self.assertTrue(all(x >= 90 and y % 16 in range(9, 15) for x, y in diff), diff[:6])

    def test_rows_draw_cpu_between_gpu_and_memory_in_the_number_row(self):
        diff = self.changed("rows", cpu=100.0)
        self.assertTrue(diff)
        self.assertTrue(all(32 <= x <= 52 and y % 16 in range(1, 7) for x, y in diff), diff[:6])

    def test_rows_gpu_label_clears_a_three_character_tag_even_at_100_percent(self):
        nodes = [layouts.Node("studio10", layouts.LIVE, gpu=100.0, mem=50.0, dec=10.0, pre=0.0)]
        img = layouts.render("rows", nodes)
        tag = [x for x in range(layouts.W) for y in range(1, 7) if img.getpixel((x, y)) == layouts.FG]
        gpu = [x for x in range(layouts.W) for y in range(1, 7) if img.getpixel((x, y)) == layouts.GPU_C]
        self.assertLess(max(tag), min(gpu))

    def test_rows_stat_columns_hold_still_whatever_the_digit_counts(self):
        def right_edges(temp, watts, cpu):
            img = layouts.render("rows", self.nodes(temp=temp, watts=watts, cpu=cpu))
            edge = lambda colour, rows: max(x for x in range(layouts.W) for y in rows if img.getpixel((x, y)) == colour)
            return (edge(layouts.MEM_HOT, range(9, 15)), edge(layouts.WATT_C, range(9, 15)), edge(layouts.CPU_C, range(1, 7)))
        base = right_edges(90.0, 182.0, 100.0)
        for temp, watts, cpu in ((85.0, 7.0, 9.0), (99.0, 99.0, 9.0), (100.0, 7.0, 42.0), (88.0, 999.0, 100.0)):
            self.assertEqual(right_edges(temp, watts, cpu), base, (temp, watts, cpu))

    def test_rows_bars_stop_short_of_the_stats(self):
        img = layouts.render("rows", self.nodes()).load()
        bars = [(x, y) for x in range(layouts.W) for y in range(layouts.H)
                if y % 16 >= 8 and img[x, y] in (layouts.GPU_C, layouts.MEM_C)]
        self.assertTrue(bars)
        self.assertLess(max(x for x, _ in bars), 90)

    def test_spark_draws_watts_over_cpu_in_the_column_after_a_shorter_history(self):
        self.assertEqual(layouts.HIST, 36)
        diff = self.below_the_bar("spark", watts=9999.0, cpu=100.0)
        self.assertTrue(diff)
        self.assertTrue(all(82 <= x <= 100 and (y - 8) % 14 in range(1, 13) for x, y in diff), diff[:6])
        ys = {(y - 8) % 14 for _, y in diff}
        self.assertTrue(any(v <= 6 for v in ys) and any(v >= 7 for v in ys))

    def test_quad_draws_temperature_and_watts_beside_the_tag_and_cpu_in_the_row_above_the_bars(self):
        quadrants = {(0, 0), (1, 0), (0, 1), (1, 1)}
        hardware = self.below_the_bar("quad", temp=100.0, watts=9999.0)
        self.assertTrue(hardware)
        self.assertTrue(all(13 <= x % 64 < 53 and (y - 8) % 28 in range(1, 7) for x, y in hardware), hardware[:6])
        self.assertEqual({(x // 64, (y - 8) // 28) for x, y in hardware}, quadrants)
        cpu = self.below_the_bar("quad", cpu=100.0)
        self.assertTrue(all(23 <= x % 64 <= 41 and (y - 8) % 28 in range(21, 27) for x, y in cpu), cpu[:6])
        self.assertEqual({(x // 64, (y - 8) // 28) for x, y in cpu}, quadrants)

    def test_gpu_and_cpu_percentages_sit_side_by_side_with_memory_after_them(self):
        bands = {"rows": (1, 7, 0, 128), "quad": (29, 35, 2, 62), "focus": (39, 45, 68, 128)}
        for name, (y0, y1, x0, x1) in bands.items():
            img = layouts.render(name, self.nodes(cpu=12.0))
            ink = lambda colour: [x for x in range(x0, x1) for y in range(y0, y1) if img.getpixel((x, y)) == colour]
            gpu, cpu, mem = ink(layouts.GPU_C), ink(layouts.CPU_C), ink(layouts.MEM_C)
            self.assertTrue(gpu and cpu and mem, name)
            self.assertLess(max(gpu), min(cpu), name)
            self.assertLess(max(cpu), min(mem), name)
            self.assertLessEqual(min(cpu) - max(gpu), 6, name)

    def test_gpu_and_memory_labels_are_drawn_whole_at_every_width_in_every_layout(self):
        """The label's own pixels, and nothing else, fill its box: a bar, a rate or a neighbour reaching into it fails."""
        places = {"rows": (("r", 32, 1), ("r", 72, 1)), "cols": (("l", 3, 37), ("l", 3, 44)),
                  "quad": (("r", 23, 29), ("r", 63, 29)), "spark": (("l", 14, 9), ("l", 14, 15)),
                  "focus": (("r", 86, 39), ("r", 126, 39))}
        for gpu, mem, text in ((88.0, 71.0, ("G88%", "M71%")), (5.0, 5.0, ("G05%", "M05%")), (100.0, 100.0, ("G99%", "M99%"))):
            nodes = [layouts.Node("studio1", layouts.LIVE, gpu=gpu, mem=mem, dec=118.4, pre=2400.0, **self.WIDEST)]
            nodes += [layouts.Node(f"studio{i}", layouts.LIVE, gpu=gpu, mem=mem, dec=90.0, pre=0.0, **self.WIDEST) for i in (2, 3, 4)]
            gc, mc = layouts.bar_colors(nodes[0])
            for name, spots in places.items():
                img = layouts.render(name, nodes)
                for (edge, x, y), label, colour in zip(spots, text, (gc, mc)):
                    left = x if edge == "l" else x - layouts.font.width(label)
                    alone = layouts.Canvas()
                    alone.text(left, y, label, colour)
                    box = [(px, py) for px in range(left, left + layouts.font.width(label) - 1) for py in range(y, y + 6)]
                    self.assertEqual([img.getpixel(p) for p in box], [alone.img.getpixel(p) for p in box], (name, label))

    def test_quad_decode_mark_and_prefill_never_touch_even_at_the_widest_rates(self):
        for dec in (118.4, 9949.0, 9999.0):
            nodes = [layouts.Node("studio1", layouts.LIVE, gpu=50.0, mem=50.0, dec=dec, pre=9999.0)]
            img = layouts.render("quad", nodes).load()
            decode = {x for x in range(64) for y in range(16, 28) if img[x, y] in (layouts.DEC_C, layouts.dim(layouts.DEC_C, 0.6))}
            prefill = {x for x in range(64) for y in range(16, 22) if img[x, y] == layouts.PRE_C}
            self.assertLess(max(decode), min(prefill), dec)

    def test_quad_labels_prefill_beside_the_decode_figure_and_marks_decode_with_a_d(self):
        img = layouts.render("quad", self.nodes(dec=118.4)).load()
        pre = [x for x in range(64) for y in range(16, 22) if img[x, y] == layouts.PRE_C]
        self.assertTrue(pre and min(pre) >= 42 and max(pre) == 60, (min(pre), max(pre)))
        d = [x for x in range(64) for y in range(16, 22) if img[x, y] == layouts.dim(layouts.DEC_C, 0.6)]
        self.assertTrue(d and min(d) >= 3 + 3 * 8 + 2 and max(d) < 42, d)

    def test_focus_leads_its_decode_label_with_the_tag_and_draws_no_hardware_of_its_own(self):
        img = layouts.render("focus", self.nodes()).load()
        tag = [x for x in range(layouts.W) for y in range(30, 36) if img[x, y] == layouts.FG]
        self.assertTrue(tag and max(tag) < 13, tag)
        self.assertEqual(self.changed("focus", temp=100.0), [])

    def test_focus_draws_gpu_cpu_and_memory_right_of_the_shorter_bars_and_the_cluster_bar_above(self):
        diff = self.below_the_bar("focus", cpu=100.0)
        self.assertTrue(diff)
        self.assertTrue(all(x >= 68 and 39 <= y <= 44 for x, y in diff), diff[:6])
        img = layouts.render("focus", self.nodes()).load()
        track_end = next(x for x in range(1, layouts.W) if img[x, 41] == layouts.BG)
        self.assertEqual(track_end - 1, layouts.FOCUS_BAR_W)
        self.assertLess(track_end, 66)

    def test_each_stat_alone_is_drawn_where_it_has_room(self):
        for name in ("rows", "focus", "quad", "spark", "cols"):
            for stat in (dict(temp=70.0), dict(watts=180.0), dict(cpu=9.0)):
                drawn = bool(self.changed(name, **stat))
                expected = not ("temp" in stat and name in ("spark", "focus", "cols"))
                self.assertEqual(drawn, expected, (name, stat))

    def test_cols_draws_the_cluster_bar_and_no_hardware_of_its_own(self):
        diff = self.changed("cols", **self.WIDEST)
        self.assertTrue(diff)
        self.assertTrue(all(y < 7 for _, y in diff), diff[:6])

    def test_the_stats_only_add_ink_to_empty_space_even_beside_the_widest_neighbours(self):
        for name in ("rows", "spark", "focus", "quad", "cols"):
            for dec in (118.4, 9999.0):
                a = layouts.render(name, self.nodes(dec=dec, **self.WIDEST)).load()
                b = layouts.render(name, self.nodes(dec=dec)).load()
                overwritten = [(x, y) for x in range(layouts.W) for y in range(layouts.H)
                               if a[x, y] != b[x, y] and b[x, y] != layouts.BG]
                self.assertEqual(overwritten, [], (name, dec))

    def test_a_node_that_is_not_reporting_shows_none(self):
        for name in ("rows", "spark", "focus", "quad", "cols"):
            self.assertEqual(self.changed(name, state=layouts.STALE, **self.WIDEST), [], name)


class MemoryTurnsRedAbove90(unittest.TestCase):
    """Memory is drawn red only when the figure on the panel is above 90%, on every layout and in the cluster bar."""

    def frame(self, name, mem):
        nodes = [layouts.Node(f"studio{i}", layouts.LIVE, gpu=50.0, mem=mem, dec=100.0, pre=1000.0) for i in range(1, 5)]
        img = layouts.render(name, nodes)
        return {colour for _, colour in img.getcolors(layouts.W * layouts.H)}

    def test_every_layout_keeps_memory_violet_up_to_90_and_red_from_91(self):
        for name in layouts.LAYOUTS:
            for mem in (86.0, 90.0, 90.4):
                self.assertNotIn(layouts.MEM_HOT, self.frame(name, mem), f"{name} at {mem}%")
                self.assertIn(layouts.MEM_C, self.frame(name, mem), f"{name} at {mem}%")
            for mem in (90.6, 91.0, 99.0):
                self.assertIn(layouts.MEM_HOT, self.frame(name, mem), f"{name} at {mem}%")

    def test_a_memory_total_near_zero_reads_as_full_and_every_layout_still_draws(self):
        b = boardmod.Board(["studio1"], clock=Clock())
        b.ingest(report(mem_gb=1, mem_total_gb=1e-320))
        node = b.nodes()[0]
        self.assertEqual(node.mem, 100.0)
        for name in layouts.LAYOUTS:
            self.assertIn(layouts.MEM_HOT, {c for _, c in layouts.render(name, [node]).getcolors(layouts.W * layouts.H)}, name)

    def test_the_cluster_bar_follows_the_same_rule_on_the_average(self):
        def colour(mems):
            nodes = [layouts.Node(f"studio{i + 1}", layouts.LIVE, mem=m) for i, m in enumerate(mems)]
            return layouts.summary_parts(nodes)[2][1]

        self.assertEqual(colour([90.0, 90.0, 90.0, 90.0]), layouts.MEM_C)
        self.assertEqual(colour([88.0, 92.0, 90.0, 90.0]), layouts.MEM_C)
        self.assertEqual(colour([91.0, 91.0, 91.0, 91.0]), layouts.MEM_HOT)


if __name__ == "__main__":
    unittest.main()
