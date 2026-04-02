"""
Shared GitHub API helpers, scoring, and metric aggregation for fetch_data / compute_metrics.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

GITHUB_API = "https://api.github.com"


def token() -> str | None:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
    t = token()
    if t:
        s.headers["Authorization"] = f"Bearer {t}"
    return s


def sleep_for_rate_limit(resp: requests.Response) -> None:
    if resp.status_code != 403:
        return
    remaining = resp.headers.get("X-RateLimit-Remaining")
    if remaining != "0":
        return
    reset = resp.headers.get("X-RateLimit-Reset")
    if reset and reset.isdigit():
        import time

        wake = int(reset) + 1
        now = int(time.time())
        time.sleep(max(0, wake - now) + 1)


def get_json(session: requests.Session, url: str, params: dict | None = None) -> Any:
    import time

    while True:
        r = session.get(url, params=params or {}, timeout=90)
        sleep_for_rate_limit(r)
        if r.status_code == 403 and token() is None:
            print(
                "Warning: 403 — set GH_TOKEN or GITHUB_TOKEN for higher limits.",
                file=sys.stderr,
            )
        if r.status_code in (403, 429):
            time.sleep(5)
            continue
        r.raise_for_status()
        return r.json()


def is_bot_login(login: str | None) -> bool:
    if not login:
        return True
    l = login.lower()
    if l.endswith("[bot]") or "[bot]" in l:
        return True
    if l in {"dependabot", "renovate", "github-actions", "cursor", "copilot"}:
        return True
    return False


def search_issues_slice(session: requests.Session, query: str) -> list[dict[str, Any]]:
    page = 1
    out: list[dict[str, Any]] = []
    while True:
        data = get_json(
            session,
            f"{GITHUB_API}/search/issues",
            params={"q": query, "per_page": 100, "page": page},
        )
        items = data.get("items") or []
        if not items:
            break
        out.extend(items)
        if len(items) < 100:
            break
        page += 1
        if len(out) >= 1000:
            print(f"WARNING: query hit 1000 cap: {query[:120]}…", file=sys.stderr)
            break
    return out


def search_time_sliced(
    session: requests.Session,
    repo: str,
    *,
    extra_qualifiers: str,
    date_field: str,
    range_start: date,
    range_end: date,
    slice_days: int,
    checkpoint_path: Path | None = None,
) -> list[dict[str, Any]]:
    """
    If checkpoint_path is set, writes the accumulated unique items to that file after each
    time slice so a long search can be resumed from partial results if the process stops.
    """
    seen: dict[int, dict[str, Any]] = {}
    d = range_start
    while d <= range_end:
        slice_end = min(d + timedelta(days=slice_days - 1), range_end)
        q = (
            f"repo:{repo} {extra_qualifiers} "
            f"{date_field}:{d.isoformat()}..{slice_end.isoformat()}"
        )
        items = search_issues_slice(session, q)
        for it in items:
            num = int(it["number"])
            if num not in seen:
                seen[num] = it
        print(
            f"  {date_field} {d}..{slice_end}: +{len(items)} (unique {len(seen)})",
            file=sys.stderr,
        )
        if checkpoint_path is not None:
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            checkpoint_path.write_text(
                json.dumps(list(seen.values()), indent=2),
                encoding="utf-8",
            )
        d = slice_end + timedelta(days=1)
    return list(seen.values())


def fetch_pull(session: requests.Session, repo: str, number: int) -> dict[str, Any]:
    owner, name = repo.split("/", 1)
    url = f"{GITHUB_API}/repos/{owner}/{name}/pulls/{number}"
    return get_json(session, url)


def fetch_pull_files(session: requests.Session, repo: str, number: int) -> list[dict]:
    owner, name = repo.split("/", 1)
    url = f"{GITHUB_API}/repos/{owner}/{name}/pulls/{number}/files"
    page = 1
    files: list[dict] = []
    while True:
        chunk = get_json(session, url, params={"per_page": 100, "page": page})
        if not chunk:
            break
        files.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return files


def fetch_pull_reviews(session: requests.Session, repo: str, number: int) -> list[dict]:
    owner, name = repo.split("/", 1)
    url = f"{GITHUB_API}/repos/{owner}/{name}/pulls/{number}/reviews"
    page = 1
    reviews: list[dict] = []
    while True:
        chunk = get_json(session, url, params={"per_page": 100, "page": page})
        if not chunk:
            break
        reviews.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return reviews


def fetch_issue_comments(session: requests.Session, repo: str, number: int) -> list[dict[str, Any]]:
    """Timeline issue comments on a PR (PRs are issues)."""
    owner, name = repo.split("/", 1)
    url = f"{GITHUB_API}/repos/{owner}/{name}/issues/{number}/comments"
    page = 1
    out: list[dict[str, Any]] = []
    while True:
        chunk = get_json(session, url, params={"per_page": 100, "page": page})
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return out


def fetch_pull_line_comments(session: requests.Session, repo: str, number: int) -> list[dict[str, Any]]:
    """Review comments on diff lines."""
    owner, name = repo.split("/", 1)
    url = f"{GITHUB_API}/repos/{owner}/{name}/pulls/{number}/comments"
    page = 1
    out: list[dict[str, Any]] = []
    while True:
        chunk = get_json(session, url, params={"per_page": 100, "page": page})
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return out


CLOSING_ISSUE_RE = re.compile(
    r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s*:?\s*#(\d+)",
    re.IGNORECASE,
)


def parse_closing_issue_refs(text: str) -> set[int]:
    if not text:
        return set()
    found: set[int] = set()
    for m in CLOSING_ISSUE_RE.finditer(text):
        try:
            found.add(int(m.group(1)))
        except ValueError:
            continue
    return found


def is_test_related_path(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    if "__tests__" in p or "/tests/" in p or p.startswith("tests/"):
        return True
    base = p.rsplit("/", 1)[-1]
    if base.startswith("test_") and base.endswith(".py"):
        return True
    if base.startswith("tests.") or base.startswith("test."):
        return True
    if ".test." in base or ".spec." in base:
        return True
    if any(x in p for x in (".cy.", ".e2e.", "/cypress/", "/playwright/", "/e2e/")):
        return True
    if p.endswith("_test.go") or p.endswith("_test.rs"):
        return True
    if "/jest/" in p or "jest.config" in p or "pytest.ini" in p or "vitest.config" in p:
        return True
    return False


def fetch_commit_check_runs(session: requests.Session, repo: str, sha: str) -> list[dict[str, Any]]:
    if not sha:
        return []
    owner, name = repo.split("/", 1)
    url = f"{GITHUB_API}/repos/{owner}/{name}/commits/{sha}/check-runs"
    page = 1
    out: list[dict[str, Any]] = []
    while True:
        data = get_json(session, url, params={"per_page": 100, "page": page})
        runs = data.get("check_runs") or []
        if not runs:
            break
        out.extend(runs)
        if len(runs) < 100:
            break
        page += 1
    return out


def fetch_workflow_runs(
    session: requests.Session,
    repo: str,
    since: datetime,
    max_pages: int,
) -> list[dict]:
    owner, name = repo.split("/", 1)
    url = f"{GITHUB_API}/repos/{owner}/{name}/actions/runs"
    since_aware = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        data = get_json(session, url, params={"per_page": 100, "page": page})
        runs = data.get("workflow_runs") or []
        if not runs:
            break
        stop_page = False
        for run in runs:
            ca = run.get("created_at")
            if not ca:
                continue
            dt = datetime.fromisoformat(ca.replace("Z", "+00:00"))
            if dt < since_aware:
                stop_page = True
                break
            out.append(run)
        if stop_page or len(runs) < 100:
            break
    return out


def path_weight(filename: str) -> float:
    f = filename.lower()
    if any(
        x in f
        for x in (
            "pnpm-lock",
            "package-lock",
            "yarn.lock",
            "poetry.lock",
            "uv.lock",
            ".snap",
            ".png",
            ".jpg",
            ".webp",
            ".gif",
        )
    ):
        return 0.15
    if f.endswith(".md") and f.count("/") <= 1:
        return 0.35
    if f.startswith("docs/") or "/docs/" in f:
        return 0.45
    if f.startswith("ee/") or "/ee/" in f:
        return 1.25
    if f.startswith("posthog/") or "/posthog/" in f:
        return 1.15
    if f.startswith("frontend/") or "/frontend/" in f or f.startswith("products/"):
        return 1.1
    if f.endswith(".py") or f.endswith(".tsx") or f.endswith(".ts") or f.endswith(".rs"):
        return 1.05
    return 1.0


def label_boost(labels: list[dict]) -> float:
    names = {str(l.get("name", "")).lower() for l in labels}
    boost = 1.0
    if any("bug" in n or "fix" in n for n in names):
        boost += 0.2
    if any("security" in n for n in names):
        boost += 0.35
    if any("breaking" in n for n in names):
        boost += 0.15
    if any("priority" in n or "p0" in n or "p1" in n for n in names):
        boost += 0.1
    return boost


def shipping_impact_score(
    pull: dict,
    files: list[dict] | None,
) -> tuple[float, dict[str, Any]]:
    additions = int(pull.get("additions") or 0)
    deletions = int(pull.get("deletions") or 0)
    changed_files = int(pull.get("changed_files") or 0)
    labels = pull.get("labels") or []

    churn = additions + deletions
    size_component = math.sqrt(math.log1p(churn)) * min(1.0, math.log1p(changed_files) / 4)

    if files:
        wsum = sum(path_weight(f.get("filename", "")) for f in files)
        path_mult = wsum / max(len(files), 1)
    else:
        path_mult = 1.0

    lb = label_boost(labels if isinstance(labels, list) else [])
    score = size_component * path_mult * lb

    breakdown = {
        "additions": additions,
        "deletions": deletions,
        "changed_files": changed_files,
        "size_component": round(size_component, 4),
        "path_multiplier": round(path_mult, 4),
        "label_multiplier": round(lb, 4),
        "score": round(score, 4),
    }
    return score, breakdown


def parse_github_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def summarize_collaboration_on_pull(
    pull: dict,
    issue_comments: list[dict[str, Any]],
    line_comments: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Others’ involvement after the PR was opened: non-author, non-bot comments
    with created_at strictly after PR created_at.
    """
    author = (pull.get("user") or {}).get("login")
    pr_open = parse_github_dt(pull.get("created_at"))
    partners: set[str] = set()

    ic_other_after = 0
    issue_by_login: dict[str, int] = defaultdict(int)
    for c in issue_comments:
        u = (c.get("user") or {}).get("login")
        if is_bot_login(u) or not u or u == author:
            continue
        ct = parse_github_dt(c.get("created_at"))
        if pr_open and ct and ct <= pr_open:
            continue
        ic_other_after += 1
        issue_by_login[u] += 1
        partners.add(u)

    rc_other_after = 0
    line_by_login: dict[str, int] = defaultdict(int)
    for c in line_comments:
        u = (c.get("user") or {}).get("login")
        if is_bot_login(u) or not u or u == author:
            continue
        ct = parse_github_dt(c.get("created_at"))
        if pr_open and ct and ct <= pr_open:
            continue
        rc_other_after += 1
        line_by_login[u] += 1
        partners.add(u)

    reviewers = pull.get("requested_reviewers") or []
    req_count = len(reviewers) if isinstance(reviewers, list) else 0
    assignees = pull.get("assignees") or []
    assign_non_author = sum(
        1 for a in assignees if (a.get("login") if isinstance(a, dict) else None) not in (None, author)
    )

    return {
        "issue_comments_from_others_after_open": ic_other_after,
        "review_line_comments_from_others_after_open": rc_other_after,
        "issue_comments_by_login_after_open": dict(issue_by_login),
        "review_line_comments_by_login_after_open": dict(line_by_login),
        "requested_reviewers_at_fetch": req_count,
        "assignees_non_author": assign_non_author,
        "distinct_partner_logins": sorted(partners),
    }


