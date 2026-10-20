# Find a Job

Find a Job is a personal job-search application. It collects postings from configured job
boards, builds a candidate profile from your resume, scores postings against that profile,
and can generate tailored one-page resumes. The job posting collection can happen on a schedule
or via one-off runs. You can use the app via a web interface or via a CLI.

The application stores postings, profiles, verdicts, search targets, and generated resumes
locally. It is intended for one user and does not require a hosted service.

<img width="1627" height="1165" alt="findajob" src="https://github.com/user-attachments/assets/50f6d5e0-3776-4cbd-ba01-cd3ea6c820f5" />

## Installation

### Standalone installation

The release installer creates an isolated Python environment, installs the prebuilt package
and frontend, initializes application storage, and starts the dashboard. Python 3.10 or newer
is required; Node.js is not required for a standalone install.

From a checkout:

```bash
./install.sh
```

The installer places the `findajob` executable in `~/.local/bin` by default. Make sure that
directory is on your `PATH`, then open <http://127.0.0.1:8010>. The installer may prompt for a
DeepSeek API key; you can instead configure another supported provider later.

An installed copy keeps configuration, data, state, and logs in the operating system's
application directories. Set `FIND_A_JOB_HOME` to use a self-contained directory, or use the
`FIND_A_JOB_*_PATH` variables for individual locations.

### Development checkout

Use this setup when changing the application:

```bash
uv sync
npm ci
npm run build
cp .env.example .env
cp config.local.example.yaml config.local.yaml
uv run findajob init
```

Put provider credentials in `.env` and personal configuration in `config.local.yaml`. The
tracked `config.yaml` supplies defaults. Start the application with:

```bash
uv run findajob start --port 8010
```

Alternatively, `make setup` installs dependencies and `make build` builds the frontend.

### Docker

Docker Compose builds the frontend and Python runtime together:

```bash
cp .env.example .env
cp config.local.example.yaml config.local.yaml
make docker-up
```

The dashboard is then available at <http://localhost:8010>. Compose persists the database,
configuration override, and resumes through the mounted local files.

## Use the CLI

The `findajob` command works without a running web server. In a source checkout, replace
`findajob` in the examples below with `uv run findajob`; with a standalone installation, run
it directly.

Build and inspect the profile used for scoring:

```bash
findajob profile build /path/to/resume.pdf
findajob profile show
# Export the complete profile as a general-purpose resume (PDF or editable Typst)
findajob profile export
findajob profile export --format typst
```

Configure and inspect search targets:

```bash
findajob target list
findajob target add "Platform Engineer,Site Reliability Engineer"
findajob search seed-cells
```

Run the pipeline manually. `run` searches and then scores the resulting backlog:

```bash
findajob run
findajob search run --dry-run
findajob score run --limit 100
```

Useful inspection and application commands include:

```bash
findajob jobs list --pipeline-state scored --limit 25
findajob status
findajob resume generate 123
findajob import https://example.com/job-posting
```

Use `--json` on supported commands such as `jobs list`, `status`, `search query`, and selected
scoring commands when integrating with scripts or other tools. Run `findajob --help` or
`findajob <command> --help` for the complete command reference.

AI agents can use the same CLI to search, inspect postings, request scoring packets, save
reviewed verdicts, and generate tailored resumes. JSON output makes these workflows suitable
for supervised automation while keeping personal data in the local application store.

## Web app and interactive use

### Career memo and resume workspace

In **Resumes → Master Profile & Copilot**, open **Career memo** to import a Markdown or
text achievement library, edit it, and save a revision. The complete source is preserved;
revision history lets you load an older version and restore it as a new revision. Export
downloads the latest Markdown. Concurrent edits are rejected rather than overwriting newer notes.

Choose **AI Copilot** in the memo editor to chat about edits or use **Compact memo**
to remove repetition and application-specific notes while retaining achievements, career direction,
and factual qualifications. The copilot uses your current editor text, including unsaved edits.
Review the complete proposal, ask for refinements, then **Apply to memo editor** and **Save memo
revision**. Discarding a proposal leaves the editor unchanged; saved revisions remain in history.

Run `make verify` before handing off code changes. It checks Python/frontend lint,
Python and frontend formatting, both type checkers, pytest, Vitest, the production
frontend build, a wheel build/install smoke test, and Git whitespace, stopping at
the first failure. It checks formatting without rewriting source; use
`uv run ruff format <paths>` for Python or `npm run format` for frontend formatting.
Install dependencies with `make setup` (`uv sync` and `npm ci`). CI runs the same
verification command after installing locked dependencies.

Codex and Claude use a synchronous stop hook instead of checks after each edit.
Successful verification writes an ignored receipt under `test-results/verification/`;
the hook reuses it only while repository contents and dependency-install metadata
match. Explicit verification always reruns. Failures include a log path and request
one repair continuation; persistent failures permit an explicitly blocked handoff.
Review and trust the changed Codex hook through `/hooks` before relying on it.
If branch protection requires the previous CI jobs, update it to require `verify`.
See [AGENTS.md](AGENTS.md) and the
[verification skill](.agents/skills/verification/SKILL.md) for completion guidance.

