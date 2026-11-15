#!/usr/bin/env python3
"""Send a line to the ticker UART and print its reply.

  tools/serial_cmd.py state
  tools/serial_cmd.py "panel 2 2 0 1"
  tools/serial_cmd.py test --seconds 3

Runs under PlatformIO's Python when the system python3 has no pyserial, and
picks the one /dev/cu.usbserial-* adapter unless --port names another. Opening
the port resets the panel (see tools/mon.py).
"""
import argparse
import time

from serial_port import default_port, ensure_pyserial


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command")
    ap.add_argument("--port")
    ap.add_argument("--seconds", type=float, default=1.5)
    args = ap.parse_args()

    ensure_pyserial()
    import serial

    s = serial.Serial(args.port or default_port(), 115200, timeout=0.2)
    s.write((args.command + "\n").encode())
    t = time.time()
    buf = b""
    while time.time() - t < args.seconds:
        buf += s.read(4096)
    s.close()
    print(buf.decode("utf-8", "replace").strip())


if __name__ == "__main__":
    main()
