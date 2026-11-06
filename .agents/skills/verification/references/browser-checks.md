# Browser checks with an isolated application server

Use this workflow to check changed UI behavior against the real frontend and
backend. It supplements `make verify` when visible application behavior changes.

## Start an isolated server

Run from the repository root after installing dependencies:

```sh
make dev
```

This starts Vite at `http://127.0.0.1:5173` with hot frontend updates, proxies
`/api` to a reloading FastAPI server at `127.0.0.1:8011`, and initializes an
isolated database. No production frontend build is needed. The launcher prints
its temporary application home, clears inherited config/auth/provider variables,
and gives both servers an isolated user home. Keep that path for synthetic data
and diagnostic artifacts; it is retained after shutdown.

For another worktree, choose a different frontend/API port pair:

```sh
make dev DEV_PORT=5174 DEV_API_PORT=8012
```

Or run `python3 scripts/dev.py --port 5174 --api-port 8012` directly.

Run in a long-lived execution session and retain its session handle. Wait for
successful responses from `/` and `/api/meta` on the frontend port before browser
interaction; Vite can become ready before the API. If a port is occupied, choose
another pair rather than stopping an unrelated listener. Either server exiting
stops its companion, so a failed API cannot leave an apparently working preview.

Seed synthetic records relevant to the changed flow using the printed home and
a clean environment, before importing application modules:

```sh
env -i PATH="$PATH" HOME=/tmp/careerradar-ui-example/user \
  FIND_A_JOB_HOME=/tmp/careerradar-ui-example \
  .venv/bin/python -c 'from findajob.core.database import Database; Database().close()'
```

Replace both paths with the actual home. Do not point browser interactions at a
production database or its diagnostic snapshot.

`--lifespan off` prevents startup seeding and the background scheduler from
running scrapes or scoring. Keep automatic updates off in the UI. This setup
does not verify application startup or scheduler behavior; use a purpose-built
isolated test when those are the subject of the change. Flows that invoke AI
need a deterministic mock for the relevant provider rather than live calls.

## Handle sandbox restrictions at the execution boundary

The repository's [Codex config](../../../../.codex/config.toml) enables networking
inside the workspace-write sandbox for this trusted project. Filesystem sandbox
restrictions remain active; the setting also permits outbound networking, not
only localhost. See the [official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
Start a fresh session after changing the config: it cannot change a running
session's policy, and higher-priority runtime restrictions may override it.

Start `make dev` normally inside that sandbox. Do not request escalation for
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
in the temporary home or ignored `test-results/` directory when they help explain
the result.

Close only the named browser session with
`npx --yes @playwright/cli -s=careerradar-check close` and stop the server using its
execution session handle. Preserve diagnostic artifacts if useful; do not kill
unrelated browser sessions or listeners. Report the flows and viewports checked, the
automated verification result, and any remaining failures or unavailable checks.
