# CareerRadar

Scrapes job boards by role family and location, judges every posting against an LLM-built
profile of you, and researches the companies behind the good ones.

Four stages, each on its own timer, handing off through one column:

```
search    scrape LinkedIn + Indeed on a rotating cell matrix   -> pipeline_state='new'
score     judge each posting against your profile              -> pipeline_state='scored'
research  build a dossier for companies behind strong matches  -> pipeline_state='researched'
web       serve the dashboard
```

They are separate commands rather than one pipeline because a failure in one must not cost
the work of another: a rate-limited board cannot stall scoring of the backlog, and an
expired API key cannot lose a scrape.

Underneath, `market/` still answers the questions the earlier version was built for — which
role families actually have hiring supply, which of your skills are in demand, and which
missing skill most often blocks a posting you otherwise match.

## Setup

```bash
uv sync
scripts/sync_corpus.sh /path/to/resume     # the gitignored resume corpus
cp /path/to/.env .                         # see below
uv run careerradar migrate                 # schema
uv run careerradar profile build           # the interview -- do this first
```

`python-jobspy` is pinned to a git commit rather than the PyPI wheel, which constrains
numpy to 1.26.3 and has no cp313 wheel.

### Keys

`.env` (gitignored, loaded by `core/config.py`):

| variable | needed for |
|---|---|
| `DEEPSEEK_API_KEY` | **required** — profile, scoring, research |
| `TAVILY_API_KEY` | company intel and contacts. Without it, research still finds other openings from the corpus and says plainly that no web research happened |
| `SCRAPER_PROXIES` | optional rotating pool. With it, LinkedIn becomes a description census instead of titles only |
| `CAREERRADAR_OWNER` | the tailnet login allowed to reach the dashboard |

## The profile

Everything downstream depends on this, so it is built once, deliberately, with you in the
room.

```bash
uv run careerradar profile build      # ingest documents, then interview you
uv run careerradar profile show
uv run careerradar profile history
```

`profile build` reads your corpus — six tailored resumes, `current_resume.md`,
`achievements.md`, PDFs included — extracts what the documents support, then works out what
they *cannot* tell it and asks you. Career documents are a sales artifact: they
systematically omit honest weaknesses, compensation floors, work authorization, and what
you would refuse. Those are exactly the facts that decide whether a posting is a blocker or
a stretch.

It is a LangGraph graph checkpointed to `graphs.db`, so you can stop at question four and
resume a week later (`--resume`) with the extraction intact.

- `--no-interview` builds from the documents alone. Useful for a first pass; the
  constraints will be empty until you actually sit the interview.
- `--restart` abandons an in-progress interview and starts over.

The result is versioned and append-only. Every verdict records the `profile_version` that
produced it, so rebuilding your profile does not rewrite history — it lets you re-score and
compare.

**Skill keys are canonicalized against `data/skills.yaml` on the way in.** The model names
skills the way a person would ("ReactJS", "D3.js"); the keyword layer looks them up by
canonical key (`react`, `d3`). Left alone that mismatch is silent and expensive — a profile
saying `reactjs` at level 3 answers `level("react") == 0`, so every React requirement scores
as a gap. The first real build produced exactly this for 4 of 28 skills.

## Running

```bash
# Scrape. Postings land unscored.
uv run careerradar search run --dry-run --limit 3     # verify without writing
uv run careerradar search run --backfill --source indeed
uv run careerradar search run                          # steady state

# Score. Cheap, idempotent, safe to run often.
uv run careerradar score run --dry-run                 # cost estimate, writes nothing
uv run careerradar score run --limit 200
uv run careerradar score run --rescore-all             # after a profile rebuild

# Research the companies behind strong matches.
uv run careerradar research run --dry-run
uv run careerradar research run --company "MongoDB"

uv run careerradar web --port 8010
```

### What scoring costs

Measured against this corpus, not estimated:

