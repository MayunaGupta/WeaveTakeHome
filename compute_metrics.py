#!/usr/bin/env python3
"""
Read data/raw/*.json and write data/metrics/ (tables + optional index.html).

  python compute_metrics.py --data ./data --build-report
"""

from __future__ import annotations

import argparse
import json
import math
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

    rows, summary, bundle, timeline_export = gh.compute_engineer_metrics(
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

    # Performance band from `full_stack_score` tertiles (High / Medium / Low).
    # This is intended for scheduling prioritization signals, not formal evaluation.
    scores = [r.get("full_stack_score") for r in rows if r.get("full_stack_score") is not None]
    scores = [float(s) for s in scores if isinstance(s, (int, float))]

    def _quantile(sorted_scores: list[float], p: float) -> float:
        n = len(sorted_scores)
        if n == 1:
            return sorted_scores[0]
        pos = (n - 1) * p
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            return sorted_scores[lo]
        w = pos - lo
        return sorted_scores[lo] * (1.0 - w) + sorted_scores[hi] * w

    q33 = q66 = None
    if len(scores) >= 3:
        sorted_scores = sorted(scores)
        q33 = _quantile(sorted_scores, 1 / 3)
        q66 = _quantile(sorted_scores, 2 / 3)

    for r in rows:
        s = r.get("full_stack_score")
        if s is None or q33 is None or q66 is None:
            r["performance_band"] = "Medium" if s is not None else "—"
            continue
        if float(s) >= q66:
            r["performance_band"] = "High"
        elif float(s) <= q33:
            r["performance_band"] = "Low"
        else:
            r["performance_band"] = "Medium"

    summary["performance_band"] = {
        "method": "tertiles on full_stack_score within the computed window",
        "q33": q33,
        "q66": q66,
    }

    (metrics_dir / "metrics_by_author.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    gh.write_csv(metrics_dir / "metrics_by_author.csv", rows)
    gh.write_csv(metrics_dir / "engineers_for_notion.csv", rows)
    (metrics_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (metrics_dir / "timeline_by_author.json").write_text(
        json.dumps(timeline_export, indent=2), encoding="utf-8"
    )

    if args.build_report:
        gh.build_report(metrics_dir, repo, bundle, timeline_export)

    print(f"[compute] Wrote → {metrics_dir}", file=sys.stderr)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
