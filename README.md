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
cp /path/to/.env .                         # see below
scripts/sync_corpus.sh /path/to/resume     # pulls achievements.md in
uv run careerradar migrate                 # schema
uv run careerradar profile build           # the interview -- do this first

npm install                                # dashboard frontend (Preact + TypeScript)
npm run build                              # -> careerradar/web/frontend/dist/, gitignored
```

The frontend is built output, not something FastAPI generates at request time -- `web/app.py`
serves whatever is in `frontend/dist/` and 503s with a clear message if nothing has been
built yet. Re-run `npm run build` after pulling changes that touch
`careerradar/web/frontend-src/`, or run `npm run dev` for hot reload against a locally
running `careerradar web` (see `vite.config.ts` for the dev proxy target).

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

`profile build` reads your corpus, extracts what the documents support, then works out what
they *cannot* tell it and asks you. Career documents are a sales artifact: they
systematically omit honest weaknesses, compensation floors, work authorization, and what
you would refuse. Those are exactly the facts that decide whether a posting is a blocker or
a stretch.

### The corpus is an explicit list

`profile.corpus` in config.yaml names the documents. Markdown, plain text, and PDF are all
read; a document that is named but missing is an error rather than a shrug.

```yaml
profile:
  corpus:
    - path: achievements.md
      kind: achievements
    - path: resumes/resume.txt
      kind: resume
```

This used to be a scan of `resumes/`, which is quietly dangerous, because that directory
also holds tailored resumes written for *submitting* to employers — a different kind of
document from evidence about what you can actually do. Six LLM-generated variants were
being read as evidence, and their inflated skill lists (Go listed first on the strength of
one side project; NestJS, D3.js and CloudFormation with no backing work) became skill
levels and then scores. Nothing failed; the numbers were just wrong.

Adding a document to the profile should be a decision, not a side effect of where a file
happens to live. `scripts/sync_corpus.sh` no longer uses `rsync --delete` for the same
reason: it was deleting hand-curated documents that exist only here.

It is a LangGraph graph checkpointed to `graphs.db`, so you can stop at question four and
resume a week later (`--resume`) with the extraction intact.

- `--no-interview` builds from the documents alone. Useful for a first pass; the
  constraints will be empty until you actually sit the interview.
- `--resume` continues an interview you left unfinished.
- `--force` rebuilds even when the corpus has not changed.

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
uv run careerradar search cost                         # what the last run cost, per cell

# Score. Cheap, idempotent, safe to run often.
uv run careerradar score run --dry-run                 # cost estimate, writes nothing
uv run careerradar score run --limit 200
uv run careerradar score run --rescore-all             # after a profile rebuild

# Research the companies behind strong matches.
uv run careerradar research run --dry-run
uv run careerradar research run --company "MongoDB"

uv run careerradar web --port 8010
```

### What scraping costs

Every cell visit records its wall time (`cell_observations.duration_ms`) and the HTTP
requests it issued (`.requests_made`), measured around the JobSpy call. `careerradar search
cost` reports them per source and lists the slowest cells; the `cell_cost` view exposes the
same rows with the ratios (`seconds`, `ms_per_request`, `requests_per_posting`) already
derived, for ad-hoc SQL:

```sql
SELECT source, AVG(seconds), AVG(requests_per_posting) FROM cell_cost GROUP BY source;
```

The number to watch is measured requests against what `scheduler.estimate_units` planned.
`careerradar search cost` prints both and warns when a source outspends its estimate. Rows
scraped before schema v11 are NULL and are reported as unmeasured.

The first measurements (2026-08-15, 11 cells) showed the estimate was wrong in both
directions, and the cost model was corrected against them:

| | before | measured | after |
|---|---|---|---|
| Indeed cell, 75 wanted | 5 requests | 1-2 | 2 (`page_size: 100`, +1 for the request that finds the end) |
| LinkedIn census cell, 50 wanted | 5 requests | 56 | 56 (6 pages + `requests_per_description: 1` x 50) |

`request_units` is what decides how many cells a run visits, so undercounting LinkedIn by
11x meant its budget bought a tenth of the cells it claimed. The proxied LinkedIn budget is
now `request_units: 1010` / `max_pages_per_run: 108` against `searches_per_run: 18` — the
same 18 cells a run that were already happening, with the arithmetic that describes them
correctly.

**LinkedIn descriptions come from the guest fragment, not the job page.** JobSpy fetches
each description from `/jobs/view/<id>` (~302KB median); `/jobs-guest/jobs/api/jobPosting/<id>`
returns a byte-identical description in ~49KB, and was faster on 16 of 16 paired postings
(240ms vs 630ms median, `scripts/probe_linkedin_description_endpoint.py`). `jobspy_source.py`
rewrites the URL under the library, which keeps JobSpy's own parsing of industry, job
function, employment type, apply URL and logo. Only `job_level` differs, and nothing reads
it. Measured on a live 50-posting cell: 64.7s before, 37.4s after, same 56 requests.

### What scoring costs

Measured against this corpus, not estimated:

| | |
|---|---|
| model | `deepseek-v4-flash` |
| per posting | ~$0.00057 (17,471 verdicts, $9.89) |
| tokens per posting | 6,045 in (81% served from cache) / 1,392 out |
| 300-posting run | ~$0.17 |
| full 21,700-posting corpus | ~$12.30 |