| | |
|---|---|
| model | `deepseek-v4-flash` |
| per posting | ~$0.0003 |
| 300-posting run | $0.0966, 68% of input tokens served from cache |
| full 5,900-posting backlog | ~$1.80 |

That is cheap enough that scoring is not a thing to ration — which is the point. The old
design gated storage on a keyword threshold because judgement was expensive; now every
posting that classifies as software work gets read properly.

The economics come from DeepSeek's automatic prefix caching: the system rules plus your
frozen profile prefix are byte-identical on every call and bill at 1/50th the input rate.
That makes prompt layout architectural — see `docs/deepseek.md`, and the regression check
in `scoring/worker.py` that warns if the cache rate collapses.

`scoring.max_usd_per_run` is a pre-flight gate that **aborts** rather than trimming the
queue. A budget that silently drops the tail produces a partial pass that looks complete,
and every coverage figure downstream inherits the shortfall.

## How the search space is defined

`data/roles.yaml` holds ~28 role families × 7 locations. The cross-product is pruned to
**252 cells** (126 per source) because LinkedIn rate-limits around the 10th page on a single
IP. The scheduler *rotates* through those cells rather than sweeping them, so a full matrix
cycle takes roughly 5 days — which is why `analytics.min_window_days` is 30.

`data/skills.yaml` is the shared vocabulary: 172 canonical skills with aliases. The profile
and scraped descriptions both map onto these keys, which is what makes a match mean the same
thing on both sides.

Editing either file changes what a run measures, so `taxonomy_hash` and `plan_hash` are
recorded on every run and every stats row, and trend queries refuse to compare across a
change.

## Two scores, on purpose

| | what it is | drives |
|---|---|---|
| `match_score` | keyword coverage + BM25 + family tier + seniority. Deterministic, reproducible. | the skill-gap analytics |
| `fit_score` | the scoring agent's judgement, 0–100, with quoted blockers | dashboard ranking, the research queue |

They can disagree, and when they do that is informative rather than a bug. Keyword coverage
is what misjudges a career change or an unusual title; it is kept because `gap_analysis`
measures its blocking gap against postings you match at `GOOD_FIT_THRESHOLD` or better, and
swapping in a judgement score would silently change what those charts mean.

Every hard blocker quotes the phrase from the posting that makes it one, so a verdict can be
checked against its evidence instead of trusted.

## Reading the numbers honestly

Supply is a **rate**, not a total. Boards never report how many postings exist and every
result set is truncated, so absolute supply is not estimable and is never claimed. An amber
bar with an arrow cap and a `≥` label is a lower bound.

"None observed" is not zero demand — a blank heatmap cell means never scraped, and the
coverage strip says which.

Each location is charted separately because flow is only comparable within one
location+source pair. There is no pooled cross-location ranking anywhere in the product.

Skill demand is post-stratified against a *declared* `analytics.reference_mix` with Kish
`n_eff` confidence intervals, falling back to unweighted-and-labelled figures until rotation
coverage is adequate. Within-stratum selection bias is **not** corrected; it is reported
(`saturated_share`, `n_companies`, `max_company_share`).

Suppression is always visible with a reason, never silent.

The same standard applies to dossiers: source URLs are recorded from what was actually
fetched, never authored by the model. Asked to supply them, it produced six plausible,
well-formed, entirely invented URLs on a run where no search had happened — a failure
invisible precisely because the URLs looked right. When web search is unavailable, the
dossier says so.

## Deployment

Runs on a Tailscale-reachable home server: one long-lived web service and three `oneshot`
units driven by timers.

| unit | schedule | why |
|---|---|---|
| `careerradar-search.timer` | 07:00, 19:00 ±30min | matches the 252-cell/5-day rotation. `Persistent=true` — a missed run means suppressed supply figures, not a neutral gap |
| `careerradar-score.timer` | every 30 min ±3min | cheap and idempotent; a posting scraped at 07:00 is judged by 07:30. `Persistent=false` — the queue is in the database, nothing to catch up |
| `careerradar-research.timer` | 20:30 ±20min | after the evening scrape and its scoring have settled |
| `careerradar-web.service` | always | binds `127.0.0.1:8010` |