Prettier is pinned locally; `npm run format:check` checks frontend source, browser
tests, and frontend configuration. `make package-smoke` builds the frontend and
tests packaging independently: it builds a wheel, installs it with locked runtime
dependencies in a temporary environment, checks dependency consistency, imports the
web app, verifies bundled frontend assets, and runs both installed CLIs with `--help`
from outside the checkout.
It uses a temporary application home and leaves the development environment intact.
A cold cache may need network access for packaging dependencies.

Suggested addition: browser tests in CI after restoring their missing preview fixture.

Run `make verify-browser` to include the memo copilot's Chromium checks on desktop and mobile.
These currently require restoring the missing
`.agents/skills/browser-verification/scripts/serve_preview.py` fixture referenced by
`playwright.config.ts`; until then browser verification is blocked.
Install the browser once with `npx playwright install chromium` after `npm install`.
The tests build the frontend and start an isolated FastAPI server with synthetic data and
mocked AI responses; they exercise the real editor, chat API, revision persistence, recovery,
and error handling. Screenshots and failure traces go in `test-results/browser`.

The master profile remains the structured baseline for job scoring. Tailored resume generation
uses both the profile and the full memo, including its corrections and factual qualifications.
Inputs exceeding the 90,000-byte context budget are explicitly rejected, never silently truncated.

Open **Build & interview** from a job, or **Open workspace** from a tailored resume. The workspace
keeps every draft, its source snapshot, evidence citations, grounding review, recruiter-style
review score, and interview history. AI review scores are editorial judgments, not employer ATS
predictions. Answers are saved as dated additions to the memo; choose **Revise from latest
evidence** to continue. Important unanswered questions pause automatic revisions when a supported
draft is available. Qualification gaps remain explicit rather than being filled with invented facts.

Compare drafts side by side, inspect their numbered source snapshots, and select a supported
draft for export. Automatic generation retains the highest-scoring grounded, printable candidate
within its bounded retry loop. PDF compilation and an actual one-page check are required before
export. Historical drafts remain available when the memo changes, but selecting an old draft for
a new export requires regeneration against the current evidence. If generation fails, saved drafts
remain in the workspace; an abandoned running operation can be recovered after its one-hour lease.

Memo revisions, drafts, and interviews live in the application SQLite database and are included
when that database is backed up. The same memo-backed workflow also powers CLI resume generation.

`findajob start` serves the web dashboard and, when enabled in configuration, runs the
background scheduler. The dashboard is the convenient interactive interface for reviewing
the profile, editing search targets and settings, browsing and filtering postings, inspecting
verdicts, importing jobs, and generating resumes. Use the **Sync Now** action for a manual
search-and-score pass.

Automatic scheduling is disabled by default. Enable it from the **Automatic updates**
control in the dashboard; the choice is stored in the local application database. Schedule
times, scrape budgets, and other operational limits remain configuration settings.

The CLI is the alternative for terminal-only and automated workflows. `search`, `score`,
`resume`, `import`, and inspection commands operate directly on the local data; they do not
need the dashboard to be running. LLM providers may be API-based or authenticated CLI-based
providers. Use `findajob llm status`, `findajob llm test`, and `findajob llm auth <provider>`
to inspect or authenticate providers.

### Private verdict labeling

The separate `findajob-labeler` application creates blinded Apply / Stretch / Pass /
Insufficient-information labels for scorer evaluation. It reads the production database
without writing to it, stores
immutable job/profile/verdict snapshots in `labeler.db`, supports deterministic family-level
dataset splits, manually curated held-out cases, and consistency re-labeling, and exports
versioned JSONL datasets. See
[the verdict labeler guide](docs/verdict_labeler.md) for setup and keyboard shortcuts.

## Architecture

The application is a Python package with a Preact frontend:

```text
search targets -> search workers -> SQLite postings (new)
                                      |
                                      v
candidate resume -> profile builder -> scoring workers -> verdicts (scored)
                                      |
                                      v
                              tailored resume renderer

FastAPI + Preact dashboard <-> the same SQLite data and configuration
```

- `findajob/search` plans target cells and retrieves/normalizes job-board results.
- `findajob/profile` extracts and versions the candidate profile from a resume.
- `findajob/scoring` sends postings and the active profile to the configured LLM and stores
  verdicts with their profile and prompt provenance.
- `findajob/profile/renderer.py` generates tailored resumes and validates their layout.
- `findajob/market` provides aggregate demand, coverage, and skill-gap analysis.
- `findajob/web` contains the FastAPI application and the built React dashboard.
- `findajob/core` provides configuration, paths, SQLite access, migrations, scheduling, and
  pipeline coordination.

Search and scoring are separate stages connected by posting state. This lets either stage be
run independently, while `findajob run` and the dashboard's sync action chain them together.
