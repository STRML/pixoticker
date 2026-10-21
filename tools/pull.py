#!/usr/bin/env python3
"""Pull frames/ops from the relay and forward them to the panel.

  tools/pull.py 192.168.4.1
  tools/pull.py 192.168.4.1 --relay https://relay.example.com/pxt

Runs forever; long-polls the relay, applies the newest frame and any queued
ops. The panel API stays private to the local network.
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.request

KEY_PATH = os.environ.get("PXT_KEY_FILE", os.path.expanduser("~/.config/pxt/relay.key"))


def key():
    with open(KEY_PATH) as f:
        return f.read().strip()


def relay_get(base, path, k, timeout=35):
    req = urllib.request.Request(base + path, headers={"X-Api-Key": k})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def panel_post(host, path, body, ctype="text/plain", timeout=10):
    req = urllib.request.Request(
        f"http://{host}{path}", data=body, method="POST", headers={"Content-Type": ctype}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def apply_ops(host, ops):
    for op in ops:
        t = op.get("type")
        if t == "pixel":
            body = f"{op['x']},{op['y']},{op.get('r',0)},{op.get('g',0)},{op.get('b',0)}"
            panel_post(host, "/pixel", body.encode())
        elif t == "pixels":
            panel_post(host, "/pixels", op["data"].encode())
        elif t == "brightness":
            panel_post(host, f"/brightness?level={int(op['level'])}", b"")
        else:
            print("unknown op", t)
        print("op", t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host", nargs="?", default="192.168.4.1")
    ap.add_argument("--relay", default=os.environ.get("PXT_RELAY_URL", ""),
                    help="relay base URL, for example https://relay.example.com/pxt")
    args = ap.parse_args()
    if not args.relay:
        ap.error("pass --relay or set PXT_RELAY_URL")

    k = key()
    print("puller up", flush=True)
    since = 0
    while True:
        try:
            snap = relay_get(args.relay, f"/next?since={since}&wait=2", k, timeout=12)
            applied = False
            if snap.get("frame") and snap["seq"] > since:
                raw = base64.b64decode(snap["frame"])
                fmt = "rgb888" if snap.get("fmt") == "888" else "rgb565"
                for attempt in (1, 2):
                    try:
                        panel_post(args.host, f"/frame?fmt={fmt}", snap["frame"].encode())
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        time.sleep(0.03)
                applied = True
                print(f"frame seq={snap['seq']} {len(raw)}B", flush=True)
            if snap.get("ops"):
                apply_ops(args.host, snap["ops"])
                applied = True
            since = snap.get("seq", since)
            time.sleep(0.05 if applied else 0.4)
        except Exception as e:
            print("retry:", e, file=sys.stderr)
            time.sleep(2)


if __name__ == "__main__":
    main()