```bash
git clone https://github.com/your-username/careerradar.git ~/projects/careerradar
cd ~/projects/careerradar
uv sync
scripts/sync_corpus.sh /path/to/resume
cp /path/to/.env .
sqlite3 /path/to/old/jobs.db ".backup 'jobs.db'"   # WAL mode: never plain-copy a live DB
uv run careerradar migrate

sudo cp deploy/careerradar-*.service deploy/careerradar-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now careerradar-web.service \
    careerradar-search.timer careerradar-score.timer careerradar-research.timer
```

The units hardcode the app root and `User=`; adjust both if the server layout differs.

`careerradar-search.service` deliberately has **no `Restart=`** — the circuit breaker has
already decided how long to back off, and restarting would discard that decision and
re-approach a board that just rate-limited us. The score and research units *do* retry: they
talk to an API with its own rate-limit semantics, and a failed posting simply stays queued.

The web service binds `127.0.0.1:8010` and stays there. Tailscale fronts it:

```bash
sudo tailscale serve --bg --https 9443 8010    # https://<host>.ts.net:9443
```

### The owner gate

`CAREERRADAR_OWNER` is the tailnet login allowed through. Tailscale Serve *sets*
`Tailscale-User-Login` on each proxied request and strips any copy the client supplied, so a
forged header never reaches the app — but that is only true because nothing except Serve can
reach the port, which is what the loopback bind guarantees.

A request with no identity header is treated as a genuinely local caller (the CLI, a health
check) and allowed. If `CAREERRADAR_OWNER` is unset, every proxied request is refused.

**Do not expose this via `tailscale funnel`:** Funnel traffic carries no identity headers, so
every request would look local.

**Known gap:** Serve does not populate identity headers for traffic from *tagged* devices, so
a tagged node would arrive looking local and be let through. There are no tagged devices on
this tailnet, and tags only exist when created deliberately. The fix is to give up the
loopback TCP port and have Serve proxy to a Unix socket (`tailscale serve unix:...` +
`uvicorn --uds`), after which a missing header can be refused outright.

## Layout

```
careerradar/
├── core/       config, db, migrations, logging, paths, LLM construction + cost
├── taxonomy/   roles.yaml and skills.yaml loaders -- the shared vocabulary
├── profile/    document ingest, interview graph, canonicalization, versioned store
├── search/     scheduler, sources, circuit breaker, proxies, normalizer, keyword score
├── scoring/    prompts, per-posting graph, queue worker
├── research/   company dossier graph, search tools, queue worker
├── market/     supply analytics, skill-gap analysis, digests
├── web/        FastAPI app, owner gate, frontend/
└── cli.py
data/           roles.yaml, skills.yaml
deploy/         systemd units
docs/           deepseek.md -- the API constraints the scoring design rests on
```

`core/paths.py` is the single definition of where anything lives. Twelve modules used to
recompute the repo root from their own depth in the tree, which broke silently the moment
the package was reorganized.

## Tests

```bash
uv run python -m pytest tests/ -q
```

279 tests, no API calls — the model is faked where behaviour around it is what matters.
Notable ones: `test_scheduler.py` simulates 200 runs and asserts no cell starves;
`test_taxonomy.py` is a false-positive gauntlet ("go to market", "a ray of sunshine",
"spark joy" must not match Go, Ray, Spark); `test_scoring_module.py` asserts the cached half
of the prompt contains no posting data; `test_profile_module.py` asserts a rendered profile
is byte-stable under skill reordering.

Several suites depend on the gitignored corpus and skip without it.

## Terms of service

LinkedIn's and Indeed's terms prohibit automated scraping. This is single-user, personal-use
scraping at low volume against public listing pages, with a circuit breaker that backs off
rather than retrying a rate limit. Run it against your own job search, not as a service.
