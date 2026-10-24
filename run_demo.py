"""Start the whole demo with one command: ShopFast, then Argus, then the self-check.

python run_demo.py              # shop on :8100, Argus on :8000; Ctrl+C stops both
python run_demo.py --no-shop    # Argus only

Anything already running on a port is left alone and reused, so this never starts a second Argus on the same
database (the database lock would refuse it anyway).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ARGUS_PORT, SHOP_PORT = 8000, 8100


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for(url: str, seconds: float = 40) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=3):
                return True
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.5)
    return False


def start(name: str, port: int, argv: list[str], procs: list) -> None:
    if port_open(port):
        print(f"  {name}: already running on :{port}, reusing it")
        return
    procs.append(subprocess.Popen([sys.executable, *argv], env=os.environ.copy()))
    print(f"  {name}: starting on :{port}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-shop", action="store_true", help="don't start ShopFast")
    ap.add_argument("--open", action="store_true", help="open the dashboard in the browser")
    a = ap.parse_args()
    procs: list[subprocess.Popen] = []
    print("Argus demo")
    try:
        if not a.no_shop:
            start("ShopFast", SHOP_PORT, ["-m", "shop.server"], procs)
        start("Argus", ARGUS_PORT, ["-m", "uvicorn", "ui.app:app", "--port", str(ARGUS_PORT)], procs)
        if not wait_for(f"http://localhost:{ARGUS_PORT}/argus/state"):
            print("Argus did not come up. Its output above says why.")
            return 1
        if not a.no_shop:
            wait_for(f"http://localhost:{SHOP_PORT}/api/status", 20)
        with urllib.request.urlopen(f"http://localhost:{ARGUS_PORT}/health", timeout=30) as r:
            report = json.loads(r.read())
        print()
        for c in report["checks"]:
            print(f"  {'ok  ' if c['status'] == 'ok' else 'WARN' if c['status'] == 'warn' else 'FAIL'}  {c['name']}: {c['detail']}")
        print(f"\nDashboard     http://localhost:{ARGUS_PORT}\nSystem check  http://localhost:{ARGUS_PORT}/check")
        if not a.no_shop:
            print(f"ShopFast      http://localhost:{SHOP_PORT}")
        if a.open:
            webbrowser.open(f"http://localhost:{ARGUS_PORT}")
        if not procs:
            return 0
        print("\nCtrl+C stops what this command started.")
        while all(p.poll() is None for p in procs):
            time.sleep(1)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
