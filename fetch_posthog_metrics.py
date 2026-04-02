#!/usr/bin/env python3
"""
Runs fetch_data.py then compute_metrics.py (same flags, except --out maps to --data).

  python fetch_posthog_metrics.py --days 90 --data ./data --fetch-reviews --build-report

  --fetch-only    Only populate ./data/raw/
  --compute-only  Only build ./data/metrics/ from existing raw/
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    argv = sys.argv[1:]
    fetch_only = "--fetch-only" in argv
    compute_only = "--compute-only" in argv
    argv = [a for a in argv if a not in ("--fetch-only", "--compute-only")]
    build_report = "--build-report" in argv
    fetch_argv = [a for a in argv if a != "--build-report"]

    mapped: list[str] = []
    i = 0
    while i < len(fetch_argv):
        if fetch_argv[i] == "--out" and i + 1 < len(fetch_argv):
            mapped.extend(["--data", fetch_argv[i + 1]])
            i += 2
        else:
            mapped.append(fetch_argv[i])
            i += 1

    if not compute_only:
        sys.argv = ["fetch_data.py"] + mapped
        runpy.run_path(str(ROOT / "fetch_data.py"), run_name="__main__")
    if not fetch_only:
        c = ["compute_metrics.py"] + mapped
        if build_report:
            c.append("--build-report")
        sys.argv = c
        runpy.run_path(str(ROOT / "compute_metrics.py"), run_name="__main__")


if __name__ == "__main__":
    main()
