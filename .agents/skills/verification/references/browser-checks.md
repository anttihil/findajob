# Browser checks with an isolated application server

Use this workflow to check changed UI behavior against the real frontend and
backend. It supplements `make verify`; it does not recreate a browser-test
harness or depend on a preview script.

## Start an isolated server

Run from the repository root. Use the current frontend build (`make verify`
already builds it); otherwise run `npm run build` first.

Create a temporary application home and initialize its database before starting
the server. Start with a clean child environment so inherited database, config,
authentication, and credential variables cannot override the temporary home:

```sh
task_preview_home=$(mktemp -d /tmp/careerradar-ui-XXXXXX)
env -i PATH="$PATH" FIND_A_JOB_HOME="$task_preview_home" \
  .venv/bin/python -c 'from findajob.core.database import Database; Database().close()'
```

Record the absolute temporary-home path. Seed synthetic records relevant to the
changed flow using the same environment, before importing application modules.
Keep that path identical across seeding, server startup, and any retry. Do not
point browser interactions at a production database or its diagnostic snapshot.

```sh
env -i PATH="$PATH" FIND_A_JOB_HOME="$task_preview_home" \
  .venv/bin/python -m uvicorn findajob.web.app:app \
  --host 127.0.0.1 --port 8011 --lifespan off
```

Run the server in a long-lived execution session and retain its session handle.
Separate tool calls may not preserve shell variables: substitute the recorded
absolute home path when starting or retrying in a new call. Wait for a successful
HTTP response from `/api/meta` before interacting with the page.

`--lifespan off` prevents startup seeding and the background scheduler from
running scrapes or scoring. Keep automatic updates off in the UI. This setup
does not verify application startup or scheduler behavior; use a purpose-built
isolated test when those are the subject of the change. Flows that invoke AI
need a deterministic mock for the relevant provider rather than live calls.

## Handle sandbox restrictions at the execution boundary

If localhost binding fails with a sandbox or permission error, retry the same
isolated server command once outside the sandbox through the execution tool's
normal escalation mechanism. Preserve the temporary home and loopback-only
binding. Use existing approval when applicable; request only the permission the
tool requires. Do not disable sandboxing globally or change application code to
work around the execution environment.

A bind failure alone does not prove sandbox restrictions. If the port is already
in use, choose another local port and update the browser URL. Do not stop an
unrelated listener. If the approved retry also fails, diagnose that failure
instead of repeatedly escalating or changing ports without evidence.

Use a named browser session through the
[Playwright CLI skill](../../playwright-cli/SKILL.md), for example:

```sh
playwright-cli -s=careerradar-check open http://127.0.0.1:8011
```

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

Close only the named browser session and stop the server using its execution
session handle. Preserve diagnostic artifacts if useful; do not kill unrelated
browser sessions or listeners. Report the flows and viewports checked, the
automated verification result, and any remaining failures or unavailable checks.
