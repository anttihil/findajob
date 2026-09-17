# Find a Job

Find a Job is a personal job-search application. It collects postings from configured job
boards, builds a candidate profile from your resume, scores postings against that profile,
and can generate tailored one-page resumes. The job posting collection can happen on a schedule
or via one-off runs. You can use the app via a web interface or via a CLI.

The application stores postings, profiles, verdicts, search targets, and generated resumes
locally. It is intended for one user and does not require a hosted service.

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
