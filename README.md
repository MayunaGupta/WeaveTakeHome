# GitHub engineer metrics (PostHog-style)

Pulls **merged PRs, reviews, optional files, CI checks, and collaboration signals** from any GitHub repo via the API (no clone), then builds **per-engineer CSV/JSON** and a **static HTML dashboard**.

Designed for **trend and discussion**, not as a single source of truth for performance reviews.

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`
- A GitHub token (strongly recommended): `export GH_TOKEN=ghp_...` or `GITHUB_TOKEN`

## Quick start

```bash
cd posthog-github-metrics
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GH_TOKEN=ghp_your_token

# 1) Download raw JSON → data/raw/
python fetch_data.py --data ./data --repo PostHog/posthog --days 90 \
  --fetch-reviews --fetch-files --fetch-merge-checks \
  --fetch-collaboration --fetch-actions --actions-max-pages 30

# 2) Build tables + dashboard → data/metrics/
python compute_metrics.py --data ./data --build-report
```

Open `data/metrics/index.html` in a browser, or publish that folder (see [Hosting](#hosting)).

### One-shot (legacy)

```bash
python fetch_posthog_metrics.py --data ./data --days 90 --fetch-reviews --build-report
```

Runs **fetch** then **compute**. `--out DIR` is accepted as an alias for `--data DIR`.

- `--fetch-only` — only `data/raw/`
- `--compute-only` — only `data/metrics/` (expects existing `data/raw/manifest.json`)

## Project layout

| Path | Role |
|------|------|
| `fetch_data.py` | GitHub API → `data/raw/*.json` + `manifest.json` |
| `compute_metrics.py` | Reads `raw/`, writes `data/metrics/*` |
| `gh_common.py` | Shared HTTP helpers, scoring, aggregation |
| `report/template.html` | Dashboard template (embedded JSON payload) |
| `fetch_posthog_metrics.py` | Wrapper: fetch + compute |

### Output directories

- **`data/raw/`** — large JSON (searches, pulls, optional files/reviews/collaboration/workflows). Safe to `.gitignore` for size.
- **`data/metrics/`** — `metrics_by_author.json`, CSVs, `summary.json`, **`timeline_by_author.json`** (per-day merge counts + streaks for scheduling), `index.html`.

## Fetch flags

| Flag | Effect |
|------|--------|
| `--data DIR` | Root directory (default `./data`) |
| `--repo owner/name` | Target repository |
| `--days N` | Calendar lookback from today |
| `--slice-days N` | Search slice width (use **3** if you hit the **1000 results per query** cap) |
| `--fetch-files` | Per-PR file lists (path weighting, **test detection**) |
| `--fetch-reviews` | Per-PR reviews (others’ PRs → **review_score**) |
| `--fetch-collaboration` | Issue comments + line comments (**teammates after PR open**) |
| `--fetch-merge-checks` | Check runs on **`merge_commit_sha`** (merge CI %) |
| `--fetch-actions` | Workflow runs sample (attributed to triggering actor) |
| `--skip-open` / `--skip-created` | Skip open/created searches (faster, fewer metrics) |
| `--resume` | Reuse existing `data/raw/search_*.json` and per-PR JSON; only fetch **missing** merged/open PRs, merge checks, etc. (same `--repo` / `--days` as before) |
| `--checkpoint-every N` | While listing merged PRs, rewrite `pulls_merged.json` and sidecars every **N** PRs (default **25**) so a crash leaves recoverable files |

**Resuming:** If a run stops (rate limit, network, Ctrl+C), run the **same** `fetch_data.py` command again with **`--resume`** added. Search results are written **after each time slice**; merged-PR progress is checkpointed every `--checkpoint-every` PRs even without `--resume`, so you can often resume with `--resume` as long as `data/raw/` still has partial JSON.

To **start over** from scratch, remove or rename `data/raw/` (or delete the specific `search_*.json` / `pulls_merged.json` files you want refreshed) and run **without** `--resume`.

Full metric definitions: **[docs/METRICS.md](docs/METRICS.md)**.

## Notion / spreadsheets

Import `data/metrics/engineers_for_notion.csv` into Notion or Google Sheets (same columns as `metrics_by_author.csv`).

## Dashboard usage (static HTML)

After `python compute_metrics.py --data ./data --build-report`, open `data/metrics/index.html` (or the GitHub Pages site) and use:

- **Overview**: sortable leaderboard of engineers with drill-down on each engineer.
- **Timelines**: merge-day “streak & gap” view.
  - Multi-select enabled: pick multiple engineers to see their merges on the same UTC day calendar.
  - Tip: you can also Ctrl/Cmd-click engineers in the Overview table to toggle them in the Timelines selection.
- **Scoring logic**: plain-language explanation of the scoring model and caps/weights.

The table headers have hover tooltips describing what each metric means (including the scheduling-focused timeline columns).

### `performance_band`

The exported tables include `performance_band` (High/Medium/Low) derived from tertiles on `full_stack_score` within the computed window. This is for scheduling prioritization, not an intrinsic “quality” score.

## Build time context

- Focused assignment work: about `1:08:38`
- Full `fetch_data` run (GitHub data pull through completion of merged-PR fetching, including optional files/reviews/collaboration/checks): on the order of **~14 hours** wall clock (rate limits, checkpoints, large repo)

## Hosting (GitHub Pages)

Running **`compute_metrics.py --build-report`** writes **`data/metrics/index.html`** and **`docs/index.html`** (same file: Overview + Timelines tabs, timeline chart embedded).

1. Commit the repo (include `docs/index.html` if you use the `/docs` Pages source).
2. **Settings → Pages →** Branch `main`, folder **`/docs`**.
3. Site URL: `https://<user>.github.io/<repo>/`

Do **not** commit tokens. Consider `.gitignore` for `data/raw/` if it is huge.

## Limitations

- GitHub **search** returns at most **1000 items per query**; use `--slice-days` to narrow slices.
- **Unauthenticated** API limits are low; use a token.
- Metrics are **heuristics** (paths, labels, timestamps). Interpret with context.

## License

Use and modify as needed for internal analytics.