@dataclass
class AuthorAgg:
    login: str
    median_cycle_days: float | None = None
    merged_prs: int = 0
    shipping_score: float = 0.0
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    pr_numbers: list[int] = field(default_factory=list)
    review_events_on_others_prs: int = 0
    review_score: float = 0.0
    commits_on_merged_prs: int = 0
    cycle_days: list[float] = field(default_factory=list)
    issue_comments_merged: int = 0
    review_comments_merged: int = 0
    revert_merges: int = 0
    draft_merges: int = 0
    max_changed_files_single_pr: int = 0
    open_prs_updated: int = 0
    open_additions: int = 0
    open_deletions: int = 0
    open_changed_files: int = 0
    open_commits: int = 0
    open_discussion: int = 0
    prs_opened_in_window: int = 0
    workflow_runs: int = 0
    prs_with_close_keyword: int = 0
    linked_issue_refs: int = 0
    prs_touching_tests: int = 0
    test_files_touched: int = 0
    merge_check_runs_total: int = 0
    merge_check_runs_success: int = 0
    merge_check_runs_failure: int = 0
    collab_issue_comments_others_after_open: int = 0
    collab_line_comments_others_after_open: int = 0
    collab_requested_reviewers_sum: int = 0
    collab_assignees_non_author_sum: int = 0
    collab_prs_with_outside_input_after_open: int = 0
    collab_partners: set[str] = field(default_factory=set)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def report_template_path() -> Path:
    return Path(__file__).resolve().parent / "report" / "template.html"


