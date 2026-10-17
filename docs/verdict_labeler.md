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
