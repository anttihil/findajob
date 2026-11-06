---
name: verification
description: Verify actual careerradar application, test, dependency, build, or verification-tooling changes and diagnose failed checks. Skip read-only tasks and prose or agent-instruction edits alone; task completion is not a trigger.
---

# Repository verification

## Decide whether verification applies

Inspect `git status --short`, the staged and unstaged diff, and relevant untracked
files. Run the full suite only when actual changes affect the app or its checks:
application code and packaged resources, tests, dependencies/lockfiles,
build/deployment configuration, or verification tooling. Treat deletions and
renames as changes. Preserve unrelated user changes.

Read-only questions, reviews with no edits, planning, and finishing a turn do not
trigger verification. Prose and agent/skill instructions alone need only
`git diff --check` and `git diff --cached --check`. Markdown resources inside
`findajob/` can affect the app and still require verification. If the user
explicitly asks for verification, run it regardless of changes.

## Verify changed inputs

Run from the repository root after the last relevant edit:

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

For changes to visible behavior, also exercise the affected flow in a browser.
Read [Browser checks](references/browser-checks.md) for an isolated local server
and the sandbox fallback. Run browser commands with `npx --yes @playwright/cli`
and use the available [Playwright CLI skill](../playwright-cli/SKILL.md) for command
syntax. Its `playwright-cli` examples name the executable; substitute
`npx --yes @playwright/cli` to select the current npm package rather than the
deprecated `playwright-cli` package. No repository Playwright test harness or
preview fixture is required. Distinguish browser checks from API and
unit tests, and report visual failures even when automated checks pass.

Successful runs write a local receipt under `test-results/verification/`. The stop
hook first checks whitespace and whether staged, unstaged, or untracked changes
affect verification inputs. Clean and prose-only checkouts do not run the suite,
even without a receipt. Otherwise, the hook reuses success while the relevant changed files match.
It hashes staged, unstaged, and untracked files, including deletions, without
scanning unchanged files or installed dependencies. Later prose edits do not
invalidate it.
Explicit `make verify` and CI always execute checks. Relevant changes during a run
invalidate it. These receipts are a convenience, not a security boundary.

On hook failure, inspect the referenced log. The hook requests one automatic repair
continuation, then checks again and allows a blocked handoff if still failing.
Report the command, result, and outstanding failures or limitations. Never describe
a blocked or partial run as passing; do not disable checks to finish.
