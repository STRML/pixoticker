#!/usr/bin/env python3
"""Individual pixel writes over HTTP.

  tools/pixels_demo.py [host]        # draws a moving band across the panel
  tools/pixels_demo.py [host] x y r g b
"""
import sys
import time
import urllib.request

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.4.1"
BASE = f"http://{HOST}"


def post(path, body, timeout=10):
    # text/plain keeps the body in the "plain" arg; form-encoding would split it.
    req = urllib.request.Request(BASE + path, data=body.encode(), method="POST",
                                 headers={"Content-Type": "text/plain"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def state():
    with urllib.request.urlopen(BASE + "/state", timeout=10) as r:
        return r.read().decode()


def main():
    print(state())
    if len(sys.argv) == 7:
        x, y, r, g, b = map(int, sys.argv[2:7])
        print(post("/pixel", f"{x},{y},{r},{g},{b}"))
        return
    s = state()
    import json

    w = json.loads(s)["width"]
    h = json.loads(s)["height"]
    for x in range(w):
        lines = "\n".join(f"{x},{y},0,120,255" for y in range(h))
        post("/pixels", lines)
        time.sleep(0.03)
    print("band done")


if __name__ == "__main__":
    main()
