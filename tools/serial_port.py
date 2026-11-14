"""Shared by the serial tools: run under PlatformIO's Python, and find the port.

The system python3 on macOS has no pyserial. PlatformIO's interpreter always
carries it, so a tool that cannot import serial re-runs itself under that one.
"""
import glob
import os
import sys

PENV_PYTHON = os.path.expanduser("~/.platformio/penv/bin/python")


def ensure_pyserial():
    try:
        import serial  # noqa: F401
    except ImportError:
        if os.path.exists(PENV_PYTHON) and os.path.realpath(sys.executable) != os.path.realpath(PENV_PYTHON):
            os.execv(PENV_PYTHON, [PENV_PYTHON] + sys.argv)
        sys.exit(f"pyserial missing: run with {PENV_PYTHON}")


def default_port():
    """The one USB serial adapter plugged in. Its number changes with the USB
    port it sits in (usbserial-10 here, usbserial-210 elsewhere)."""
    ports = sorted(glob.glob("/dev/cu.usbserial-*"))
    if len(ports) == 1:
        return ports[0]
    if not ports:
        sys.exit("no USB serial adapter at /dev/cu.usbserial-*: plug it in or pass --port")
    sys.exit(f"several USB serial adapters ({', '.join(ports)}): pass --port")
