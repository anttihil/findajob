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

For billing-only recovery, follow [Recover jobs quarantined by billing failures](billing_quarantine_recovery.md).

## Single-stage priority assessment

Normal scoring makes one Flash assessment per posting. A valid `priority` (`first`,
`consider`, `low`) and short `reason` completes assessment; only failures or malformed
responses cause bounded retries. There is no automatic Pro review. Legacy `scoring.review`
and `fit_threshold` configuration is accepted but ignored. The provider's normal scoring
model is Flash; an explicit scoring-model override remains supported.

Priority describes review value, independently of other postings. `consider` retains credible
stretches and mixed evidence. Missing information is uncertainty, not confirmed ineligibility.
Results are grouped by priority, then posting date (discovery date fallback) and stable ID.
Saved/applied/rejected status remains independent of assessment.

New results have `fit=NULL`. Historical binary judgments remain stored for reporting, but new priority
results do not count as negative qualifications or enter scrape-cell qualification counters.
The dashboard and `jobs list` filter by priority; legacy fit, reason-type, and assessment-stage
filters are retired. Old dashboard query parameters for those filters are ignored.
The dashboard's Review first control uses priority, not fit. The verdict panel shows the
explanation even without a qualification judgment.

The Market view uses First/Consider/Low priorities from the same profile as the dashboard.
Its priority rate is `(First + Consider) / priority-scored jobs`; historical binary judgments
are excluded. See [the market legacy removal map](market-priority-cleanup.md) for API changes
and the remaining reporting/counter dependencies.

Migration v38 removes the unused `v_job_assessments` compatibility view without changing
stored verdicts or history.

Migration v36 adds durable `assessment_runs` checkpoints, archives replaced final verdicts
in `job_verdict_history`, and lets final assessments take precedence over legacy screening
exclusions while preserving screening history. Unfinished old reviews require a fresh
single-stage assessment; they are never reinterpreted as final priorities.

`findajob score run --limit 100` assesses eligible jobs without a priority, including historical
binary verdicts and screening exclusions. The queue starts with the newest posting date,
falling back to discovery date, with stable ID ordering for ties. Existing jobs qualify
immediately without another scrape. A priority under any profile or schema keeps a job out
of automatic scoring even after a repeat scrape. Duplicate, thin-description, likely closed,
and quarantined jobs remain excluded. Previous verdicts stay active until a successful
assessment archives and replaces them. No profile rewrite occurs.

Completed checkpoints are reusable only with matching profile/rules/schema, full posting
content, provider and model. Saving a profile marks known old hashes stale without reopening
the backlog. Explicit selected-job assessment makes a fresh call and preserves the earlier
final result in history:

```sh
findajob score jobs --job-id 123 --json
findajob jobs list --priority consider --json
findajob score verdict --job-id 123 --priority consider --reason-description 'Credible stretch' --json
```

`--review-only` is a deprecated alias for ordinary selected-job reassessment; it never chooses
Pro. Failed paid attempts retain cumulative usage and costs in checkpoints. Normal provider
outage handling and the existing per-run spending safeguard remain in place. Wallet deltas
are account-wide observations, not precise per-request prices; spending-cap redesign is
outside this change.

For the paired comparison with the old 70% binary prompt, see
[single-stage validation](single_stage_flash_validation.md). The evaluator freezes postings
and profile from a read-only snapshot and writes isolated results under ignored `experiments/`.
Only explicit bounded selections are used when testing writes on the production database copy.

### Off-peak DeepSeek scoring

When `deepseek` is the active LLM provider and `scoring.postpone_deepseek_peak_hours` is
`true` (the default), `findajob score run` postpones batch scoring during DeepSeek's UTC
peak windows (`01:00–04:00` and `06:00–10:00 UTC`, Monday through Friday):

- If invoked during a peak window (by the background scheduler, search-to-score chaining,
  or CLI), `score run` exits cleanly before making model calls, leaving postings in
  `pipeline_state='new'` for the next off-peak run.
- If an in-progress run crosses into a peak window, it finishes persisting and accounting
  for its current bounded batch (`scoring.concurrency`) and stops before starting the next
  batch.
- Interactive user actions (Copilot chat, URL import, single-job scoring/resume generation)
  remain immediate. Set `scoring.postpone_deepseek_peak_hours: false` in `config.local.yaml`
  to disable peak-hour postponement.

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
