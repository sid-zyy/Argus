"""Replay a saved run through the same renderer (no network needed).

python -m orchestrator.replay [replay/incident1_conn_leak.json] [--speed 1.0]
"""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from orchestrator import console


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default="replay/incident1_conn_leak.json")
    ap.add_argument("--speed", type=float, default=4.0, help="playback speed multiplier; 0 = no delays")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    events = json.loads(Path(args.path).read_text(encoding="utf-8"))
    prev = None
    for e in events:
        t = datetime.fromisoformat(e["ts"])
        if prev and args.speed > 0:
            time.sleep(min(3.0, (t - prev).total_seconds() / args.speed))
        prev = t
        console.render(e, args.verbose)


if __name__ == "__main__":
    main()
