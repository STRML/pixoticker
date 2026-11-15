#!/usr/bin/env python3
"""Checks for tools/serial_port.py: which port the serial tools pick.

    python3 tools/test_serial_port.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serial_port  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def pick(ports):
    with mock.patch("glob.glob", return_value=ports):
        try:
            return serial_port.default_port()
        except SystemExit as e:
            return f"exit: {e}"


got = pick(["/dev/cu.usbserial-10"])
check("one adapter is picked whatever its number", got == "/dev/cu.usbserial-10", got)
got = pick([])
check("no adapter says so and exits", got.startswith("exit:") and "no USB serial" in got, got)
got = pick(["/dev/cu.usbserial-10", "/dev/cu.usbserial-210"])
check("two adapters name both and ask for --port",
      got.startswith("exit:") and "usbserial-10" in got and "usbserial-210" in got and "--port" in got, got)

print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
