# Metrics reference

This document describes every field written to **`metrics_by_author.json`**, **`metrics_by_author.csv`**, and **`engineers_for_notion.csv`**, plus how composite scores are built.

Unless noted, counts are **per GitHub login** over the **manifest window** (`window_start` … `window_end` in `data/raw/manifest.json`).

---

## Composite scores (sorting & rollups)

### `full_stack_score` (default leaderboard sort)

`combined_score + delivery_score + collaboration_score`

Single number for sorting; interpret using the three components below.

### `combined_score`

`shipping_score + review_score`

- **`shipping_score`** — From **merged PRs** you authored: sublinear function of diff size, optional **path weights** (product vs docs vs lockfiles), and **label** hints (`bug`/`fix`, `security`, `breaking`, `priority` / `p0` / `p1`). See [Shipping score (detail)](#shipping-score-detail) below.
- **`review_score`** — From **`--fetch-reviews`**: when you submit a **review** on someone else’s merged PR (not your own), each distinct reviewer–PR pair adds weight: base **0.35**, **+0.65** if state is **APPROVED**, **0.25** for other nonstandard states. De-duplicated per (reviewer, PR).

### `delivery_score`

Rewards **issue closure language**, **tests in diff**, and **green merge CI** (if fetched). Formula (each term capped as shown in code):

| Input | Role |
|--------|------|
| `prs_with_close_keyword` | + up to 25 via `min(count × 2.0, 25)` |
| `linked_issue_refs` | + up to 20 via `min(count × 0.35, 20)` |
| `prs_touching_tests` | + up to 18 via `min(count × 1.5, 18)` |
| `test_files_touched` | + up to 8 via `min(log1p(test_files), 8)` |
| `merge_check_success_pct` | + `(pct/100) × 12` when merge checks exist |

Requires **`--fetch-files`** for test-related fields to be non-zero. Requires **`--fetch-merge-checks`** for merge CI fields to be populated.

### `collaboration_score`

Rewards **teammate engagement on your merged PRs after the PR was opened** (plus small bumps for review requests / assignees on the PR snapshot).

| Input | Role (capped in code) |
|--------|------------------------|
| `collab_issue_comments_others_after_open` | `log1p(count) × 2.2`, max 14 |
| `collab_line_comments_others_after_open` | `log1p(count) × 1.8`, max 16 |
| `collab_prs_with_teammate_after_open` | `count × 1.0`, max 14 |
| `collab_unique_partners_on_own_prs` | `log1p(partners) × 2.8`, max 14 |
| `collab_requested_reviewers_sum` | `sum × 0.25`, max 6 |
| `collab_assignees_non_author_sum` | `sum × 0.2`, max 5 |

Requires **`--fetch-collaboration`** for comment-based fields to be non-zero.

---

## Merge-day timeline (`tl_*` columns, scheduling)

Derived only from **merge dates** (`merged_at`, **UTC calendar date**). A **productive day** is any day with **≥1 merge** by that author inside the manifest window.

These do **not** affect `full_stack_score`; they are for **capacity / rhythm** (streaks, gaps, recency).

| Column | Description |
|--------|-------------|
| `tl_productive_days` | Count of distinct calendar days with at least one merge. |
| `tl_longest_streak_days` | Longest run of **consecutive** calendar days each with ≥1 merge. |
| `tl_latest_streak_days` | Length of the consecutive productive run **ending on the last merge day** (walk backward until a gap). Useful for “how hot were they right before they went quiet?” |
| `tl_max_gap_days` | Longest stretch of **calendar days without a merge** inside the window (includes gaps from `window_start` to first merge and from last merge to `window_end`). |
| `tl_first_merge_date` | Earliest merge date in the window (`YYYY-MM-DD`), or `null` if no merges. |
| `tl_last_merge_date` | Latest merge date in the window, or `null`. |
| `tl_days_since_last_merge` | `(window_end − tl_last_merge_date)` in days; `null` if no merges in window. |
| `tl_best_week_merges` | Maximum total merges in any single **ISO week** (Mon–Sun) that intersects the window. |
| `tl_active_day_pct` | `100 × tl_productive_days / window_calendar_days` (inclusive span from `window_start` to `window_end`). |

**Detail export:** `data/metrics/timeline_by_author.json` includes per author:

- `daily_merges`: map of `YYYY-MM-DD` → merge count  
- `merge_dates_sorted`: sorted list of days with activity  
- The same summary fields as above (under slightly different names for readability)

**Caveats:** Uses **UTC** dates only. Does not reflect reviews-only days, meetings, or work that never merged. Engineers with **zero merges** in the window get zeros / `null` dates and `tl_max_gap_days` equal to the full window span.

---

## Shipping score (detail)

For each **merged** PR, with optional file list from **`--fetch-files`**:

- `churn = additions + deletions`
- `size_component = sqrt(log1p(churn)) × min(1, log1p(changed_files) / 4)`
- **`path_multiplier`**: mean of per-file weights (lockfiles/snapshots/images low; `ee/`, `posthog/`, `frontend/`, `products/` higher; `.py`/`.tsx`/`.ts`/`.rs` slight bump).
- **`label_multiplier`**: from label names (bug/fix, security, breaking, priority).
- Per-PR contribution: `size_component × path_multiplier × label_multiplier`, summed per author.

---

## Identity & volume

| Column | Description |
|--------|-------------|
| `login` | GitHub username (bots filtered from authorship). |
| `performance_band` | Scheduling band derived from `full_stack_score` within the current window using tertiles: `High` (top third), `Medium` (middle third), `Low` (bottom third). |
| `merged_prs` | Merged PRs in the window whose **author** is this user. |
| `prs_opened_in_window` | PRs **created** in the window (`search_created.json`), by author. |
| `merge_rate_pct` | `merged_prs / prs_opened_in_window` when the denominator is positive; approximate funnel, not causal. |
| `open_prs_updated` | Open PRs whose **updated** time fell in the window (search + fetch). |
| `additions` | Sum of GitHub **`additions`** on your merged PRs. |
| `deletions` | Sum of **`deletions`** on your merged PRs. |
| `changed_files` | Sum of **`changed_files`** on your merged PRs. |
| `max_changed_files_single_pr` | Largest single merged PR by file count. |

---

## Cycle time & iteration

| Column | Description |
|--------|-------------|
| `median_cycle_days` | Median of `(merged_at − created_at)` in days over your merged PRs. |
| `commits_on_merged_prs` | Sum of PR **`commits`** count (branch commits before merge). |
| `avg_commits_per_merged_pr` | `commits_on_merged_prs / merged_prs`. |
| `discussion_on_merged_prs` | Sum of PR **`comments`** + **`review_comments`** (GitHub totals, not split by author). |

---

## Reviews & CI (Actions)

| Column | Description |
|--------|-------------|
| `review_events_on_others_prs` | Distinct review submissions by you on **others’** merged PRs (with **`--fetch-reviews`**). |
| `shipping_score` | See [Shipping score (detail)](#shipping-score-detail). |
| `review_score` | See [combined_score](#combined_score). |
| `workflow_runs` | Count of **Actions workflow runs** attributed to **`triggering_actor`** (**`--fetch-actions`**; newest-first, capped by `--actions-max-pages`). |

---

## Delivery / “finished work” proxies

| Column | Description |
|--------|-------------|
| `prs_with_close_keyword` | Merged PRs whose **title + body** contain GitHub closing phrases (`close`/`fix`/`resolve` + `#NNN`). |
| `linked_issue_refs` | Sum of distinct `#NNN` counts per such PR (same issue may repeat across PRs). |
| `prs_touching_tests` | Merged PRs with ≥1 file path matching **test/e2e heuristics** (`--fetch-files`). |
| `test_files_touched` | Count of such files across merged PRs. |
| `merge_check_runs_total` | Completed check runs on **`merge_commit_sha`** summed across your merged PRs (`--fetch-merge-checks`). |
| `merge_check_success_pct` | `100 × success / total` for **completed** runs with `conclusion == success`. |
| `merge_check_failures` | Completed runs with `conclusion == failure`. |
| `revert_merges` | Merged PRs whose **title** (lowercased) starts with `revert`. |
| `draft_merges` | Merged PRs with **`draft`** true at fetch time. |

---

## Collaboration (others after PR open)

Requires **`--fetch-collaboration`**. For each **merged** PR:

- **Issue comments** from `/issues/{n}/comments`
- **Line comments** from `/pulls/{n}/comments`

Only counts **non-author**, **non-bot** comments with **`created_at` strictly after** the PR’s **`created_at`**.

| Column | Description |
|--------|-------------|
| `collab_issue_comments_others_after_open` | Teammate issue-thread comments after open. |
| `collab_line_comments_others_after_open` | Teammate review line comments after open. |
| `collab_prs_with_teammate_after_open` | Merged PRs with at least one such comment. |
| `collab_unique_partners_on_own_prs` | Unique teammate logins across those comments. |
| `collab_requested_reviewers_sum` | Sum of **`requested_reviewers`** length on each PR payload at fetch time (often **0** after merge). |
| `collab_assignees_non_author_sum` | Sum of assignees on the PR who are not the author (snapshot). |

---

## Open WIP (open PRs in window)

Sums over **open** PRs you own that were fetched (updated in window):

| Column | Description |
|--------|-------------|
| `open_wip_additions` / `open_wip_deletions` | Sum of additions/deletions. |
| `open_wip_churn` | `open_wip_additions + open_wip_deletions`. |
| `open_wip_files` | Sum of `changed_files`. |
| `open_wip_commits` | Sum of `commits`. |
| `open_wip_discussion` | Sum of `comments + review_comments`. |

---

## Narrative fields (not numeric scores)

| Column | Description |
|--------|-------------|
| `rewarded_skills` | Short bullet-style string for **recognition** (issue closure, tests, merge CI, collaboration, partners). Heuristic text only. |
| `note` | Auto summary from thresholds (cycle time, iteration, reviews, WIP, delivery, collaboration). |

---

## Raw JSON artifacts (`data/raw/`)

| File | Content |
|------|---------|
| `manifest.json` | `repo`, window dates, `flags` used at fetch time. |
| `search_merged.json` | Search hits for merged PRs in window. |
| `search_open_updated.json` | Open PRs updated in window. |
| `search_created.json` | PRs created in window. |
| `pulls_merged.json` | Full REST pull objects (merged). |
| `pulls_open.json` | Full pull objects (open sample). |
| `pull_files.json` | Map PR number → file list (optional). |
| `pull_reviews.json` | Map PR number → reviews (optional). |
| `collaboration_by_pr.json` | Per-PR collaboration summary (optional). |
| `merge_checks_by_pr.json` | Per-PR `{total, success, failure}` for completed checks (optional). |
| `workflow_runs_sample.json` | Actions runs sample (optional). |

---

## `summary.json` (in `data/metrics/`)

- **`generated_at`**, **`repo`**, **`window_*`**
- **`totals`**: `distinct_engineers`, `merged_prs`, `open_prs_touched`, `prs_opened`, `workflow_runs_sampled`
- **`definitions`**: short strings for key composites
- **`top_by_full_stack_score`**: top 20 rows by `full_stack_score`

## `timeline_by_author.json` (in `data/metrics/`)

Structured timeline for charts or scheduling tools: `window_*`, `window_calendar_days`, `note`, and **`authors`** → per-login objects with `daily_merges`, streak/gap summaries, etc. Produced on every **`compute_metrics.py`** run (no extra flags).

The same timeline JSON is **embedded** in the static dashboard when you run **`compute_metrics.py --build-report`**: open **`docs/index.html`** or **`data/metrics/index.html`**, use the **Timelines** tab, pick an engineer, and view the daily bar chart (UTC days, height ∝ merge count). Clicking a row on the Overview table syncs the timeline if that author has merge data.

---

## Disclaimer

These metrics **do not** measure intrinsic engineer quality. They approximate **visibility in GitHub** (shipping, review, CI, collaboration). Use alongside context, team norms, and non-GitHub work.
