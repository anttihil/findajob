# Find a Job

Scrapes job boards by configured query and location, then judges every posting against an
LLM-built profile of you.

Four stages, each on its own timer, handing off through one column:

```
search    scrape LinkedIn + Indeed on a rotating cell matrix   -> pipeline_state='new'
score     judge each posting against your profile              -> pipeline_state='scored'
start     serve the dashboard; scheduling is opt-in
```

They are separate commands rather than one pipeline because a failure in one must not cost
the work of another: a rate-limited board cannot stall scoring of the backlog, and an
expired API key cannot lose a scrape.

Underneath, `market/` still answers the questions the earlier version was built for — which
queries and locations actually have hiring supply, which of your skills are in demand, and which
missing skill most often blocks a posting you otherwise match.

## Setup

### Local

For normal use, run the installer. It manages the private runtime environment; users do not
need `uv`, npm, or Node.js.

```bash
./install.sh
```

It initializes storage and starts the dashboard at http://127.0.0.1:8010. Complete the
provider, resume, location, and query setup from the dashboard. Python 3.10+ is the only
runtime requirement; release wheels are built in CI with the frontend already included.

### Development checkout

```bash
# Install dependencies and compile frontend
make setup
make build

# 2. Configure an LLM provider and personal overrides
cp .env.example .env                          # add DEEPSEEK_API_KEY, or use an authenticated CLI provider
cp config.local.example.yaml config.local.yaml

# 3. Initialize schema, start the dashboard, and upload your resume
uv run findajob migrate
make start                                   # or: uv run findajob start --port 8010
# Open http://127.0.0.1:8010 and drop your PDF, TXT, or Markdown resume on the Resumes page.
```

### Global install

For normal use, install Find a Job once and run it from any directory:

```bash
uv tool install findajob
findajob init
findajob profile build /path/to/your-resume.pdf
findajob start
```

An installed copy keeps configuration, databases, and logs in your operating system's normal
application directories. Generated résumés go to your visible `Documents/Find a Job` folder
by default. Set `resumes.output_dir` in the generated configuration to choose another folder.
`FIND_A_JOB_HOME` makes a self-contained config/data/state/resume tree for automation or a
second profile; individual `FIND_A_JOB_*_PATH` variables remain available for precise control.

Source checkouts retain their existing repository-local paths, so development commands such as
`uv run findajob ...` continue to work without moving current data.

### Docker

```bash
# 1. Configure environment and persistent personal overrides
cp .env.example .env                         # add DEEPSEEK_API_KEY for the default Docker provider
cp config.local.example.yaml config.local.yaml

# 2. Start via Docker Compose
make docker-up                               # or: docker compose up -d

# Dashboard is live at http://localhost:8010
```

The frontend is built output, not something FastAPI generates at request time -- `findajob start`
serves whatever is in `careerradar/web/frontend/dist/` and 503s with a clear message if nothing has been
built yet. Re-run `npm run build` after pulling changes that touch
`careerradar/web/frontend-src/`, or run `npm run dev` for hot reload against a locally
running `findajob start` (see `vite.config.ts` for the dev proxy target).

`python-jobspy` is pinned to a git commit rather than the PyPI wheel, which constrains
numpy to 1.26.3 and has no cp313 wheel.

### Keys

`.env` (gitignored, loaded by `core/config.py`):

| variable | needed for |
|---|---|
| `DEEPSEEK_API_KEY` | profile extraction and scoring when using the DeepSeek API provider |
| `SCRAPER_PROXIES` | optional rotating pool. With it, LinkedIn becomes a description census instead of titles only |
| `FIND_A_JOB_OWNER` | the tailnet login allowed to reach the dashboard |

## Your profile

Find a Job never includes or expects personal career documents in the repository. On the
**Resumes** page, drop your own PDF, TXT, or Markdown resume; the same extraction model used
by the CLI drafts a profile, which you can refine with the profile copilot before saving.

For a terminal-only setup, pass the resume explicitly:

```bash
uv run findajob profile build /path/to/your-resume.pdf
uv run findajob profile show
```

The CLI saves the extracted profile immediately. The dashboard lets you review and refine it
before saving. In either case, profile data lives in the local database, not in Git.

The result is versioned and append-only. Every verdict records the `profile_version` that
produced it, so rebuilding your profile does not rewrite history — it lets you re-score and
compare.

**Skills are open-vocabulary and profile-driven.** The candidate profile defines skills directly
across any discipline or industry without requiring static taxonomy file maintenance. The model
reasons over candidate evidence and posting requirements directly during scoring rather than
relying on brittle keyword checklist matching.

## Running

```bash
# One manual run: scrape, then score the resulting backlog.
uv run findajob run

# Individual stages remain available for troubleshooting and development.
uv run findajob search run --dry-run
uv run findajob search run
uv run findajob score run --limit 200

# Serve dashboard. Automatic scheduling is disabled until you opt in.
uv run findajob start --port 8010
```

The dashboard's **Sync Now** action also runs search followed by scoring. To enable automatic
runs, set `scheduler.enabled: true` in your gitignored `config.local.yaml`; the schedule in
the tracked `config.yaml` then supplies the defaults. Dashboard configuration changes are
saved to `config.local.yaml`, so pulling project updates does not replace them.

### CLI automation

Humans and coding agents use the same commands. Add `--json` for one machine-readable result
on stdout; diagnostics remain on stderr. First build an active profile as described above.

