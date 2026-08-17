"""Remove blank, test, and duplicate empty bids from live HARALD."""
from __future__ import annotations

import argparse
import sys

import httpx

DEFAULT_BASE = "http://137.23.54.1:8000"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    base = args.base.rstrip("/")

    with httpx.Client(timeout=120.0) as client:
        r = client.post(f"{base}/api/proposals/cleanup-demos")
        if r.status_code == 404:
            print("Cleanup endpoint not deployed yet — restart the HARALD container, then rerun.", file=sys.stderr)
            return 1
        r.raise_for_status()
        data = r.json()
        removed = data.get("removed") or []
        print(f"Removed {len(removed)} bid(s):")
        for name in removed:
            print(f"  - {name}")
        print(f"Remaining: {data.get('remaining')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
