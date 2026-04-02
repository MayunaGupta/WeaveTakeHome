#!/usr/bin/env python3
"""
Read data/raw/*.json and write data/metrics/ (tables + optional index.html).

  python compute_metrics.py --data ./data --build-report
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import gh_common as gh


def main() -> None:
    p = argparse.ArgumentParser(description="Compute engineer metrics from data/raw/.")
    p.add_argument("--data", type=Path, default=Path("./data"), help="Root folder with raw/ and metrics/")
    p.add_argument("--build-report", action="store_true", help="Write index.html into metrics/")
    args = p.parse_args()

    raw = args.data / "raw"
    metrics_dir = args.data / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)

    if not (raw / "manifest.json").is_file():
        print(f"Missing {raw / 'manifest.json'} — run fetch_data.py first.", file=sys.stderr)
        sys.exit(1)

    manifest = gh.load_json(raw / "manifest.json", {})
    repo = manifest.get("repo", "unknown/repo")
    window_days = int(manifest.get("window_days", 90))

    ws = manifest.get("window_start")
    we = manifest.get("window_end")
    window_start = date.fromisoformat(ws) if ws else date.today()
    window_end = date.fromisoformat(we) if we else date.today()

    pulls_merged = gh.load_json(raw / "pulls_merged.json", [])
    if not pulls_merged:
        print("No pulls_merged.json or empty — nothing to compute.", file=sys.stderr)
        sys.exit(1)

    pulls_open = gh.load_json(raw / "pulls_open.json", [])
    created_items = gh.load_json(raw / "search_created.json", [])
    opened_by_author: dict[str, int] = {}
    for it in created_items:
        u = (it.get("user") or {}).get("login")
        if gh.is_bot_login(u):
            continue
        opened_by_author[u] = opened_by_author.get(u, 0) + 1

    files_by_pr = gh.load_json(raw / "pull_files.json", {})
    reviews_by_pr = gh.load_json(raw / "pull_reviews.json", {})
    workflow_runs = gh.load_json(raw / "workflow_runs_sample.json", [])
    collaboration_by_pr = gh.load_json(raw / "collaboration_by_pr.json", {})
    merge_checks_by_pr = gh.load_json(raw / "merge_checks_by_pr.json", {})

    rows, summary, bundle = gh.compute_engineer_metrics(
        repo=repo,
        window_start=window_start,
        window_end=window_end,
        window_days=window_days,
        pulls_merged=pulls_merged,
        pulls_open=pulls_open,
        created_items=created_items,
        opened_by_author=opened_by_author,
        files_by_pr=files_by_pr if isinstance(files_by_pr, dict) else {},
        reviews_by_pr=reviews_by_pr if isinstance(reviews_by_pr, dict) else {},
        workflow_runs=workflow_runs if isinstance(workflow_runs, list) else [],
        collaboration_by_pr=collaboration_by_pr if isinstance(collaboration_by_pr, dict) else {},
        merge_checks_by_pr=merge_checks_by_pr if isinstance(merge_checks_by_pr, dict) else {},
    )

    (metrics_dir / "metrics_by_author.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    gh.write_csv(metrics_dir / "metrics_by_author.csv", rows)
    gh.write_csv(metrics_dir / "engineers_for_notion.csv", rows)
    (metrics_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if args.build_report:
        gh.build_report(metrics_dir, repo, bundle)

    print(f"[compute] Wrote → {metrics_dir}", file=sys.stderr)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
