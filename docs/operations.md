# Operating a deployment

This page covers routine checks and maintenance for a running Find a Job instance. See the
root [README](../README.md) for installation and general usage.

## Health checks

`status` reads the local database and makes no API calls:

```bash
findajob status
findajob status --json
```

It reports recent search and scoring activity, the unscored backlog, cell health, verdict
coverage, quarantined postings, and the active search-target fingerprint. A stalled scorer or
quarantined posting can usually be investigated with:

```bash
systemctl status findajob.service
findajob score retry
```

## Secondary verdict review

Production scoring uses a two-stage LangGraph workflow:
`render → score → capture → review`. Both positive and negative first-line decisions
are checked. Each model gets up to three structured-output attempts. The reviewer's
decision becomes the final verdict used by the dashboard, analytics, and scrape-cell quality.

The defaults are enabled with `deepseek-v4-pro`. Override them in `config.local.yaml`:

```yaml
scoring:
  review:
    enabled: true
    model: deepseek-v4-pro
```

The reviewer uses the configured LLM provider. Batch scoring, selected-job scoring, and
import-time scoring all use this workflow. Prompt evaluations in the labeler explicitly
use the first-line stage only to preserve single-model comparisons.

Migration v32 adds `verdict_reviews`. It preserves the first-line verdict and its usage,
plus the latest review state with cumulative costs, errors, and attempts. The worker
commits the first-line result before starting review. A failed review publishes no new
final verdict; the next scoring run resumes review without repeating the first-line call.
Recovery requires matching profile/rules, rendered posting, and model configuration;
changing these starts a fresh assessment. Repeated failures use the existing quarantine
and `findajob score retry` mechanism.

Run spending includes both stages and their retries, charging only new calls when resuming.
Work is submitted in batches bounded by `scoring.concurrency`; a batch already started
finishes and is fully accounted for before the budget stops the next batch. Consequently,
the measured spending ceiling may be exceeded by the current batch.

The CLI reports reviewed, overturned, and review-failure counts. Existing stored verdicts
are reviewed when explicitly rescored, rather than automatically backfilled.

## Accessing a loopback deployment

The dashboard's JSON endpoints can be queried through SSH without exposing port 8010:

```bash
ssh <host> 'curl -s http://127.0.0.1:8010/api/sync/status'
ssh <host> 'curl -s http://127.0.0.1:8010/api/market/coverage'
ssh <host> 'curl -s http://127.0.0.1:8010/api/stats'
```

If the dashboard is exposed through a reverse proxy or Tailscale, configure the application's
authentication environment variables appropriately. Do not expose an unauthenticated
instance on a public interface.

## Logs

The application writes to the configured `app.log` path. For a systemd deployment, use either
the file or the journal:

```bash
tail -f <app-log-path>
journalctl -u findajob.service -S -1h
```

`deploy/install-systemd.sh` also installs a logrotate rule using `copytruncate`. Run that
installer for systemd deployments so the shared log remains bounded.

## Database snapshots

For analysis, copy the database and query the copy—not the live database. SQLite uses WAL mode,
so copy the `jobs.db`, `jobs.db-wal`, and `jobs.db-shm` files together when they exist.

```bash
mkdir -p /tmp/findajob-snapshot
rsync -av <host>:<data-dir>/jobs.db* /tmp/findajob-snapshot/
sqlite3 /tmp/findajob-snapshot/jobs.db 'pragma quick_check;'
```

Never rsync a snapshot back to the deployment, and do not commit it: it contains personal job
search data.

## Concurrent stages

The writing commands `search`, `score`, `run`, and `migrate` serialize through the shared
`.pipeline.lock`. If another stage is running, the next one waits. The lock is released by the
operating system when the process exits, including after a crash; no manual lock cleanup is
needed.

## Deploying updates

For a source-checkout deployment:

```bash
cd <install-dir>
git pull
uv sync                              # if Python dependencies changed
uv run findajob migrate
npm ci && npm run build               # if frontend source changed
sudo systemctl restart findajob.service
uv run findajob status
```

Keep personal settings in the gitignored `config.local.yaml` and credentials in `.env`.

To install or refresh the systemd service for a checkout:

```bash
sudo ./deploy/install-systemd.sh \
  --user "$(id -un)" --install-dir "$(pwd)"
```

## Changing search targets

Queries can be changed from the dashboard or CLI. Locations are managed from the dashboard.
After changing targets, refresh the scrape matrix:

```bash
findajob target add "AI Engineer"
findajob search seed-cells --prune
```

The next search pass uses the refreshed matrix. Existing postings are not reclassified merely
because a search target changes.
