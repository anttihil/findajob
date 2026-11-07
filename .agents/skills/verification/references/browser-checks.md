# Browser checks with the development application

Use this workflow to check changed UI behavior against the real frontend and
backend. It supplements `make verify` when visible application behavior changes.

## Start the backend and Vite directly

Run from the repository root after installing dependencies, in two separate
long-lived execution sessions:

```sh
uv run findajob start --dev
```

```sh
npm run dev
```

Vite serves `http://127.0.0.1:5173` with hot frontend updates and proxies `/api`
to the application at `127.0.0.1:8010`. No production frontend build or wrapper
script is needed. `make dev` is shorthand for starting only the backend.
Backend autoreload is optional (`uv run findajob start --dev --reload`); leave it
off for browser checks to avoid an extra reload supervisor process.

Dev mode prints its database and log paths. Data persists at `.dev/data/jobs.db`;
the first start uses a read-only SQLite backup of `prod_jobs.db` when available,
or creates an empty database. Existing dev data is never replaced. Config overrides,
graph checkpoints, status, and resumes also stay under the ignored `.dev/` directory.
Defaults come from the tracked `config.yaml`; dev overrides and credentials go in
`.dev/config/config.local.yaml` and `.dev/config/.env`. Inherited `FIND_A_JOB_*`
settings are cleared before application imports. Other provider credentials remain
available, so mock AI providers for flows that should not make live calls.
Application, Uvicorn, and request logs append to `.dev/dev.log`.

For another worktree, choose a different frontend/API port pair:

```sh
uv run findajob start --dev --port 8012
FIND_A_JOB_API_PROXY=http://127.0.0.1:8012 npm run dev -- --port 5174
```

Retain both execution session handles. Wait for
successful responses from `/` and `/api/meta` on the frontend port before browser
interaction; Vite can become ready before the API. If a port is occupied, choose
another pair rather than stopping an unrelated listener. Vite fails on an occupied
port instead of silently choosing another one. These are independent foreground
servers: if either fails, inspect its output and `.dev/dev.log`, then stop or restart
that session explicitly. A Vite page alone does not establish backend readiness.

When synthetic records are needed, use the dev home before application imports:

```sh
FIND_A_JOB_HOME="$PWD/.dev" FIND_A_JOB_LOG_PATH="$PWD/.dev/dev.log" \
  uv run python -c 'from findajob.core.database import Database; Database().close()'
```

Clear any inherited path overrides when using this snippet. Do not point browser
interactions at a production database or its diagnostic snapshot. Preserve existing
dev records and use distinct synthetic identifiers.

Dev mode uses the normal application lifespan, including startup cell seeding and
live updates. It disables the persisted scheduler preference at every launch so a
production snapshot cannot trigger automatic scraping or scoring. Keep automatic
updates off in the UI unless scheduler behavior is what you are testing.

## Handle sandbox restrictions at the execution boundary

The repository's [Codex config](../../../../.codex/config.toml) enables networking
inside the workspace-write sandbox for this trusted project. Filesystem sandbox
restrictions remain active; the setting also permits outbound networking, not
only localhost. See the [official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
Start a fresh session after changing the config: it cannot change a running
session's policy, and higher-priority runtime restrictions may override it.

Start both direct commands normally inside that sandbox. Do not request escalation for
every server start, enable global full access, or disguise the command as an
already-approved unrelated server. If socket creation/binding is still denied,
report the actual policy restriction and that a fresh session with project
networking is required. Report port conflicts separately from sandbox restrictions. If project networking cannot be enabled, a fixed launcher can
instead be approved once via the execution tool's persistent command-prefix
rule; that fallback runs outside the sandbox and must be described as such.

Use `npx --yes @playwright/cli` with a named browser session. The npm package is
`@playwright/cli`; `playwright-cli` is its executable name and also the name of a
deprecated npm package. Avoid `npx playwright-cli`, which can fetch that deprecated
package. Use the [Playwright CLI skill](../../playwright-cli/SKILL.md) for command
syntax, substituting `npx --yes @playwright/cli` for its `playwright-cli` examples:

```sh
npx --yes @playwright/cli --help
npx --yes @playwright/cli -s=careerradar-check open http://127.0.0.1:5173
npx --yes @playwright/cli -s=careerradar-check snapshot
```

If `open` reports that Chromium is missing, install the version required by the
CLI with `npx --yes @playwright/cli install-browser chromium`, using the session's
permissions, then retry the same named session.

If an apparently successful browser launch is followed by “Browser is not open”
in the next call, the session may not have survived the execution boundary.
Retry in an approved execution context outside the sandbox and use that same
context and session name for subsequent browser commands. Avoid a loop of
reopening browsers that discards the state being tested. This does not require
a persistent browser profile or reinstalling the removed test harness.

If approval is denied or the runtime cannot support the server/browser, report
the browser check as blocked with the actual error. API or unit tests remain
useful evidence but do not establish interactive or visual correctness.

## Check the changed behavior and clean up

Exercise the actual user flow and its observable results: distinct records,
filters, counts, navigation, and persistence as relevant to the change. After an
action, wait for the expected record IDs, labels, or saved state, not just a row
count that the previous screen could also satisfy. Reload when persistence is
part of the behavior.

Inspect screenshots at desktop and narrow widths for layout changes. A page
with no console errors can still have overlapping or inaccessible controls;
report such failures even when DOM assertions pass. Keep screenshots or traces
under `.dev/` or the ignored `test-results/` directory when they help explain
the result.

Close only the named browser session with
`npx --yes @playwright/cli -s=careerradar-check close` and stop **both** servers with
Ctrl+C through their execution session handles. Confirm both sessions exit and
their ports no longer respond. Preserve `.dev/` and diagnostic artifacts; do not kill
unrelated browser sessions or listeners. Report the flows and viewports checked, the
automated verification result, and any remaining failures or unavailable checks.
