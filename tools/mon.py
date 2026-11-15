#!/usr/bin/env python3
"""Boot-log capture for the ticker UART.

  tools/mon.py                 # reset and print 8 s of log
  tools/mon.py --seconds 20
  tools/mon.py --no-reset      # skip the extra reset pulse (see below)

Runs under PlatformIO's Python when the system python3 has no pyserial, and
picks the one /dev/cu.usbserial-* adapter unless --port names another.

Opening the port resets the panel's adapter even with --no-reset: macOS raises
DTR and RTS on open, before this script can drop them, and RTS drives EN.
--no-reset only skips the second pulse. Every run is a reboot.
"""
import argparse
import time

from serial_port import default_port, ensure_pyserial


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=8)
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--port")
    args = ap.parse_args()

    ensure_pyserial()
    import serial

    s = serial.Serial(args.port or default_port(), 115200, timeout=0.2)
    # Opening a port asserts DTR/RTS; RTS is wired to EN on this adapter.
    # Release both so the chip is not held in reset while we listen.
    s.setDTR(False)
    s.setRTS(False)
    if not args.no_reset:
        s.setRTS(True)
        time.sleep(0.1)
        s.setRTS(False)
    t = time.time()
    buf = b""
    while time.time() - t < args.seconds:
        buf += s.read(4096)
    s.close()
    print(buf.decode("utf-8", "replace"))


if __name__ == "__main__":
    main()
