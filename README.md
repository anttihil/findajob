# CareerRadar

Scrapes LinkedIn and Indeed across a catalog of role families and locations, matches postings
against your resume corpus, and answers three questions in a dashboard:

- **Which roles actually have hiring supply?** — new postings per day per role family.
- **Which of your skills are genuinely in demand?**
- **Which skills should you learn next?** — ranked by how often a missing skill blocks a
  posting you *otherwise* match well.

## Setup

```bash
git clone https://github.com/your-username/careerradar.git
cd careerradar
uv sync
scripts/sync_corpus.sh ../resume        # the resume corpus -- see below
uv run python -m scripts.seed_cells     # build the scrape matrix from data/roles.yaml
```

`python-jobspy` is pinned to a **git commit**, not the PyPI release: the published wheel
constrains numpy to 1.26.3, which has no cp313 wheel and cannot build on Python 3.13.

No credentials are needed for scraping. `ANTHROPIC_API_KEY` is only used by the optional
reranker below. `SCRAPER_PROXIES` in `.env` is optional but load-bearing: without it the
budgets fall back to conservative single-IP limits and LinkedIn drops to titles-only.

## The resume corpus

`profile.py` grades your skills from eight files this repo deliberately does **not** track:

```
resumes/*.md        six tailored variants
current_resume.md
achievements.md
```

They are personal career documents and their source of truth is the separate `resume` repo,
so a fresh clone has no corpus at all. A missing corpus does not fail — it falls back to the
levels declared in `data/skills.yaml`, which silently shifts every match score. That is why
`build_profile()` logs a warning whenever it reads fewer than eight sources. Heed it:

```bash
scripts/sync_corpus.sh ../resume        # path to the resume repo; ../resume is the default
```

## Running

```bash
# 1. Verify the scrapers without writing anything. Run this first.
uv run python sync.py --dry-run --limit 3

# 2. Deep first pass. Indeed only, widest window, ~14 cells per invocation.
#    Run it 4-5 times across a few days to cover the matrix.
uv run python sync.py --backfill --limit 14

# 3. Steady state: both sources, incremental. Twice a day is the design point.
uv run python sync.py

# 4. Dashboard
uv run run.py            # http://127.0.0.1:8000
```

**Run syncs from the CLI, not the dashboard's Sync button, while developing.** `run.py`
starts uvicorn with `reload=True` and the sync runs in-process, so saving any file mid-scrape
kills it.

| Flag | Effect |
|---|---|
| `--dry-run` | scrape a small sample, print normalized rows and scores, write nothing |
| `--backfill` | Indeed only, max results, widest window |
| `--source indeed` | restrict to one source (repeatable) |
| `--rescore-only` | re-derive scores and skills for stored postings under the current taxonomy; no scraping |
| `--limit N` | cap cells per source |

Unattended operation is bundled — a systemd timer runs the sync twice a day. See
**Deployment** below.

## How the search space is defined

`data/roles.yaml` holds ~28 role families × 7 locations. The cross-product is pruned to
**252 cells** (126 per source) because LinkedIn rate-limits around the 10th page on a single
IP. The scheduler *rotates* through those cells rather than sweeping them, so a full matrix
cycle takes roughly 5 days — which is why `analytics.min_window_days` is 30.

`data/skills.yaml` is the shared vocabulary: 172 canonical skills with aliases. Both the
resume corpus and scraped descriptions map onto these keys, which is what makes a match mean
the same thing on both sides.

Editing either file changes what a run measures, so `taxonomy_hash` and `plan_hash` are
recorded on every run and every stats row, and trend queries refuse to compare across a
change. `--rescore-only` re-derives history under the current taxonomy when you'd rather have
consistency than reproducibility.

## Reading the numbers honestly

Worth understanding before trusting a chart.

**Supply is a rate, not a total.** Boards never report how many postings exist and every
result set is truncated, so absolute supply is not estimable and is never claimed. The
headline metric is postings/day over the *interval union* of observed windows.

**An amber bar with an arrow cap and a `≥` label is a lower bound.** The board cut off the
result set, so there were more. It is deliberately not drawn as an ordinary bar.

**"none observed" ≠ zero demand.** It means that cell was scraped and returned nothing
on-topic. A dot in the heatmap means the cell has not been scraped at all.

**Each location is charted separately on purpose.** Flow is comparable only within one
location and source, so there is no pooled cross-location ranking anywhere in the product.

**Skill demand is post-stratified** against the declared `analytics.reference_mix`, so it
stays comparable as the rotation changes. Confidence intervals use Kish `n_eff`, not raw `n`,
because reweighting an unbalanced sample costs precision. Until the rotation has covered
enough of your target mix, the tab falls back to **unweighted** figures and says so — those
reflect what was scraped, not the market.

