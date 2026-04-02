#!/usr/bin/env python3
"""
Fetch GitHub data only → writes <data>/raw/*.json and manifest.json.
Run compute_metrics.py afterward to build tables and the dashboard.

  python fetch_data.py --data ./data --repo PostHog/posthog --days 90 \\
    --fetch-reviews --fetch-files --fetch-merge-checks --fetch-collaboration --fetch-actions
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import gh_common as gh


def main() -> None:
    p = argparse.ArgumentParser(description="Fetch GitHub raw JSON into data/raw/.")
    p.add_argument("--data", type=Path, default=Path("./data"), help="Root folder (creates raw/ inside)")
    p.add_argument("--repo", default="PostHog/posthog")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--slice-days", type=int, default=7)
    p.add_argument("--fetch-files", action="store_true")
    p.add_argument("--fetch-reviews", action="store_true")
    p.add_argument("--fetch-collaboration", action="store_true", help="Issue + line comments (others after open)")
    p.add_argument("--fetch-actions", action="store_true")
    p.add_argument("--actions-max-pages", type=int, default=30)
    p.add_argument("--fetch-merge-checks", action="store_true")
    p.add_argument("--skip-open", action="store_true")
    p.add_argument("--skip-created", action="store_true")
    args = p.parse_args()

    root = args.data
    raw = root / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    if not gh.token():
        print("No GH_TOKEN/GITHUB_TOKEN — unauthenticated limits apply.", file=sys.stderr)

    end = date.today()
    start = end - timedelta(days=args.days)
    since_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    session = gh.make_session()
    repo = args.repo

    print(f"[fetch] Merged PR search {start}..{end}", file=sys.stderr)
    merged_items = gh.search_time_sliced(
        session,
        repo,
        extra_qualifiers="is:pr is:merged",
        date_field="merged",
        range_start=start,
        range_end=end,
        slice_days=args.slice_days,
    )
    merged_nums = {int(x["number"]) for x in merged_items}
    (raw / "search_merged.json").write_text(json.dumps(merged_items, indent=2), encoding="utf-8")

    open_items: list[dict] = []
    if not args.skip_open:
        print(f"[fetch] Open PRs updated {start}..{end}", file=sys.stderr)
        open_items = gh.search_time_sliced(
            session,
            repo,
            extra_qualifiers="is:pr is:open",
            date_field="updated",
            range_start=start,
            range_end=end,
            slice_days=args.slice_days,
        )
    (raw / "search_open_updated.json").write_text(json.dumps(open_items, indent=2), encoding="utf-8")

    created_items: list[dict] = []
    if not args.skip_created:
        print(f"[fetch] PRs created {start}..{end}", file=sys.stderr)
        created_items = gh.search_time_sliced(
            session,
            repo,
            extra_qualifiers="is:pr",
            date_field="created",
            range_start=start,
            range_end=end,
            slice_days=args.slice_days,
        )
    (raw / "search_created.json").write_text(json.dumps(created_items, indent=2), encoding="utf-8")

    open_nums = [int(x["number"]) for x in open_items if int(x["number"]) not in merged_nums]
    open_nums = list(dict.fromkeys(open_nums))

    files_by_pr: dict[str, list] = {}
    reviews_by_pr: dict[str, list] = {}
    collaboration_by_pr: dict[str, dict] = {}
    merge_checks_by_pr: dict[str, dict[str, int]] = {}

    pulls_merged: list[dict] = []
    print("[fetch] Merged PR payloads…", file=sys.stderr)
    for i, num in enumerate(sorted(merged_nums, reverse=True), 1):
        try:
            pull = gh.fetch_pull(session, repo, num)
        except Exception as e:
            print(f"SKIP merged PR#{num}: {e}", file=sys.stderr)
            continue
        pulls_merged.append(pull)
        snum = str(num)
        if args.fetch_files:
            files_by_pr[snum] = gh.fetch_pull_files(session, repo, num)
            time.sleep(0.04)
        if args.fetch_reviews:
            reviews_by_pr[snum] = gh.fetch_pull_reviews(session, repo, num)
            time.sleep(0.04)
        if args.fetch_collaboration:
            try:
                ic = gh.fetch_issue_comments(session, repo, num)
                time.sleep(0.03)
                lc = gh.fetch_pull_line_comments(session, repo, num)
                time.sleep(0.03)
                collaboration_by_pr[snum] = gh.summarize_collaboration_on_pull(pull, ic, lc)
            except Exception as e:
                print(f"SKIP collaboration PR#{num}: {e}", file=sys.stderr)
        if i % 40 == 0:
            print(f"  merged {i}/{len(merged_nums)}", file=sys.stderr)
        time.sleep(0.02)

    pulls_open: list[dict] = []
    if open_nums:
        print(f"[fetch] Open PR payloads ({len(open_nums)})…", file=sys.stderr)
        for i, num in enumerate(open_nums, 1):
            try:
                pulls_open.append(gh.fetch_pull(session, repo, num))
            except Exception as e:
                print(f"SKIP open PR#{num}: {e}", file=sys.stderr)
            time.sleep(0.02)
            if i % 40 == 0:
                print(f"  open {i}/{len(open_nums)}", file=sys.stderr)

    if args.fetch_merge_checks:
        print("[fetch] Merge-commit check runs…", file=sys.stderr)
        for i, pull in enumerate(pulls_merged, 1):
            sha = pull.get("merge_commit_sha")
            num = pull.get("number")
            if not sha or num is None:
                continue
            try:
                runs = gh.fetch_commit_check_runs(session, repo, sha)
            except Exception as e:
                print(f"SKIP checks {sha[:7] if sha else '?'}: {e}", file=sys.stderr)
                continue
            t = s = f = 0
            for r in runs:
                st = (r.get("status") or "").lower()
                if st != "completed":
                    continue
                t += 1
                con = (r.get("conclusion") or "").lower()
                if con == "success":
                    s += 1
                elif con == "failure":
                    f += 1
            merge_checks_by_pr[str(num)] = {"total": t, "success": s, "failure": f}
            if i % 25 == 0:
                print(f"  merge checks {i}/{len(pulls_merged)}", file=sys.stderr)
            time.sleep(0.03)

    workflow_runs: list[dict] = []
    if args.fetch_actions:
        print("[fetch] Workflow runs…", file=sys.stderr)
        workflow_runs = gh.fetch_workflow_runs(session, repo, since_dt, args.actions_max_pages)

    tips: list[str] = []
    if not args.fetch_files:
        tips.append("--fetch-files for test paths in delivery metrics")
    if not args.fetch_merge_checks:
        tips.append("--fetch-merge-checks for merge CI")
    if not args.fetch_collaboration:
        tips.append("--fetch-collaboration for teammate engagement after PR open")
    if tips:
        print("Tip: " + " · ".join(tips), file=sys.stderr)

    (raw / "pulls_merged.json").write_text(json.dumps(pulls_merged, indent=2), encoding="utf-8")
    (raw / "pulls_open.json").write_text(json.dumps(pulls_open, indent=2), encoding="utf-8")
    if files_by_pr:
        (raw / "pull_files.json").write_text(json.dumps(files_by_pr, indent=2), encoding="utf-8")
    if reviews_by_pr:
        (raw / "pull_reviews.json").write_text(json.dumps(reviews_by_pr, indent=2), encoding="utf-8")
    if collaboration_by_pr:
        (raw / "collaboration_by_pr.json").write_text(
            json.dumps(collaboration_by_pr, indent=2), encoding="utf-8"
        )
    if merge_checks_by_pr:
        (raw / "merge_checks_by_pr.json").write_text(
            json.dumps(merge_checks_by_pr, indent=2), encoding="utf-8"
        )
    if workflow_runs:
        (raw / "workflow_runs_sample.json").write_text(
            json.dumps(workflow_runs, indent=2), encoding="utf-8"
        )

    manifest = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "window_days": args.days,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "flags": {
            "fetch_files": args.fetch_files,
            "fetch_reviews": args.fetch_reviews,
            "fetch_collaboration": args.fetch_collaboration,
            "fetch_merge_checks": args.fetch_merge_checks,
            "fetch_actions": args.fetch_actions,
        },
    }
    (raw / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[fetch] Done → {raw}", file=sys.stderr)


if __name__ == "__main__":
    main()
