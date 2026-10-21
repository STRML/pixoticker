#!/usr/bin/env python3
"""Send a line to the ticker UART and print its reply.

  tools/serial_cmd.py state
  tools/serial_cmd.py "panel 2 2 0 1"
  tools/serial_cmd.py test --seconds 3
"""
import argparse
import time

PORT = "/dev/cu.usbserial-210"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command")
    ap.add_argument("--port", default=PORT)
    ap.add_argument("--seconds", type=float, default=1.5)
    args = ap.parse_args()

    import serial

    s = serial.Serial(args.port, 115200, timeout=0.2)
    s.write((args.command + "\n").encode())
    t = time.time()
    buf = b""
    while time.time() - t < args.seconds:
        buf += s.read(4096)
    s.close()
    print(buf.decode("utf-8", "replace").strip())


if __name__ == "__main__":
    main()