**What is not corrected:** within-stratum selection bias. If a cell truncated and the board
ranks larger employers first, demand inside that stratum skews toward big-company stacks.
`saturated_share`, `n_companies`, and `max_company_share` are reported so you can see it.

**Suppression is visible, never silent.** A figure that lacks the sample size, company
spread, or coverage to state honestly is listed with its reason rather than dropped.

## Optional Claude reranker

Off by default. The deterministic scorer runs on everything and is what the dashboard shows;
this reads full descriptions for the top N and adds judgements keyword matching cannot make —
whether a requirement is a hard blocker, whether the seniority band fits.

```yaml
matching:
  llm:
    enabled: true
    model: claude-opus-5
    top_n: 25
    use_batch_api: true      # 50% cost
    max_usd_per_run: 1.00    # pre-flight token count ABORTS rather than overspending
```

Needs `ANTHROPIC_API_KEY` and `uv add anthropic`. Without either, the stage is skipped with a
warning — ingestion never depends on it. Job descriptions are treated as untrusted input:
delimited, declared as data in the system prompt, and constrained to a structured output, so
the worst case is a wrong score rather than a hijacked agent.

## Deployment

Runs on a Tailscale-reachable home server as two systemd units: a long-lived web service and
a `oneshot` sync driven by a timer (07:00 and 19:00, `Persistent=true` so a run missed while
the machine was off is caught up rather than dropped).

```bash
git clone https://github.com/your-username/careerradar.git ~/projects/careerradar
cd ~/projects/careerradar
uv sync
scripts/sync_corpus.sh /path/to/resume         # corpus
cp /path/to/.env .                             # SCRAPER_PROXIES
sqlite3 /path/to/old/jobs.db ".backup 'jobs.db'"   # WAL mode: never plain-copy a live DB

sudo cp deploy/careerradar-*.service deploy/careerradar-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now careerradar-web.service careerradar-sync.timer
```

The units hardcode the app root and `User=`; adjust both if the server layout differs. The
sync unit deliberately has no `Restart=` — the circuit breaker has already decided how long to
back off, and restarting the unit would discard that decision and re-approach a board that
just rate-limited us.

The web service binds `127.0.0.1:8010` and stays there. Tailscale fronts it:

```bash
sudo tailscale serve --bg --https 9443 8010    # https://<host>.ts.net:9443
```

A dedicated HTTPS port rather than the tailnet root, because on the current server the root
is already proxied to a different app. Serving this one under a subpath instead would not
work without changes: the frontend asks for `/static/...` and `/api/...` at the origin root,
so those requests would land on whatever owns `/`.

That config lives in `tailscaled` state and survives reboot, so there is nothing else to
enable. Access is gated twice: the tailnet boundary, and a middleware in `backend/main.py`
that rejects proxied requests whose `Tailscale-User-Login` is not the owner. The API has no
other authentication and `POST /api/config` rewrites `config.yaml` on disk, so do not expose
this with `tailscale funnel`.

Two operational notes. `config.yaml` is rewritten at runtime by the dashboard, so the
server's working tree goes dirty on its own — reconcile it before pulling. And the frontend
loads its font and icons from CDNs, so a viewing device with no route to the public internet
gets a working dashboard with fallback typography.

## Layout

```
data/roles.yaml          role families, seniority, locations, exclusions
data/skills.yaml         canonical skills + aliases + informational blockers
backend/
  taxonomy.py            skill matching (word / literal / strict-with-context modes)
  roles.py               title -> role family + seniority
  profile.py             graded skill profile from 8 resume/achievement sources
  scheduler.py           which cells to scrape, under budget
  scraper_guard.py       per-source circuit breaker (429 is NOT retried)
  normalizer.py          board rows -> schema; salary, location, dedup hashing
  scoring.py             deterministic fit score, 4 explainable components
  llm_scorer.py          optional Claude reranker
  analytics.py           flow estimation, censoring, comparability classes
  gap_analysis.py        skill demand, blocking gap, adjacency, priority
  migrations.py          versioned schema (PRAGMA user_version)
frontend/js/charts.js    hand-rolled inline SVG charts
scripts/seed_cells.py    build the scrape matrix
scripts/sync_corpus.sh   pull the resume corpus in from the `resume` repo
deploy/                  systemd units + timer (see Deployment)
```

## Tests

```bash
uv run python -m pytest tests/ -q
```

Notable suites: `test_taxonomy.py` is a false-positive gauntlet ("go to market", "R&D", "a
ray of sunshine", "spark joy" must not match Go/R/Ray/Spark); `test_scheduler.py` simulates
200 runs to assert no cell starves; `test_roles.py` uses real titles from the database rather
than invented ones.

## Terms of service

LinkedIn's and Indeed's terms prohibit automated scraping. This is single-user, personal-use
scraping at low volume against public listing pages. The budget limits in `config.yaml` keep
request volume modest, which is also what keeps it working.