Retries are on top of that: a posting the model answers unusably is called again, and
~4% of the bill buys no verdict. The verdict rows carry it since 2026-08-16; older rows
record only the attempt that succeeded, so summing `job_verdicts.cost_usd` over them
reads low by about that much.

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
**410 cells** (205 per source) on the assumption that LinkedIn rate-limits around the 10th
page on a single IP — a figure carried forward from prior scraping experience, not something
this project ever measured. A direct probe (`scripts/probe_linkedin_page_wall.py`) found zero
429s or blocks across 99 consecutive pages (990 results) on one proxied IP; the run stopped at
page 100 only because that's LinkedIn's own guest-API pagination ceiling (offset ~1000, the
same wall JobSpy hardcodes as `start < 1000`), not because anything got flagged. See
`experiments/linkedin_page_wall/`. The scheduler still *rotates* through the 410 cells rather
than sweeping them, now as a request-budget control rather than a proven rate-limit dodge, so
a full matrix cycle takes roughly 5 days — which is why `analytics.min_window_days` is 30.

`data/skills.yaml` is the shared vocabulary: 172 canonical skills with aliases. The profile
and scraped descriptions both map onto these keys, which is what makes a match mean the same
thing on both sides.

Editing either file changes what a run measures, so `taxonomy_hash` and `plan_hash` are
recorded on every run and every stats row, and trend queries refuse to compare across a
change.

## The scoring agent answers questions; it does not produce a score

Five ordinals, each with anchored criteria in `careerradar/scoring/rubric.py`:

| dimension | values |
|---|---|
| `eligibility` | eligible / conditional / blocked |
| `role_match` | same_role / adjacent / different_domain / different_field |
| `capability_match` | exceeds / meets / most_with_gaps / major_gaps / not_close |
| `seniority_gap` | matched / candidate_above / candidate_below |
| `evidence_quality` | strong / adequate / thin |

They are what gets stored. The earlier schema asked the model for `fit_score: int` and got
53 distinct values across 5,511 verdicts, 99.84% of which agreed with the band its own
prompt assigned -- inside `worth_applying`, the value 62 alone was 45% of the band. It was
picking a label and decorating it with digits.

**Ranking does not merge them.** `eligibility` partitions: nothing blocked outranks anything
eligible, at any tier. Within a partition, `scoring/scale.py` orders by Pareto dominance --
A beats B only if A is at least as good on every dimension. Equal tiers are honest ties, not
equal quality. A weighted score would have to assert an exchange rate between the dimensions
and nothing supports one.

`fit_score` survives as a *projection* of the tuple, for the places that need one number. It
is an invented weighting and is labelled as such. Because the ordinals are stored,
`careerradar score rescale` recomputes it over the whole corpus with no API calls.

`match_score` measures requirement coverage, not fit. It feeds the skill hint in the scoring
prompt, the gap analytics, and a tiebreak. BM25 was removed from it: its query was a fixed
bag of the candidate's skill labels, identical for every posting, so it restated the coverage
component beside it with more noise and no validation.

## How the verdicts are checked

There is no golden set yet, and two of the checks that matter need no labels at all.

`careerradar score audit` re-runs both over stored verdicts with no API calls. A quote is or
is not in the posting -- measured over 9,053 stored blockers, 89.6% were verifiable and ~10%
could not be found. And a blocker either does or does not demand something the profile says
you already have; that check found ~50 verdicts blocking a US citizen for not being a US
citizen.

`uv run python -m evals.metamorphic` supplies directional ground truth by construction:
append a clearance requirement and `eligibility` must become `blocked`; add a must-have the
candidate lacks and `capability_match` must not improve. Invariance perturbations (renaming
the company, reordering bullets) are reported rather than gated. ~$0.03 a run.

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

Runs as a single unified service: FastAPI dashboard and background `asyncio` scheduler running
in one process, executing pipeline stages (`search`, `score`, `research`) as isolated worker
subprocesses with automated pipeline chaining.

```bash
git clone https://github.com/your-username/careerradar.git ~/projects/careerradar
cd ~/projects/careerradar
uv sync
scripts/sync_corpus.sh /path/to/resume
cp /path/to/.env .
sqlite3 /path/to/old/jobs.db ".backup 'jobs.db'"   # WAL mode: never plain-copy a live DB
uv run careerradar migrate                         # applies migrations & auto-seeds scrape cells

npm ci                                             # frontend, locked to package-lock.json
npm run build                                      # -> frontend/dist/; careerradar-web serves it

sudo cp deploy/careerradar.service /etc/systemd/system/
sudo install -m 0644 deploy/careerradar.logrotate /etc/logrotate.d/careerradar
sudo systemctl daemon-reload
sudo systemctl enable --now careerradar.service
```

The unit hardcodes the app root and `User=`; the logrotate snippet hardcodes the same two.
Adjust all of them if the server layout differs.

The background scheduler manages stage execution, boot catch-up (`persistent_catchup: true`),
and pipeline chaining (Search $\rightarrow$ Score $\rightarrow$ Research) as configured in `config.yaml`.

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
├── web/        FastAPI app, owner gate, JSON API, SPA shell
│   ├── frontend-src/   Preact + TypeScript dashboard (Vite), source of truth
│   └── frontend/dist/  built output `npm run build` produces, gitignored
└── cli.py
data/           roles.yaml, skills.yaml
deploy/         systemd units, logrotate snippet for app.log
docs/           deepseek.md -- the API constraints the scoring design rests on
                operations.md -- status, the JSON API over ssh, read-only snapshots, logs
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
