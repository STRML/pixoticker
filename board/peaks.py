"""Each metric's usual peak, learned by watching.

A bar is only useful against a scale, and the scale differs by node and by how the work is split: four
Studios in tensor parallel decode at a different rate from one alone. The peak is kept per key (the node
that serves, and how many nodes share the job) and per metric. It is the highest value that held for HOLD
samples in a row, so a one-sample spike cannot set the scale, and it decays with a half-life of three days,
so a node that got slower (a new pack, another mode) is not measured against a record it can no longer reach.
Until LEARN_N such samples have been seen the caller's default scale applies. The state can be saved to a
file, so a restart does not start the learning over.
"""
from __future__ import annotations

import json
import os
import time
from collections import deque

HALF_LIFE_S = 3 * 86400
HOLD = 3
LEARN_N = 60
SAVE_EVERY_S = 60


class Peaks:
    def __init__(self, path="", clock=time.time):
        self.path, self.clock = path, clock
        self.peak = {}      # (key, metric) -> [value, when it was set, samples seen]
        self.recent = {}    # (key, metric) -> the last HOLD live values
        self.dirty, self.saved_at, self.failing = False, 0.0, False
        if path:
            self.load()

    def _decayed(self, entry, now):
        return entry[0] * 0.5 ** (max(0.0, now - entry[1]) / HALF_LIFE_S)

    def observe(self, key, metric, value):
        """One live sample a second. Zero or nothing ends the run that has to hold."""
        slot = (key, metric)
        if not value or value <= 0:
            self.recent.pop(slot, None)
            return
        run = self.recent.setdefault(slot, deque(maxlen=HOLD))
        run.append(value)
        if len(run) < HOLD:
            return
        held, now = min(run), self.clock()
        entry = self.peak.get(slot)
        self.peak[slot] = [held, now, 1] if entry is None else [max(self._decayed(entry, now), held), now, entry[2] + 1]
        self.dirty = True

    def get(self, key, metric, seed):
        """The learned peak, or `seed` until enough has been watched."""
        entry = self.peak.get((key, metric))
        if entry is None or entry[2] < LEARN_N:
            return seed
        return self._decayed(entry, self.clock())

    def save_if_due(self):
        now = self.clock()
        if not (self.path and self.dirty and now - self.saved_at >= SAVE_EVERY_S):
            return False
        body = {"peaks": {f"{k}|{m}": e for (k, m), e in self.peak.items()}}
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(body, f)
            os.replace(tmp, self.path)
        except OSError as err:
            if not self.failing:
                print(f"peaks not saved to {self.path}: {err}", flush=True)
            self.failing = True
            return False
        self.dirty, self.saved_at, self.failing = False, now, False
        return True

    def load(self):
        try:
            with open(self.path) as f:
                raw = json.load(f).get("peaks", {})
        except FileNotFoundError:
            return
        except (OSError, ValueError, AttributeError) as err:
            print(f"peaks file {self.path} ignored: {err!r}", flush=True)
            return
        for name, entry in raw.items() if isinstance(raw, dict) else []:
            key, _, metric = name.rpartition("|")
            ok = (key and metric and isinstance(entry, list) and len(entry) == 3
                  and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in entry)
                  and entry[0] > 0)
            if ok:
                self.peak[(key, metric)] = [float(entry[0]), float(entry[1]), int(entry[2])]
