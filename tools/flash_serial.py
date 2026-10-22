#!/usr/bin/env python3
"""Serial flash helper: recovery when the panel's Wi-Fi and OTA are unreachable.

  tools/flash_serial.py                # newest .pio/build/*/firmware.bin -> 0x10000
  tools/flash_serial.py --env safe     # pick a build env explicitly
  tools/flash_serial.py --dump out.bin # read the whole 4 MB flash
  tools/flash_serial.py --port /dev/ttyUSB0

Run --dump once before the first write: it is the only way back to the stock
firmware. The image path is resolved here so the command line carries no binary.
"""
import argparse
import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ESPTOOL = os.path.expanduser("~/.platformio/packages/tool-esptoolpy/esptool.py")
PORT = "/dev/cu.usbserial-210"
APP_OFFSET = "0x10000"
OTADATA_OFFSET = "0xe000"
OTADATA_SIZE = "0x2000"


def newest_bin(env=None):
    pattern = os.path.join(ROOT, ".pio", "build", env or "*", "firmware.bin")
    cands = [p for p in glob.glob(pattern) if os.path.isfile(p)]
    if not cands:
        sys.exit("no firmware.bin found: run pio run first")
    return max(cands, key=os.path.getmtime)


def run(port, args):
    cmd = [sys.executable, ESPTOOL, "--port", port, "--baud", "460800"] + args
    print("+", " ".join(cmd))
    return subprocess.call(cmd)


PENV_PYTHON = os.path.expanduser("~/.platformio/penv/bin/python")


def ensure_pyserial():
    # esptool needs pyserial, which PlatformIO's interpreter always carries.
    try:
        import serial  # noqa: F401
    except ImportError:
        if os.path.exists(PENV_PYTHON) and os.path.realpath(sys.executable) != os.path.realpath(PENV_PYTHON):
            os.execv(PENV_PYTHON, [PENV_PYTHON] + sys.argv)
        sys.exit("pyserial missing: run with ~/.platformio/penv/bin/python")


def main():
    ensure_pyserial()

    ap = argparse.ArgumentParser()
    ap.add_argument("--env")
    ap.add_argument("--dump")
    ap.add_argument("--offset", default=APP_OFFSET)
    ap.add_argument("--port", default=PORT)
    args = ap.parse_args()

    if args.dump:
        sys.exit(run(args.port, ["read_flash", "0", "0x400000", args.dump]))

    image = newest_bin(args.env)
    print(f"image {image} ({os.path.getsize(image)} bytes) -> {args.offset}")
    if args.offset == APP_OFFSET:
        # An earlier OTA leaves otadata pointing at app1, so the bootloader
        # would keep running that old image and ignore this write to app0.
        # Erase it before writing, without a reset in between: esptool 4.8
        # cannot chain these two commands, and erasing first means the chip
        # never boots with the new image written and the old slot selected.
        rc = run(args.port, ["--after", "no_reset", "erase_region", OTADATA_OFFSET, OTADATA_SIZE])
        if rc:
            sys.exit(rc)
    sys.exit(run(args.port, ["write_flash", args.offset, image]))


if __name__ == "__main__":
    main()
