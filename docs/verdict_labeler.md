# Verdict labeling workbench

The verdict labeler is a private local web application for creating blinded human labels
without altering production predictions. It uses its own SQLite database and snapshots the
job, candidate profile, and model verdict exactly as they existed at import time.

## Quick start

From a development checkout:

```bash
uv run findajob-labeler init
uv run findajob-labeler import --count 75 --seed 1
uv run findajob-labeler serve
```

Open <http://127.0.0.1:8020>. The server binds only to localhost by default. Use the `A`,
`S`, `P`, and `I` keys for Apply, Stretch, Pass, and Insufficient information; after the
prediction is revealed, press `N` for the next example.

The default files are `data/jobs.db` (opened read-only) and `data/labeler.db`. Override them
with `--source-db` and `--label-db`. To import exact postings instead of a sampled queue:

```bash
uv run findajob-labeler import --job-id 123 --job-id 456
```

The sampled initial queue follows the quality plan: approximately 50% stratified model
rejects, 25% model matches, and 25% contradictory or gap-admitting borderline cases. The
mixture is deterministically shuffled so queue position does not reveal the stratum.

## Consistency review and export

After the first round, create a blinded 10% re-label queue:

```bash
uv run findajob-labeler relabel --percent 10 --seed 1
```

Start the server again with `uv run findajob-labeler serve` and refresh the page.
The UI opens the first queue with pending reviews. You can also select
`relabel-10pct-seed1` from the queue dropdown to open this consistency queue.

After completing the queue, click **Compare labels** in the top bar. The comparison
view reports exact outcome agreement between your first and second human labels,
with disagreements listed first and expanded. Each posting includes both rounds'
outcome, reason, confidence, and notes, plus the original posting and profile.
Pending second-round reviews are excluded so their first labels stay hidden.
Use **Back to queue** to return to labeling.

For each disagreement, choose **Use first label** or **Use second label** under
**Final dataset label**, optionally explain the deciding criterion, then click
**Save final label**. The unresolved count decreases when you save. You can change
the choice later. Agreeing pairs require no action.

Exports use your selected round's outcome, reason, confidence, and notes as the
canonical human label, and include the separate resolution rationale. Both original
annotations remain intact, and the consistency score still reflects their original
agreement. Until resolved, a disagreement uses the first-round label in exports.
Resolve disagreements before freezing the dataset for prompt optimization.

Create a new, immutable dataset export:

```bash
uv run findajob-labeler export --version baseline-v1
```

This writes `data/labeler_exports/baseline-v1/examples.jsonl` plus a manifest containing
record counts and a SHA-256 checksum. Existing export versions are never overwritten.
Examples are assigned to optimization or validation by a stable family hash, keeping
same-company/same-title posting families together. The split selector in the review UI can
manually promote a whole family into the held-out set for curated regression cases.

Both the database and default export directory are ignored by Git because they contain full
profile and posting text.

## Offline prompt evaluation

Click **Evaluate prompts** in the top bar to freeze a dataset and compare the current scorer
with candidate prompt instructions. Resolve all consistency disagreements before using
**Create export**. Existing CLI-created exports registered in the same labeler database also
appear in the frozen dataset selector.

Choose a dataset and split, edit the candidate instructions, and optionally add a factual
candidate-profile boundary. The default instructions require evidence for role-defining
skills and their depth, distinguish secondary tools from core stack requirements, and keep
specialized tenure and allowances for adjacent experience scoped to the evidence. They do
not add candidate-specific claims. Both variants use
the same snapshotted profile and posting, fit threshold, provider, and model. Defaults come
from **`LLM_PROVIDER`** and **`LLM_SCORING_MODEL`** in `.env`, using the application's shared
LLM resolver. For Codex with GPT Sol 6.1, set:

```dotenv
LLM_PROVIDER=codex
LLM_SCORING_MODEL=gpt-6.1-sol
```

Restart the labeler after changing `.env`. The UI displays the resolved defaults; explicit
UI or CLI selections override them for both variants. When these environment variables are
unset, the shared resolver falls back to application configuration/provider defaults.
The baseline rebuilds the **current**
production prompt; it is not the old model verdict captured during labeling.

Use **Validation** for prompt iteration and **Held-out regression** for final comparisons.
The **All** split is exploratory and includes optimization data. Never select a prompt by
its performance on optimization examples or repeatedly tune it against held-out results.

Click **Run comparison** to start provider calls in the background. Progress and saved runs
remain available when you return to the view. Keep the labeler server running; stopping it
interrupts unfinished runs, which can be inspected afterward. Start a new run to retry.
Each eligible example gets a baseline and candidate evaluation, alternating their call order
across examples. The production scorer's bounded retry policy applies to each evaluation.
Scoring failures are retained per example and do not abort the remaining comparisons.

The results show agreement, weighted agreement, false positives/negatives, confusion matrices,
metrics by dataset split/original model reason/human decisive reason, and available token and
cost measurements. Expand a posting to see explanations, exact prompts, and rendered input.

Metric definitions:

- Match maps to human **Apply**, Reject to **Pass**. The current production schema cannot
  predict **Stretch**, so exact three-class agreement exposes that limitation.
- **Insufficient information** is excluded from scoring and accuracy denominators.
- Exact agreement divides correct predictions by all eligible labels. Failures count as
  incorrect. Weighted agreement gives human Pass labels weight 2 and Apply/Stretch weight 1,
  emphasizing false-positive matches.
- False-positive match rate is human Pass / all predicted Matches. The saved report also
  includes the false-positive rate among human Pass cases.
- False-negative rate is predicted Reject / human Apply-or-Stretch cases. Both error rates
  use successfully scored examples; inspect failure counts alongside them.
- Tokens and cost include recorded retry attempts. Missing provider measurements are
  unavailable rather than assumed free; totals and averages identify measurement coverage.

The same runner is available from the CLI:

```bash
uv run findajob-labeler evaluate \
  --dataset data/labeler_exports/baseline-v1 \
  --split validation
```

Use `--candidate-rules-file instructions.txt` and/or `--profile-boundary-file boundary.txt`
to supply UTF-8 candidate changes, `--model` and `--provider` to override both variants'
model/provider defaults from `.env`, and
`--fit-threshold` to change their shared threshold (default 70). Exit status is nonzero
if any evaluation failed.

Runs are saved as JSON under `labeler_evaluations/` beside the labeler database (normally
`data/labeler_evaluations/`). CLI `--output-dir` overrides this location. Each run retains
the dataset manifest/checksum, split, model/provider, schema, source-code checksums, exact
prompts and hashes, original record provenance, per-example predictions/errors, and metrics.
The default evaluation directories are ignored by Git because these artifacts contain
full candidate and posting text. Evaluations never update production job verdicts.