```bash
# Search one board query outside the configured rotation.
uv run findajob search query --query "Platform Engineer" \
  --location "Helsinki, Finland" --country FI --indeed-country finland --json

# Inspect local normalized postings with stable filters and full descriptions.
uv run findajob jobs list --pipeline-state new --source indeed --limit 25 --json

# Use the configured Find a Job LLM provider for selected postings.
uv run findajob score jobs --job-id 123 --job-id 124 --json
```

An external reviewer may score without a separately configured provider: request a packet
containing the active-profile rules and posting text, then save its validated result.

```bash
uv run findajob score packet --job-id 123 --json
uv run findajob score verdict --job-id 123 --fit \
  --reason-type match --reason-description "Strong relevant platform experience." --json
```

See [AGENTS.md](AGENTS.md) for automation and privacy constraints. Explicit queries are stored
in the local database, but are deliberately excluded from the configured search matrix and its
coverage statistics.

### What scraping costs

Every cell visit records its wall time (`cell_observations.duration_ms`) and the HTTP
requests it issued (`.requests_made`), measured around the JobSpy call. The `cell_cost` view exposes
these rows with the ratios (`seconds`, `ms_per_request`, `requests_per_posting`) already
derived, for ad-hoc SQL:

```sql
SELECT source, AVG(seconds), AVG(requests_per_posting) FROM cell_cost GROUP BY source;
```

The number to watch is measured requests against what `scheduler.estimate_units` planned.
The `cell_cost` view exposes both and reveals when a source outspends its estimate. Rows
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

Search queries and locations are stored in SQLite (`search_queries`, `search_locations`) and
managed directly via the Web Dashboard or CLI (`findajob target`). The search matrix holds
the cross-product of enabled queries across enabled locations. The scheduler rotates through
these cells as a request-budget control, with cycle times tracking within the analysis window.

Skills are open-vocabulary, derived dynamically from the candidate's active profile and
target domain, allowing Find a Job to generalize cleanly across any discipline.

Scoring verdicts record the `profile_version` and `prompt_hash` to track provenance across
profile revisions and prompt iterations.

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
the projection can be recomputed over the whole corpus with no API calls.

`match_score` measures requirement coverage, not fit. It feeds the skill hint in the scoring
prompt, the gap analytics, and a tiebreak. BM25 was removed from it: its query was a fixed
bag of the candidate's skill labels, identical for every posting, so it restated the coverage
component beside it with more noise and no validation.

## How the verdicts are checked

There is no golden set yet, and two of the checks that matter need no labels at all.

Verdicts can be audited over stored verdicts with no API calls. A quote is or
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

Skill demand is a plain unweighted count over the eligible corpus, with Wilson confidence
intervals. It therefore reflects the scrape rotation, not the market. Selection bias is
**not** corrected; it is reported (`saturated_share`, `n_companies`, `max_company_share`).

Suppression is always visible with a reason, never silent.

## Deployment

The service runs the FastAPI dashboard. Its background scheduler remains off until you enable
it in `config.local.yaml`.

```bash
git clone https://github.com/anttihil/findajob.git ~/projects/findajob
cd ~/projects/findajob
make setup
cp .env.example .env                                   # configure API keys
cp config.local.example.yaml config.local.yaml
uv run findajob migrate                             # applies migrations & auto-seeds scrape cells

make build                                             # -> careerradar/web/frontend/dist/

sudo ./deploy/install-systemd.sh --user "$(id -un)" --install-dir "$PWD"
```

The installer validates the checkout, then renders the systemd and logrotate files for that
specific Unix user and directory. Add `--no-start` to install without immediately starting the
service. Keep personal overrides in `config.local.yaml`; project defaults remain in
`config.yaml` and can update safely with Git.

When enabled, the scheduler runs search and score as isolated worker subprocesses. Manual
runs are always available through **Sync Now** or `uv run findajob run`.

The web service binds `127.0.0.1:8010` and stays there. Tailscale fronts it:

```bash
sudo tailscale serve --bg --https 9443 8010    # https://<host>.ts.net:9443
```

### The owner gate

`FIND_A_JOB_OWNER` is the tailnet login allowed through. Tailscale Serve *sets*
`Tailscale-User-Login` on each proxied request and strips any copy the client supplied, so a
forged header never reaches the app — but that is only true because nothing except Serve can
reach the port, which is what the loopback bind guarantees.

A request with no identity header is treated as a genuinely local caller (the CLI, a health
check) and allowed. If `FIND_A_JOB_OWNER` is unset, every proxied request is refused.

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
├── search/     targets, scheduler, sources, circuit breaker, proxies, normalizer
├── profile/    resume parsing, copilot refinement, canonicalization, versioned store
├── scoring/    prompts, per-posting graph, queue worker
├── market/     supply analytics, skill-gap analysis
├── web/        FastAPI app, owner gate, JSON API, SPA shell
│   ├── frontend-src/   Preact + TypeScript dashboard (Vite), source of truth
│   └── frontend/dist/  built output `npm run build` produces, gitignored
└── cli.py
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

418 tests, no API calls — the model is faked where behaviour around it is what matters.
Notable ones: `test_scheduler.py` simulates 200 runs and asserts no cell starves;
`test_scoring_module.py` asserts the cached half of the prompt contains no posting data;
`test_profile_module.py` asserts a rendered profile is byte-stable under skill reordering.

## Terms of service

LinkedIn's and Indeed's terms prohibit automated scraping. This is single-user, personal-use
scraping at low volume against public listing pages, with a circuit breaker that backs off
rather than retrying a rate limit. Run it against your own job search, not as a service.
