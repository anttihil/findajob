# Operating a deployment

Three ways to look at a running instance, in the order you should reach for them.

## 1. `careerradar status`

One health report over the whole pipeline, straight from the database. No API calls, no
network, safe to run at any time.

```bash
careerradar status
careerradar status --json          # for piping
ssh <prod-host> 'cd <install-dir> && .venv/bin/careerradar status'
```

It answers what nothing else does: how long since each stage last did anything, how big the
scoring backlog is and how old its oldest posting is, whether verdict coverage is even
across sources, whether cells are being revisited inside their tier's cadence, what is
quarantined after repeated failures, and whether stored postings still agree with the
taxonomy on disk.

**Read the coverage section first.** Every hit rate in this project — the target roles table
included — is a ratio over *scored* postings. If one source is scored at
67% and another at 27%, those ratios describe the scored subset rather than the market. The
report prints a warning when the spread crosses 20 points.

Two failures it exists to catch, both of which happened:

- A stage timer sitting disabled. `careerradar-score.timer` was disabled on the production
  host for weeks; 62% of the corpus went unjudged, and every family hit rate silently
  skewed toward whatever had been scored.
- A cold backlog deadlocking against `scoring.max_usd_per_run`. The pre-flight aborts the
  whole run rather than trimming the queue, by design — so a backlog whose estimate exceeds
  the ceiling is never scored at all, on every run, forever. `status` shows the backlog; the
  ceiling is in `config.yaml`.

## 2. The JSON API, over SSH

The dashboard already serves its data as JSON, and `uvicorn` binds `127.0.0.1`, so the
endpoints are reachable through an SSH session without exposing anything:

```bash
ssh <prod-host> curl -s localhost:8010/api/sync/status
ssh <prod-host> curl -s localhost:8010/api/market/coverage
ssh <prod-host> curl -s localhost:8010/api/stats
```

`/api/sync/status` reports the scheduler's own view, including cells past their staleness
floor. `/api/market/coverage` is per-cell health — one row per (source, family, location,
query), with `ewma_new_per_scrape` and `ewma_fit_score`, which is how you tell whether a
particular query phrasing is earning its cell.

The owner gate (`restrict_to_owner`) checks a `tailscale-user-login` header and lets
unheadered requests through. That is safe only because the process binds loopback. **If you
ever bind a public interface, that gate is not sufficient on its own.**

## 3. A read-only snapshot, for analysis

Anything heavier than a status check — corpus statistics, hit rates by title shape, testing
a taxonomy edit against real postings — wants a local copy and ad-hoc SQL, not an endpoint.

```bash
# on the dev machine
mkdir -p /tmp/careerradar-prod
rsync -avz <prod-host>:<install-dir>/jobs.db /tmp/careerradar-prod/
sqlite3 /tmp/careerradar-prod/jobs.db 'pragma quick_check;'
```

Rules that make this safe:

- **Copy, never query in place.** The live database is in WAL mode and is being written by
  four services. Open the copy, not the original; if you must touch the original, open it
  `-readonly`.
- **Pull the `-wal` and `-shm` files too**, or the copy is missing whatever has not been
  checkpointed yet.
- **Never rsync back.** There is no merge path — the copy diverges the moment the next
  scrape lands.
- **Do not commit the snapshot, or a script containing your hostnames and paths.** The
  procedure is documented here on purpose and the script is deliberately not in the
  repository; a snapshot is a copy of a personal job search, and a published repository is
  the wrong place for either it or the map to it.

Analysis done this way is why several historical benchmark figures carry a note saying
which database produced them. A figure computed on a development copy and a figure computed
on production have differed by up to 7x in this project, in both directions. Say which one
you used.

## The log file

`app.log` in the install directory is still the one place to read. Every stage and the web
server append to it at DEBUG level; the journal holds the same records at INFO under each
unit's identifier, which is the faster way to read one run in isolation:

```bash
tail -f <install-dir>/app.log
journalctl -t careerradar-search -S -1h     # or -score, -research, -web
```

**Rotation is logrotate's, not the application's.** Four processes hold `app.log` open at
once, and the old in-process `RotatingFileHandler` assumed a single writer: when one stage
rotated, the other three kept appending to the renamed file, which the next rotation
deleted. `deploy/install-systemd.sh` generates a `copytruncate` policy so every open handle survives a
rotation. It is a one-time install and nothing warns you if you skip it — the file simply
grows until the disk does:

```bash
sudo ./deploy/install-systemd.sh --user "$(id -un)" --install-dir "$PWD" --no-start
sudo logrotate --debug /etc/logrotate.d/careerradar   # dry run, prints what it would do
```

## The stages queue, they do not overlap

`search`, `score`, `run` and `migrate` take an exclusive `flock` on `.pipeline.lock`
before they touch the database, and hold it until the process exits. A stage that finds the
lock taken says so on stderr and waits:

```
score: another pipeline stage holds the database; waiting.
```

This is not a fault. The timers are independent -- a scrape takes ~20 minutes and scoring
fires every half hour -- so an overlap is normal, and SQLite admits one writer at a time.
Two stages writing together outlived the 30s `busy_timeout` and failed with `database is
locked`. Waiting costs nothing: the queue lives in `jobs.pipeline_state`, so a stage that
starts late still drains exactly what it would have drained.

The wait is unbounded here on purpose. `TimeoutStartSec` in each unit is the bound, and a
second one in the code would only disagree with it. The dashboard's Sync button takes the
same lock; read-only commands (`status`, `profile show`, `target list`) and `start` never do.

Nothing needs to be cleaned up after a crash. The kernel drops an `flock` when the holder
dies, unlike the `sync_status.json` lock, which needs `STALE_LOCK_MINUTES` to recover.

## Deploying an update

Prod is a plain git checkout, so a deploy is a pull plus whatever the change touched.
Personal settings belong in the gitignored `config.local.yaml`; pulling updates changes only
the tracked defaults in `config.yaml`.

```bash
ssh <prod-host>
cd <install-dir>
git pull
uv sync                                 # only if Python dependencies changed
uv run careerradar migrate              # applies migrations and auto-seeds/prunes cells
npm ci && npm run build                 # only if careerradar/web/frontend-src/ changed --
                                         # the built output is gitignored, so a pull alone
                                         # leaves the previous build in place until this runs
sudo systemctl restart careerradar      # restarts dashboard and any enabled scheduler
uv run careerradar status               # confirm the pipeline still reads healthy
```

## Deploying a taxonomy change
 
Skills are open-vocabulary and derived dynamically from the candidate's active profile and target domain.

Target roles, queries, and locations are managed directly in SQLite via the web dashboard or CLI:
 
```bash
careerradar target add role "AI Engineer" --pattern "ai engineer|machine learning engineer"
careerradar target add query "AI Engineer" "AI Engineer"
careerradar search seed-cells --prune
```
 
This syncs configured target queries into `scrape_cells` (with `--prune` to disable retired cells).
 
And a pattern edit is **not retroactive**. `role_family` is written at ingest, and
`--rescore-only` passes it through rather than re-deriving it, so a pattern edit applies to
postings scraped after it lands and to nothing already stored.
