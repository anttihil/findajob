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

**Read the coverage section first.** Every hit rate in this project — the tier table in
`data/roles.yaml` included — is a ratio over *scored* postings. If one source is scored at
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

Analysis done this way is why several numbers in `data/roles.yaml` carry a note saying
which database produced them. A figure computed on a development copy and a figure computed
on production have differed by up to 7x in this project, in both directions. Say which one
you used.

## Deploying an update

Prod is a plain git checkout, so a deploy is a pull plus whatever the change touched.
Order matters: config and taxonomy before the commands that read them, and never enable a
timer before the config it depends on has landed.

```bash
ssh <prod-host>
cd <install-dir>
git pull
uv sync                                 # only if dependencies changed
uv run careerradar migrate              # only if a migration was added
sudo systemctl restart careerradar-web  # only if the web app or its templates changed
uv run careerradar status               # confirm the pipeline still reads healthy
```

Timers are the part that is easy to forget, because a disabled one produces no error
anywhere — it simply never runs:

```bash
systemctl list-timers 'careerradar-*'   # enabled AND scheduled, not just present
systemctl is-enabled careerradar-score.timer
```

## Deploying a taxonomy change

Two things do NOT happen on their own after you edit `data/roles.yaml`:

```bash
careerradar search seed-cells --prune   # query_terms changes reach the cell matrix
```

Without it the matrix keeps running whatever queries it was seeded with. `--prune` disables
cells that are no longer in the taxonomy rather than deleting them, so their history stays
auditable.

And a pattern edit is **not retroactive**. `role_family` is written at ingest, and
`--rescore-only` passes it through rather than re-deriving it, so a pattern edit applies to
postings scraped after it lands and to nothing already stored.