def build_report(
    out: Path,
    repo: str,
    bundle: dict[str, Any],
    timeline: dict[str, Any] | None = None,
) -> None:
    tpl_path = report_template_path()
    if not tpl_path.is_file():
        print(f"No template at {tpl_path}; skip report.", file=sys.stderr)
        return
    html = tpl_path.read_text(encoding="utf-8")
    html = html.replace("__REPO__", repo)
    payload = json.dumps(bundle, ensure_ascii=False)
    payload = payload.replace("</", "<\\/")
    html = html.replace("__PAYLOAD__", payload)
    tl = timeline or {
        "authors": {},
        "note": "",
        "window_start": "",
        "window_end": "",
        "window_calendar_days": 0,
    }
    timeline_json = json.dumps(tl, ensure_ascii=False)
    timeline_json = timeline_json.replace("</", "<\\/")
    html = html.replace("__TIMELINE_PAYLOAD__", timeline_json)

    docs_index = Path(__file__).resolve().parent / "docs" / "index.html"
    for dest in (out / "index.html", docs_index):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(html, encoding="utf-8")
    print(f"Wrote report: {out / 'index.html'} and {docs_index}", file=sys.stderr)


def merge_check_success_pct(a: AuthorAgg) -> float | None:
    if not a.merge_check_runs_total:
        return None
    return round(100.0 * a.merge_check_runs_success / a.merge_check_runs_total, 1)


def collaboration_score(a: AuthorAgg) -> float:
    """Reward others engaging on your PRs after open + breadth of partners."""
    s = 0.0
    s += min(math.log1p(a.collab_issue_comments_others_after_open) * 2.2, 14.0)
    s += min(math.log1p(a.collab_line_comments_others_after_open) * 1.8, 16.0)
    s += min(a.collab_prs_with_outside_input_after_open * 1.0, 14.0)
    s += min(math.log1p(len(a.collab_partners)) * 2.8, 14.0)
    s += min(a.collab_requested_reviewers_sum * 0.25, 6.0)
    s += min(a.collab_assignees_non_author_sum * 0.2, 5.0)
    return round(s, 3)


