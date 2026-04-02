#!/usr/bin/env python3
"""
Fetch GitHub data only → writes <data>/raw/*.json and manifest.json.
Run compute_metrics.py afterward to build tables and the dashboard.

  python fetch_data.py --data ./data --repo PostHog/posthog --days 90 \\
    --fetch-reviews --fetch-files --fetch-merge-checks --fetch-collaboration --fetch-actions

Resume after an interrupted run (reuses search_*.json and continues per-PR fetches):

  python fetch_data.py --data ./data --resume --repo PostHog/posthog --days 90 \\
    --fetch-reviews --fetch-files --fetch-merge-checks --fetch-collaboration --fetch-actions

Search is checkpointed after each time slice; merged-PR progress is written every
--checkpoint-every PRs so you can resume even without --resume if files exist
(use --resume to skip re-search and skip already-fetched PRs).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import gh_common as gh


def _load_json_list(path: Path) -> list:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _load_json_dict(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_merged_checkpoint(
    raw: Path,
    pulls_merged: list,
    files_by_pr: dict,
    reviews_by_pr: dict,
    collaboration_by_pr: dict,
    merge_checks_by_pr: dict,
) -> None:
    (raw / "pulls_merged.json").write_text(json.dumps(pulls_merged, indent=2), encoding="utf-8")
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
    p.add_argument(
        "--resume",
        action="store_true",
        help="Reuse existing search_*.json and per-PR JSON; fetch only missing merged/open PRs and checks.",
    )
    p.add_argument(
        "--checkpoint-every",
        type=int,
        default=25,
        metavar="N",
        help="While fetching merged PRs, write pulls_*.json and sidecars every N PRs (default: 25).",
    )
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

    ck_every = max(1, args.checkpoint_every)

    # --- Merged PR search ---
    merged_path = raw / "search_merged.json"
    merged_items: list[dict] = []
    if args.resume and merged_path.is_file() and merged_path.stat().st_size > 4:
        merged_items = _load_json_list(merged_path)
        if merged_items:
            print(
                f"[fetch] Resume: loaded {len(merged_items)} items from search_merged.json (skipping search)",
                file=sys.stderr,
            )
    if not merged_items:
        print(f"[fetch] Merged PR search {start}..{end}", file=sys.stderr)
        merged_items = gh.search_time_sliced(
            session,
            repo,
            extra_qualifiers="is:pr is:merged",
            date_field="merged",
            range_start=start,
            range_end=end,
            slice_days=args.slice_days,
            checkpoint_path=merged_path,
        )
    merged_nums = {int(x["number"]) for x in merged_items}
    merged_path.write_text(json.dumps(merged_items, indent=2), encoding="utf-8")

    # --- Open PR search ---
    open_path = raw / "search_open_updated.json"
    open_items: list[dict] = []
    if not args.skip_open:
        if args.resume and open_path.is_file() and open_path.stat().st_size > 4:
            open_items = _load_json_list(open_path)
            if open_items:
                print(
                    f"[fetch] Resume: loaded {len(open_items)} from search_open_updated.json",
                    file=sys.stderr,
                )
        if not open_items:
            print(f"[fetch] Open PRs updated {start}..{end}", file=sys.stderr)
            open_items = gh.search_time_sliced(
                session,
                repo,
                extra_qualifiers="is:pr is:open",
                date_field="updated",
                range_start=start,
                range_end=end,
                slice_days=args.slice_days,
                checkpoint_path=open_path,
            )
    open_path.write_text(json.dumps(open_items, indent=2), encoding="utf-8")

    # --- Created PR search ---
    created_path = raw / "search_created.json"
    created_items: list[dict] = []
    if not args.skip_created:
        if args.resume and created_path.is_file() and created_path.stat().st_size > 4:
            created_items = _load_json_list(created_path)
            if created_items:
                print(
                    f"[fetch] Resume: loaded {len(created_items)} from search_created.json",
                    file=sys.stderr,
                )
        if not created_items:
            print(f"[fetch] PRs created {start}..{end}", file=sys.stderr)
            created_items = gh.search_time_sliced(
                session,
                repo,
                extra_qualifiers="is:pr",
                date_field="created",
                range_start=start,
                range_end=end,
                slice_days=args.slice_days,
                checkpoint_path=created_path,
            )
    created_path.write_text(json.dumps(created_items, indent=2), encoding="utf-8")

    open_nums = [int(x["number"]) for x in open_items if int(x["number"]) not in merged_nums]
    open_nums = list(dict.fromkeys(open_nums))

    files_by_pr: dict[str, list] = _load_json_dict(raw / "pull_files.json") if args.resume else {}
    reviews_by_pr: dict[str, list] = _load_json_dict(raw / "pull_reviews.json") if args.resume else {}
    collaboration_by_pr: dict[str, dict] = (
        _load_json_dict(raw / "collaboration_by_pr.json") if args.resume else {}
    )
    merge_checks_by_pr: dict[str, dict[str, int]] = (
        _load_json_dict(raw / "merge_checks_by_pr.json") if args.resume else {}
    )

    pulls_merged: list[dict] = _load_json_list(raw / "pulls_merged.json") if args.resume else []
    if pulls_merged and not args.resume:
        pulls_merged = []

    done_merged: set[int] = set()
    for pobj in pulls_merged:
        n = pobj.get("number")
        if n is not None:
            done_merged.add(int(n))

    pending_merged = sorted(merged_nums - done_merged, reverse=True)
    print(
        f"[fetch] Merged PR payloads… ({len(done_merged)} cached, {len(pending_merged)} to fetch)",
        file=sys.stderr,
    )

    for i, num in enumerate(pending_merged, 1):
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
            print(f"  merged {i}/{len(pending_merged)}", file=sys.stderr)
        if i % ck_every == 0:
            _write_merged_checkpoint(
                raw, pulls_merged, files_by_pr, reviews_by_pr, collaboration_by_pr, merge_checks_by_pr
            )
            print(f"  checkpoint: wrote progress ({len(pulls_merged)} merged PRs)", file=sys.stderr)
        time.sleep(0.02)

    # Repair: PR in list but missing optional sidecars (e.g. resume after partial write)
    by_num = {int(p["number"]): p for p in pulls_merged if p.get("number") is not None}
    for num, pull in by_num.items():
        snum = str(num)
        if args.fetch_files and snum not in files_by_pr:
            try:
                files_by_pr[snum] = gh.fetch_pull_files(session, repo, num)
                time.sleep(0.04)
            except Exception as e:
                print(f"SKIP files PR#{num}: {e}", file=sys.stderr)
        if args.fetch_reviews and snum not in reviews_by_pr:
            try:
                reviews_by_pr[snum] = gh.fetch_pull_reviews(session, repo, num)
                time.sleep(0.04)
            except Exception as e:
                print(f"SKIP reviews PR#{num}: {e}", file=sys.stderr)
        if args.fetch_collaboration and snum not in collaboration_by_pr:
            try:
                ic = gh.fetch_issue_comments(session, repo, num)
                time.sleep(0.03)
                lc = gh.fetch_pull_line_comments(session, repo, num)
                time.sleep(0.03)
                collaboration_by_pr[snum] = gh.summarize_collaboration_on_pull(pull, ic, lc)
            except Exception as e:
                print(f"SKIP collaboration repair PR#{num}: {e}", file=sys.stderr)

    pulls_open: list[dict] = _load_json_list(raw / "pulls_open.json") if args.resume else []
    done_open: set[int] = set()
    for pobj in pulls_open:
        n = pobj.get("number")
        if n is not None:
            done_open.add(int(n))

    if open_nums:
        pending_open = [n for n in open_nums if n not in done_open]
        print(
            f"[fetch] Open PR payloads ({len(done_open)} cached, {len(pending_open)} to fetch)…",
            file=sys.stderr,
        )
        for i, num in enumerate(pending_open, 1):
            try:
                pulls_open.append(gh.fetch_pull(session, repo, num))
            except Exception as e:
                print(f"SKIP open PR#{num}: {e}", file=sys.stderr)
            time.sleep(0.02)
            if i % 40 == 0:
                print(f"  open {i}/{len(pending_open)}", file=sys.stderr)

    if args.fetch_merge_checks:
        done_checks = set(merge_checks_by_pr.keys())
        need_checks = [
            p
            for p in pulls_merged
            if p.get("merge_commit_sha") and str(p.get("number")) not in done_checks
        ]
        print(
            f"[fetch] Merge-commit check runs… ({len(done_checks)} cached, {len(need_checks)} to fetch)",
            file=sys.stderr,
        )
        for i, pull in enumerate(need_checks, 1):
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
                print(f"  merge checks {i}/{len(need_checks)}", file=sys.stderr)
            if i % ck_every == 0:
                (raw / "merge_checks_by_pr.json").write_text(
                    json.dumps(merge_checks_by_pr, indent=2), encoding="utf-8"
                )
            time.sleep(0.03)

    workflow_runs: list[dict] = []
    wf_path = raw / "workflow_runs_sample.json"
    if args.fetch_actions:
        if args.resume and wf_path.is_file() and wf_path.stat().st_size > 4:
            workflow_runs = _load_json_list(wf_path)
            if workflow_runs:
                print("[fetch] Resume: using existing workflow_runs_sample.json", file=sys.stderr)
        if not workflow_runs:
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
        wf_path.write_text(json.dumps(workflow_runs, indent=2), encoding="utf-8")

    manifest = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "window_days": args.days,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "resume": bool(args.resume),
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
