#!/usr/bin/env python3
"""Night dimming that keeps content visible: python3 board/test_dim.py

Scaling every channel by 0.25 takes a grey label from 95 to 24, which the LEDs barely show. The dimmer scales
by the brightest channel and gives content a floor.
"""
import os
import sys
import unittest

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server"))

import board as boardmod  # noqa: E402
import layouts  # noqa: E402
import preview  # noqa: E402

NY = "America/New_York"


def one(colour):
    img = Image.new("RGB", (1, 1))
    img.putpixel((0, 0), colour)
    return img


def dimmed(colour, factor=0.25, **kw):
    return boardmod.dim_image(one(colour), factor, **kw).getpixel((0, 0))


class Floor(unittest.TestCase):
    def test_a_grey_label_keeps_a_visible_level(self):
        self.assertEqual(dimmed(layouts.DIM), (40, 40, 40))

    def test_a_dim_colour_is_lifted_to_the_floor_and_keeps_its_hue(self):
        self.assertEqual(dimmed((153, 114, 24)), (40, 30, 6))

    def test_a_bright_colour_is_plainly_scaled(self):
        self.assertEqual(dimmed((255, 190, 40)), (64, 48, 10))
        self.assertEqual(dimmed((200, 100, 50)), (50, 25, 13))

    def test_structure_below_the_content_threshold_still_fades(self):
        self.assertEqual(dimmed((24, 24, 30)), (6, 6, 8))
        self.assertEqual(dimmed((30, 30, 30)), (8, 8, 8))

    def test_the_content_threshold_is_exact(self):
        m = boardmod.CONTENT_MIN
        self.assertEqual(max(dimmed((m, m, m))), boardmod.NIGHT_FLOOR)
        self.assertLess(max(dimmed((m - 1, m - 1, m - 1))), boardmod.NIGHT_FLOOR)

    def test_black_stays_black(self):
        self.assertEqual(dimmed((0, 0, 0)), (0, 0, 0))

    def test_a_floor_of_zero_is_plain_scaling(self):
        self.assertEqual(dimmed(layouts.DIM, floor=0), (24, 24, 24))

    def test_a_floor_is_never_applied_at_full_brightness(self):
        img = layouts.render("quad", preview.busy())
        self.assertEqual(boardmod.dim_image(img, 1.0, floor=200).tobytes(), img.tobytes())

    def test_a_gentler_level_keeps_content_at_its_scaled_level_when_that_is_above_the_floor(self):
        self.assertEqual(dimmed((95, 95, 95), 0.6), (57, 57, 57))


class OnRealFrames(unittest.TestCase):
    """The property that matters, over what the panel is actually sent."""

    def frames(self):
        for name in layouts.LAYOUTS:
            for make in (preview.busy, preview.prefilling, preview.degraded):
                yield name, make, layouts.render(name, make())

    def test_everything_that_carries_content_stays_above_the_floor_and_black_stays_black(self):
        floor = boardmod.NIGHT_FLOOR
        for name, make, img in self.frames():
            out = boardmod.dim_image(img, 0.25)
            a, b = img.load(), out.load()
            for x in range(layouts.W):
                for y in range(layouts.H):
                    m = max(a[x, y])
                    if m == 0:
                        self.assertEqual(b[x, y], (0, 0, 0), (name, make.__name__, x, y))
                    elif m >= boardmod.CONTENT_MIN:
                        self.assertGreaterEqual(max(b[x, y]), floor, (name, make.__name__, x, y, a[x, y]))

    def test_no_pixel_gets_brighter_than_the_brighter_of_its_scaled_and_floor_levels(self):
        for name, make, img in self.frames():
            out = boardmod.dim_image(img, 0.25)
            a, b = img.load(), out.load()
            for x in range(layouts.W):
                for y in range(layouts.H):
                    m = max(a[x, y])
                    self.assertLessEqual(max(b[x, y]), max(round(m * 0.25), boardmod.NIGHT_FLOOR if m else 0) + 1,
                                         (name, x, y))

    def test_the_hue_of_a_pixel_survives(self):
        for name, make, img in self.frames():
            out = boardmod.dim_image(img, 0.25)
            a, b = img.load(), out.load()
            for x in range(layouts.W):
                for y in range(layouts.H):
                    src, dst = a[x, y], b[x, y]
                    if max(src) < boardmod.CONTENT_MIN:
                        continue
                    k = max(dst) / max(src)
                    for s, d in zip(src, dst):
                        self.assertLessEqual(abs(d - s * k), 1.0, (name, x, y, src, dst))

    def test_the_grey_labels_of_the_quad_layout_are_still_drawn(self):
        img = layouts.render("quad", preview.busy())
        out = boardmod.dim_image(img, 0.25)
        a, b = img.load(), out.load()
        labels = [(x, y) for x in range(layouts.W) for y in range(layouts.H) if a[x, y] == layouts.DIM]
        self.assertTrue(labels)
        self.assertTrue(all(b[x, y] == (40, 40, 40) for x, y in labels))


class SpecFloor(unittest.TestCase):
    def test_the_floor_is_optional_and_defaults(self):
        self.assertEqual(boardmod.Dimmer("20:00-07:30@0.25", NY).floor, boardmod.NIGHT_FLOOR)

    def test_a_floor_can_be_given(self):
        d = boardmod.Dimmer("20:00-07:30@0.25:55", NY)
        self.assertEqual((d.level, d.floor), (0.25, 55))
        self.assertEqual(boardmod.Dimmer("20:00-07:30@0.25:0", NY).floor, 0)
        self.assertEqual(boardmod.Dimmer("20:00-07:30@0.25:255", NY).floor, 255)

    def test_a_bad_floor_is_refused_with_a_reason(self):
        for spec in ("20:00-07:30@0.25:256", "20:00-07:30@0.25:", "20:00-07:30@0.25:x", "20:00-07:30@0.25:-1",
                     "20:00-07:30@0.25:4.5", "20:00-07:30@0.25:1000"):
            with self.assertRaises(ValueError, msg=spec):
                boardmod.Dimmer(spec, NY)

    def test_a_step_dims_with_the_dimmers_floor(self):
        class Spy:
            def __init__(self):
                self.sent = []

            def push(self, img):
                self.sent.append(img.tobytes())

        class Clock:
            def __call__(self):
                return 1000.0

        board = boardmod.Board(["studio1"], clock=Clock())
        board.ingest({"node": "studio1", "gpu": 80, "mem_gb": 128, "mem_total_gb": 256, "dec": 50.0, "pre": 0.0})
        from datetime import datetime
        from zoneinfo import ZoneInfo
        night = datetime(2026, 11, 10, 22, 20, tzinfo=ZoneInfo(NY)).timestamp()
        results = []
        for spec in ("20:00-07:30@0.25:20", "20:00-07:30@0.25:70"):
            spy = Spy()
            boardmod.step(board, spy, boardmod.Dimmer(spec, NY), night)
            results.append(spy.sent[0])
        frame = board.frame()[1]
        self.assertEqual(results[0], boardmod.dim_image(frame, 0.25, floor=20).tobytes())
        self.assertEqual(results[1], boardmod.dim_image(frame, 0.25, floor=70).tobytes())
        self.assertNotEqual(results[0], results[1])


if __name__ == "__main__":
    unittest.main()