def delivery_score(a: AuthorAgg) -> float:
    s = 0.0
    s += min(a.prs_with_close_keyword * 2.0, 25.0)
    s += min(a.linked_issue_refs * 0.35, 20.0)
    s += min(a.prs_touching_tests * 1.5, 18.0)
    s += min(math.log1p(max(a.test_files_touched, 0)), 8.0)
    pct = merge_check_success_pct(a)
    if pct is not None and a.merge_check_runs_total > 0:
        s += (pct / 100.0) * 12.0
    return round(s, 3)


def engineer_note(a: AuthorAgg) -> str:
    parts: list[str] = []
    if a.merged_prs and a.median_cycle_days is not None:
        parts.append(f"~{a.median_cycle_days}d median time to merge")
    if a.commits_on_merged_prs and a.merged_prs:
        avg = a.commits_on_merged_prs / a.merged_prs
        if avg >= 4:
            parts.append("high iteration (commits/PR)")
    if a.review_events_on_others_prs >= 10:
        parts.append("strong review throughput")
    if a.workflow_runs >= 20:
        parts.append("frequent CI activity")
    if a.open_prs_updated and a.merged_prs:
        parts.append("active WIP + shipping")
    if a.prs_with_close_keyword >= 2:
        parts.append("closes tracked work (issues)")
    if a.prs_touching_tests >= 2:
        parts.append("tests/e2e in shipped diffs")
    pct = merge_check_success_pct(a)
    if pct is not None and pct >= 90 and a.merge_check_runs_total >= 15:
        parts.append("high green rate on merge CI")
    if a.collab_prs_with_outside_input_after_open >= 3:
        parts.append("pulls others in after open")
    if len(a.collab_partners) >= 8:
        parts.append("broad collaboration network")
    return "; ".join(parts) if parts else "—"


def rewarded_skills(a: AuthorAgg) -> str:
    bits: list[str] = []
    if a.prs_with_close_keyword >= 1:
        bits.append(f"issue closure ({a.prs_with_close_keyword} PRs w/ close keywords)")
    if a.linked_issue_refs >= 3:
        bits.append("end-to-end ownership (many linked issues)")
    if a.prs_touching_tests >= 1:
        bits.append(f"testing in diff ({a.prs_touching_tests} PRs, {a.test_files_touched} test files)")
    pct = merge_check_success_pct(a)
    if pct is not None and a.merge_check_runs_total >= 10:
        bits.append(f"merge CI success ~{pct}% ({a.merge_check_runs_total} checks sampled)")
    if a.merged_prs >= 5 and (a.prs_with_close_keyword or a.prs_touching_tests):
        bits.append("shipped completed units (merge + closure/tests signal)")
    if a.collab_prs_with_outside_input_after_open >= 1:
        bits.append(
            f"collaboration ({a.collab_prs_with_outside_input_after_open} PRs w/ teammate input after open)"
        )
    if len(a.collab_partners) >= 5:
        bits.append(f"diverse partners on own PRs (~{len(a.collab_partners)} people)")
    return " · ".join(bits) if bits else "—"


def _median_days(cycle: list[float]) -> float | None:
    if not cycle:
        return None
    return round(float(statistics.median(cycle)), 2)


def _longest_consecutive_streak(sorted_dates: list[date]) -> int:
    if not sorted_dates:
        return 0
    best = 1
    cur = 1
    for i in range(1, len(sorted_dates)):
        if (sorted_dates[i] - sorted_dates[i - 1]).days == 1:
            cur += 1
            best = max(best, cur)
        else:
            cur = 1
    return best


def _streak_ending_on_last_merge_day(
    sorted_dates: list[date],
    active: set[date],
    window_start: date,
) -> int:
    """Consecutive calendar days with ≥1 merge, walking back from last merge day."""
    if not sorted_dates:
        return 0
    d = sorted_dates[-1]
    n = 0
    while d >= window_start and d in active:
        n += 1
        d -= timedelta(days=1)
    return n


def _max_gap_calendar_days(
    sorted_dates: list[date],
    window_start: date,
    window_end: date,
) -> int:
    """Longest idle span (no merges) inside the window, including edges."""
    if not sorted_dates:
        return (window_end - window_start).days
    g = (sorted_dates[0] - window_start).days
    for i in range(1, len(sorted_dates)):
        g = max(g, (sorted_dates[i] - sorted_dates[i - 1]).days - 1)
    g = max(g, (window_end - sorted_dates[-1]).days)
    return g


