---
name: verification
description: Verify careerradar code, tests, dependency, or configuration changes before completing a coding task, and diagnose failed repository checks.
---

# Repository verification

Run from the repository root:

```sh
make verify
```

This runs Python and frontend lint and formatting checks, both type checkers,
pytest, Vitest, the production frontend build, a wheel build/install smoke test,
and Git whitespace checks in order.
It stops on the first failure and never auto-formats source. Install dependencies
with `uv sync --locked` and `npm ci` if needed, using the session's permissions.

For formatting failures, use `uv run ruff format <affected paths>` for Python or
`npm run format` for frontend files, inspect the diff, then rerun `make verify`.
Use focused tests while repairing failures; they do not replace the full command
after the last edit. Preserve unrelated user changes.

`make package-smoke` builds the frontend and checks packaging independently. The
smoke test builds a wheel, syncs locked runtime dependencies into a temporary venv,
installs the wheel, checks dependency consistency, imports the installed web app,
verifies bundled JS/CSS, and runs both CLI entry points with `--help` outside the checkout. Its application
home is temporary; the development environment is preserved. A cold cache may
require network access for the build backend and locked dependencies.

`make verify-browser` also runs the Playwright desktop/mobile tests. Use it for
browser-facing changes once the missing preview fixture referenced by
`playwright.config.ts` is restored; Chromium must be installed. Until then,
report browser verification as blocked, with the missing fixture path.

Successful runs write a local receipt under `test-results/verification/`. The stop
hook reuses it only while repository contents and dependency-install metadata
match. Explicit `make verify` and CI always execute checks. Changes during a run
invalidate it. These receipts are a convenience, not a security boundary.

On hook failure, inspect the referenced log. The hook requests one automatic repair
continuation, then checks again and allows a blocked handoff if still failing.
Report the command, result, and outstanding failures or limitations. Never describe
a blocked or partial run as passing; do not disable checks to finish.