def build_merge_timelines(
    pulls_merged: list[dict[str, Any]],
    window_start: date,
    window_end: date,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """
    Per-author merge-day timelines (UTC calendar dates from merged_at).
    Returns (row_fields_by_login, export_blob_for_json, empty_row_template).
    """
    daily: dict[str, dict[date, int]] = defaultdict(lambda: defaultdict(int))
    for pull in pulls_merged:
        user = (pull.get("user") or {}).get("login")
        if is_bot_login(user):
            continue
        ma = pull.get("merged_at")
        if not ma:
            continue
        dt = parse_github_dt(ma)
        if not dt:
            continue
        day = dt.date()
        if day < window_start or day > window_end:
            continue
        daily[user][day] += 1

    window_span = max((window_end - window_start).days + 1, 1)
    row_fields: dict[str, dict[str, Any]] = {}
    authors_export: dict[str, Any] = {}

    for login, day_counts in daily.items():
        active_days = {d for d, c in day_counts.items() if c >= 1}
        sorted_d = sorted(active_days)
        productive = len(sorted_d)
        longest = _longest_consecutive_streak(sorted_d)
        latest_streak = _streak_ending_on_last_merge_day(sorted_d, active_days, window_start)
        max_gap = _max_gap_calendar_days(sorted_d, window_start, window_end)
        last_d = sorted_d[-1]
        first_d = sorted_d[0]
        days_since = (window_end - last_d).days

        week_merges: dict[tuple[int, int], int] = defaultdict(int)
        for d, c in day_counts.items():
            iso = d.isocalendar()
            week_merges[(iso[0], iso[1])] += c
        best_week = max(week_merges.values()) if week_merges else 0

        active_pct = round(100.0 * productive / window_span, 1)

        row_fields[login] = {
            "tl_productive_days": productive,
            "tl_longest_streak_days": longest,
            "tl_latest_streak_days": latest_streak,
            "tl_max_gap_days": max_gap,
            "tl_first_merge_date": first_d.isoformat(),
            "tl_last_merge_date": last_d.isoformat(),
            "tl_days_since_last_merge": days_since,
            "tl_best_week_merges": best_week,
            "tl_active_day_pct": active_pct,
        }
        authors_export[login] = {
            "daily_merges": {d.isoformat(): day_counts[d] for d in sorted(day_counts.keys())},
            "merge_dates_sorted": [d.isoformat() for d in sorted_d],
            "productive_days": productive,
            "longest_streak_days": longest,
            "latest_streak_days": latest_streak,
            "max_gap_days": max_gap,
            "first_merge_date": first_d.isoformat(),
            "last_merge_date": last_d.isoformat(),
            "days_since_last_merge": days_since,
            "best_week_merges": best_week,
            "active_day_pct": active_pct,
        }

    empty_row = {
        "tl_productive_days": 0,
        "tl_longest_streak_days": 0,
        "tl_latest_streak_days": 0,
        "tl_max_gap_days": (window_end - window_start).days,
        "tl_first_merge_date": None,
        "tl_last_merge_date": None,
        "tl_days_since_last_merge": None,
        "tl_best_week_merges": 0,
        "tl_active_day_pct": 0.0,
    }
    export = {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "window_calendar_days": window_span,
        "note": (
            "Productive day = calendar day (UTC) with ≥1 merge. "
            "Streaks are consecutive such days. latest_streak walks back from last merge day."
        ),
        "authors": authors_export,
    }
    return row_fields, export, empty_row


def compute_engineer_metrics(
    *,
    repo: str,
    window_start: date,
    window_end: date,
    window_days: int,
    pulls_merged: list[dict[str, Any]],
    pulls_open: list[dict[str, Any]],
    created_items: list[dict[str, Any]],
    opened_by_author: dict[str, int],
    files_by_pr: dict[str, list],
    reviews_by_pr: dict[str, list],
    workflow_runs: list[dict[str, Any]],
    collaboration_by_pr: dict[str, dict[str, Any]],
    merge_checks_by_pr: dict[str, dict[str, int]],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    timeline_by_login, timeline_export, timeline_empty = build_merge_timelines(
        pulls_merged, window_start, window_end
    )
    by_author: dict[str, AuthorAgg] = {}
    pr_author: dict[int, str] = {}
    files_int: dict[int, list] = {int(k): v for k, v in files_by_pr.items()}
    reviews_int: dict[int, list] = {int(k): v for k, v in reviews_by_pr.items()}

    for pull in pulls_merged:
        user = (pull.get("user") or {}).get("login")
        if is_bot_login(user):
            continue
        num = int(pull["number"])
        pr_author[num] = user
        agg = by_author.setdefault(user, AuthorAgg(login=user))
        agg.merged_prs += 1
        agg.pr_numbers.append(num)
        agg.additions += int(pull.get("additions") or 0)
        agg.deletions += int(pull.get("deletions") or 0)
        cf = int(pull.get("changed_files") or 0)
        agg.changed_files += cf
        agg.max_changed_files_single_pr = max(agg.max_changed_files_single_pr, cf)
        agg.commits_on_merged_prs += int(pull.get("commits") or 0)
        agg.issue_comments_merged += int(pull.get("comments") or 0)
        agg.review_comments_merged += int(pull.get("review_comments") or 0)

        title = (pull.get("title") or "").lower()
        if title.startswith("revert"):
            agg.revert_merges += 1
        if pull.get("draft"):
            agg.draft_merges += 1

        created = parse_github_dt(pull.get("created_at"))
        merged = parse_github_dt(pull.get("merged_at"))
        if created and merged:
            agg.cycle_days.append((merged - created).total_seconds() / 86400.0)

        close_text = (pull.get("body") or "") + "\n" + (pull.get("title") or "")
        issue_refs = parse_closing_issue_refs(close_text)
        if issue_refs:
            agg.prs_with_close_keyword += 1
            agg.linked_issue_refs += len(issue_refs)

        files = files_int.get(num)
        if files:
            tc = sum(1 for f in files if is_test_related_path(f.get("filename", "")))
            if tc:
                agg.prs_touching_tests += 1
                agg.test_files_touched += tc

        s, _ = shipping_impact_score(pull, files)
        agg.shipping_score += s

        mc = merge_checks_by_pr.get(str(num), {})
        agg.merge_check_runs_total += int(mc.get("total", 0))
        agg.merge_check_runs_success += int(mc.get("success", 0))
        agg.merge_check_runs_failure += int(mc.get("failure", 0))

        col = collaboration_by_pr.get(str(num), {})
        if col:
            ic = int(col.get("issue_comments_from_others_after_open", 0))
            lc = int(col.get("review_line_comments_from_others_after_open", 0))
            agg.collab_issue_comments_others_after_open += ic
            agg.collab_line_comments_others_after_open += lc
            agg.collab_requested_reviewers_sum += int(col.get("requested_reviewers_at_fetch", 0))
            agg.collab_assignees_non_author_sum += int(col.get("assignees_non_author", 0))
            if ic + lc > 0:
                agg.collab_prs_with_outside_input_after_open += 1
            for p in col.get("distinct_partner_logins") or []:
                if isinstance(p, str) and not is_bot_login(p) and p != user:
                    agg.collab_partners.add(p)

    for pull in pulls_open:
        user = (pull.get("user") or {}).get("login")
        if is_bot_login(user):
            continue
        agg = by_author.setdefault(user, AuthorAgg(login=user))
        agg.open_prs_updated += 1
        agg.open_additions += int(pull.get("additions") or 0)
        agg.open_deletions += int(pull.get("deletions") or 0)
        agg.open_changed_files += int(pull.get("changed_files") or 0)
        agg.open_commits += int(pull.get("commits") or 0)
        agg.open_discussion += int(pull.get("comments") or 0) + int(pull.get("review_comments") or 0)

    for login, c in opened_by_author.items():
        if is_bot_login(login):
            continue
        agg = by_author.setdefault(login, AuthorAgg(login=login))
        agg.prs_opened_in_window = c

    if reviews_int:
        for num, revs in reviews_int.items():
            author = pr_author.get(num)
            if not author:
                continue
            seen_pairs: set[tuple[str, str]] = set()
            for rv in revs:
                rev_user = (rv.get("user") or {}).get("login")
                if is_bot_login(rev_user):
                    continue
                if rev_user == author:
                    continue
                state = (rv.get("state") or "").upper()
                key = (rev_user, str(num))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                agg = by_author.setdefault(rev_user, AuthorAgg(login=rev_user))
                agg.review_events_on_others_prs += 1
                w = 0.35
                if state == "APPROVED":
                    w += 0.65
                if state and state not in ("COMMENTED", "APPROVED", "CHANGES_REQUESTED"):
                    w = 0.25
                agg.review_score += w

    for run in workflow_runs:
        actor = (run.get("triggering_actor") or run.get("actor") or {}) or {}
        login = actor.get("login")
        if is_bot_login(login):
            continue
        agg = by_author.setdefault(login, AuthorAgg(login=login))
        agg.workflow_runs += 1

    rows: list[dict[str, Any]] = []
    for a in by_author.values():
        a.median_cycle_days = _median_days(a.cycle_days)
        med = a.median_cycle_days
        opened = a.prs_opened_in_window
        merged_n = a.merged_prs
        merge_rate = round(100.0 * merged_n / opened, 1) if opened else None
        avg_commits = round(a.commits_on_merged_prs / merged_n, 2) if merged_n else None
        discussion = a.issue_comments_merged + a.review_comments_merged
        combined = a.shipping_score + a.review_score
        dscore = delivery_score(a)
        cscore = collaboration_score(a)
        mcpct = merge_check_success_pct(a)
        full_stack = round(combined + dscore + cscore, 3)
        partners_n = len(a.collab_partners)
        tl = timeline_by_login.get(a.login, timeline_empty)
        row = {
                "login": a.login,
                "combined_score": round(combined, 3),
                "delivery_score": dscore,
                "collaboration_score": cscore,
                "full_stack_score": full_stack,
                "shipping_score": round(a.shipping_score, 3),
                "review_score": round(a.review_score, 3),
                "merged_prs": merged_n,
                "prs_opened_in_window": opened,
                "merge_rate_pct": merge_rate,
                "open_prs_updated": a.open_prs_updated,
                "median_cycle_days": med,
                "commits_on_merged_prs": a.commits_on_merged_prs,
                "avg_commits_per_merged_pr": avg_commits,
                "discussion_on_merged_prs": discussion,
                "workflow_runs": a.workflow_runs,
                "additions": a.additions,
                "deletions": a.deletions,
                "changed_files": a.changed_files,
                "review_events_on_others_prs": a.review_events_on_others_prs,
                "prs_with_close_keyword": a.prs_with_close_keyword,
                "linked_issue_refs": a.linked_issue_refs,
                "prs_touching_tests": a.prs_touching_tests,
                "test_files_touched": a.test_files_touched,
                "merge_check_runs_total": a.merge_check_runs_total,
                "merge_check_success_pct": mcpct,
                "merge_check_failures": a.merge_check_runs_failure,
                "collab_issue_comments_others_after_open": a.collab_issue_comments_others_after_open,
                "collab_line_comments_others_after_open": a.collab_line_comments_others_after_open,
                "collab_prs_with_teammate_after_open": a.collab_prs_with_outside_input_after_open,
                "collab_unique_partners_on_own_prs": partners_n,
                "collab_requested_reviewers_sum": a.collab_requested_reviewers_sum,
                "collab_assignees_non_author_sum": a.collab_assignees_non_author_sum,
                "rewarded_skills": rewarded_skills(a),
                "revert_merges": a.revert_merges,
                "draft_merges": a.draft_merges,
                "max_changed_files_single_pr": a.max_changed_files_single_pr,
                "open_wip_additions": a.open_additions,
                "open_wip_deletions": a.open_deletions,
                "open_wip_churn": a.open_additions + a.open_deletions,
                "open_wip_files": a.open_changed_files,
                "open_wip_commits": a.open_commits,
                "open_wip_discussion": a.open_discussion,
                "note": engineer_note(a),
        }
        row.update(tl)
        rows.append(row)

    rows.sort(key=lambda r: (r.get("full_stack_score") or 0), reverse=True)

    totals = {
        "distinct_engineers": len(rows),
        "merged_prs": len(pulls_merged),
        "open_prs_touched": len(pulls_open),
        "prs_opened": len(created_items),
        "workflow_runs_sampled": len(workflow_runs),
    }
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "window_days": window_days,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "totals": totals,
        "definitions": {
            "full_stack_score": "combined_score + delivery_score + collaboration_score.",
            "collaboration_score": "Teammate issue + line comments after PR open, partner breadth, review requests/assignees (see raw collaboration_by_pr).",
            "merge_rate_pct": "merged_prs / prs_opened_in_window (approximate).",
            "timeline": "See docs/METRICS.md — merge-day streaks & gaps (tl_* columns, timeline_by_author.json).",
        },
        "top_by_full_stack_score": rows[:20],
    }

    # Build a lightweight "contributions to a PR / issue" dataset for the UI.
    # To keep the static HTML small, we only include the top PRs by PR value (shipping + reviews + collaboration)
    # and then aggregate their linked issues.
    def _comment_part_score(issue_count: int, line_count: int) -> float:
        return round(math.log1p(issue_count) * 2.2 + math.log1p(line_count) * 1.8, 6)

    pr_contribs_all: list[dict[str, Any]] = []
    for pull in pulls_merged:
        author = (pull.get("user") or {}).get("login")
        if is_bot_login(author):
            continue
        num = int(pull.get("number"))
        title = pull.get("title") or ""
        files = files_int.get(num)
        pr_shipping, _ = shipping_impact_score(pull, files)

        close_text = (pull.get("body") or "") + "\n" + (pull.get("title") or "")
        issue_refs = sorted(parse_closing_issue_refs(close_text))

        # participants[login] = tracked per-user contribution components
        participants: dict[str, dict[str, Any]] = {}

        def ensure(login: str) -> dict[str, Any]:
            if login not in participants:
                participants[login] = {
                    "login": login,
                    "shipping_part": 0.0,
                    "review_part": 0.0,
                    "comment_issue_count": 0,
                    "comment_line_count": 0,
                    "comment_part": 0.0,
                    "requested_flag": False,
                    "assigned_flag": False,
                    "requested_part": 0.0,
                    "assigned_part": 0.0,
                    "total_contrib": 0.0,
                }
            return participants[login]

        # Author gets shipping credit for the PR.
        a = ensure(author)
        a["shipping_part"] = float(pr_shipping)

        # Reviews (distinct reviewer-PR pair)
        if num in reviews_int:
            seen_pairs: set[str] = set()
            for rv in reviews_int.get(num, []) or []:
                rv_user = (rv.get("user") or {}).get("login")
                if is_bot_login(rv_user) or not rv_user:
                    continue
                if rv_user in seen_pairs:
                    continue
                if rv_user == author:
                    # Author review of their own PR usually doesn't matter for "contribution to ship".
                    continue
                seen_pairs.add(rv_user)
                state = (rv.get("state") or "").upper()
                w = 0.35
                if state == "APPROVED":
                    w += 0.65
                if state and state not in ("COMMENTED", "APPROVED", "CHANGES_REQUESTED"):
                    w = 0.25
                rr = ensure(rv_user)
                rr["review_part"] += w

        # Collaboration comments after PR open (if per-login maps exist, use them; otherwise fall back)
        col = collaboration_by_pr.get(str(num), {}) or {}
        issue_by_login = col.get("issue_comments_by_login_after_open") or None
        line_by_login = col.get("review_line_comments_by_login_after_open") or None

        distinct_partners = col.get("distinct_partner_logins") or []
        partners = [p for p in distinct_partners if p and not is_bot_login(p)]

        if issue_by_login and isinstance(issue_by_login, dict):
            line_by_login = line_by_login if isinstance(line_by_login, dict) else {}
            keys = set(issue_by_login.keys()) | set(line_by_login.keys())
            for login in keys:
                if is_bot_login(login):
                    continue
                ic = int(issue_by_login.get(login) or 0)
                lc = int(line_by_login.get(login) or 0)
                if ic == 0 and lc == 0:
                    continue
                cp = ensure(login)
                cp["comment_issue_count"] += ic
                cp["comment_line_count"] += lc
                cp["comment_part"] += _comment_part_score(ic, lc)
        else:
            # Approximate distribution if per-login counts are not available.
            ic_total = int(col.get("issue_comments_from_others_after_open") or 0)
            lc_total = int(col.get("review_line_comments_from_others_after_open") or 0)
            k = max(1, len(partners))
            for login in partners:
                ic = int(round(ic_total / k))
                lc = int(round(lc_total / k))
                if ic == 0 and lc == 0:
                    continue
                cp = ensure(login)
                cp["comment_issue_count"] += ic
                cp["comment_line_count"] += lc
                cp["comment_part"] += _comment_part_score(ic, lc)

        # Requested reviewers / assignees snapshot (flags + tiny part)
        requested_reviewers = pull.get("requested_reviewers") or []
        if isinstance(requested_reviewers, list):
            for rr in requested_reviewers:
                rl = rr.get("login") if isinstance(rr, dict) else None
                if rl and rl != author and not is_bot_login(rl):
                    p = ensure(rl)
                    p["requested_flag"] = True
                    p["requested_part"] += 0.2
        assignees = pull.get("assignees") or []
        if isinstance(assignees, list):
            for a2 in assignees:
                al = a2.get("login") if isinstance(a2, dict) else None
                if al and al != author and not is_bot_login(al):
                    p = ensure(al)
                    p["assigned_flag"] = True
                    p["assigned_part"] += 0.1

        # finalize totals
        for login, p in participants.items():
            p["total_contrib"] = round(
                float(p.get("shipping_part") or 0.0)
                + float(p.get("review_part") or 0.0)
                + float(p.get("comment_part") or 0.0)
                + float(p.get("requested_part") or 0.0)
                + float(p.get("assigned_part") or 0.0),
                6,
            )

        # Build ordered participant list
        participant_rows = sorted(
            participants.values(), key=lambda x: x.get("total_contrib") or 0.0, reverse=True
        )
        pr_value = sum(float(p.get("total_contrib") or 0.0) for p in participant_rows)

        pr_contribs_all.append(
            {
                "number": num,
                "title": title,
                "issue_refs": issue_refs,
                "pr_value": round(pr_value, 6),
                "participants": participant_rows,
            }
        )

    pr_contribs_all.sort(key=lambda x: x.get("pr_value") or 0.0, reverse=True)
    TOP_PRs_FOR_UI = 60
    selected_prs = pr_contribs_all[:TOP_PRs_FOR_UI]
    selected_pr_numbers = {p["number"] for p in selected_prs}

    issues_map: dict[int, dict[str, Any]] = {}
    for pr in selected_prs:
        for issue in pr.get("issue_refs") or []:
            issue_entry = issues_map.setdefault(
                int(issue),
                {
                    "issue_number": int(issue),
                    "prs_count": 0,
                    "participants": {},
                },
            )
            issue_entry["prs_count"] += 1
            for part in pr.get("participants") or []:
                login = part.get("login")
                if not login:
                    continue
                pe = issue_entry["participants"].setdefault(
                    login,
                    {
                        "login": login,
                        "shipping_part": 0.0,
                        "review_part": 0.0,
                        "comment_issue_count": 0,
                        "comment_line_count": 0,
                        "comment_part": 0.0,
                        "requested_part": 0.0,
                        "assigned_part": 0.0,
                        "total_contrib": 0.0,
                    },
                )
                for k in [
                    "shipping_part",
                    "review_part",
                    "comment_issue_count",
                    "comment_line_count",
                    "comment_part",
                    "requested_part",
                    "assigned_part",
                ]:
                    pe[k] = float(pe.get(k) or 0.0) + float(part.get(k) or 0.0)

    # finalize issue participant lists
    issues_out: dict[str, Any] = {}
    for issue_num, entry in issues_map.items():
        parts_dict = entry.get("participants") or {}
        participant_rows = []
        for login, pe in parts_dict.items():
            pe["total_contrib"] = round(
                float(pe.get("shipping_part") or 0.0)
                + float(pe.get("review_part") or 0.0)
                + float(pe.get("comment_part") or 0.0)
                + float(pe.get("requested_part") or 0.0)
                + float(pe.get("assigned_part") or 0.0),
                6,
            )
            participant_rows.append(pe)
        participant_rows.sort(key=lambda x: x.get("total_contrib") or 0.0, reverse=True)
        issues_out[str(issue_num)] = {
            "issue_number": issue_num,
            "prs_count": entry.get("prs_count") or 0,
            "participants": participant_rows,
        }

    prs_out: dict[str, Any] = {str(pr["number"]): pr for pr in selected_prs}
    contrib = {"prs": prs_out, "issues": issues_out}

    bundle = {
        "meta": {
            "generated_at": summary["generated_at"],
            "repo": repo,
            "window_days": window_days,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "totals": totals,
        },
        "engineers": rows,
        "contrib": contrib,
    }
    return rows, summary, bundle, timeline_export


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))
