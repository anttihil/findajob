# DSPy-assisted job-verdict quality plan

## Purpose

Improve the accuracy of CareerRadar's job-fit verdicts while preserving the
model's holistic judgment of the candidate and the job. The system must avoid
two failure modes:

1. treating adjacent experience as evidence of a specialized capability; and
2. replacing contextual assessment with a brittle, weighted keyword score.

The initial motivating case is a robotics-perception posting requiring C++,
ROS/ROS2, perception, sensor fusion, and field deployment. Robot UI ownership
and SLAM-pose data handling are relevant context, but are not by themselves
evidence of those specialized requirements.

This plan uses DSPy as an *offline* prompt-program optimization and evaluation
tool. It does not require every production job to be evaluated by multiple
models or by a deterministic arithmetic scorer.

## Current observations

- The production scorer returns only `fit`, `reason_type`, and
  `reason_description`. A positive result is therefore not auditable at the
  requirement level.
- The active profile correctly contains valuable robotics-adjacent experience,
  but its positioning guidance emphasizes that experience without stating a
  concise boundary between UI/telemetry work and robotics-perception work.
- The scoring rules say secondary and niche tools may be learned. This is good
  for normal full-stack roles, but needs a clearer distinction between a
  learnable tool and a role-defining specialization.
- Existing `score verdict`/review persistence replaces the model verdict for
  the job/profile pair. It is not appropriate as a source of evaluation labels,
  because it destroys the prediction that the label is meant to assess.

## Principles

1. **Holistic judgment remains the decision-maker.** A model considers the
   candidate and role as whole narratives, rather than adding up skills.
2. **Evidence makes judgment inspectable.** For a small number of role-defining
   requirements, the model must distinguish direct evidence, adjacent evidence,
   and no evidence.
3. **No encyclopedic negative-skills list.** Add only concise, high-value scope
   boundaries where the candidate's adjacent experience is repeatedly confused
   with a different specialization.
4. **Optimize against human choices, not proxy arithmetic.** The quality metric
   comes from curated labels such as whether the candidate would apply, not a
   count of matching keywords.
5. **Spend additional runtime tokens only when measurement justifies them.**
   Begin with a one-call scorer; add a conditional critic only if it produces a
   measured precision improvement.

## Target verdict program

### Phase 1: one-call holistic verdict with a compact evidence audit

Expand the structured output, while still making one inference call per job:

```json
{
  "outcome": "match | stretch | reject",
  "reason_type": "match | skills | domain | ...",
  "reason_description": "concise human explanation",
  "role_defining_requirements": [
    {
      "requirement": "C++ and ROS/ROS2 production development",
      "job_evidence": "short source-grounded description",
      "candidate_evidence": ["quoted or referenced profile evidence"],
      "assessment": "direct | adjacent | not_evidenced"
    }
  ],
  "unresolved_concern": "optional concise concern"
}
```

Constraints:

- List at most three role-defining requirements, not every technology in the
  advertisement.
- Candidate evidence must point to supplied profile facts. The model may not
  infer an unstated skill from nearby terminology.
- The model still makes the final contextual outcome. `adjacent` does not
  automatically mean reject.
- Explicitly ask it to reconcile any `adjacent`/`not_evidenced` requirement
  with a positive decision.

This is structured self-auditing, not deterministic scoring. It adds completion
tokens but no second profile-sized request.

### Profile/rubric correction

Add a compact boundary statement to the scoring profile, tuned only to observed
confusions. For example:

> Robot UI ownership, robot telemetry, and SLAM-pose smoothing are adjacent
> experience; they are not direct evidence of production C++/ROS, perception
> algorithms, sensor fusion, controls, or on-robot deployment.

Also clarify in the rubric that an explicitly required language/framework can
be learnable *only when it is not central to the role's primary specialization*.
The model must decide specialization contextually; no universal C++ or ROS veto
is intended.

### Phase 2: conditional challenge pass (only if warranted)

If a phase-1 result is positive and its own evidence audit has an unresolved
role-defining `adjacent` or `not_evidenced` requirement, invoke a short critic:

> Make the strongest case that this positive verdict is wrong. Reconcile the
> candidate's direct evidence, adjacent evidence, and the job's primary
> specialization. Return a revised holistic outcome and concise explanation.

The critic receives the initial verdict and audit, rather than regenerating a
long analysis. It may affirm the result. The routing condition decides whether
to spend a second call; it does not decide fit.

## Separate labeling application

Build a private, standalone local web app rather than adding this workflow to
the primary CareerRadar app. Ordinary users need a job-search product, not an
evaluation workbench.

### Data boundary

```text
prod_jobs.db (read-only source)
        |
        |  import selected postings as immutable snapshots
        v
labeler.db
  examples       job/profile/model-verdict snapshot
  annotations    human outcomes, reasons, confidence, notes
  queue state    sampling and review progress
  dataset split  train / validation / held-out regression
        |
        v
versioned JSONL exports for DSPy and evaluation
```

Do not foreign-key across the SQLite databases. Store `source_job_id` and source
database/profile/rubric metadata for traceability, while retaining the exact
description, profile text, and model prediction that were shown at label time.
Snapshots prevent a later scrape refresh, profile edit, or scoring change from
silently changing an example.

Keep `labeler.db` and exported datasets containing full profile/job text out of
version control because they may contain personal information.

### Fast labeling interaction

The reviewer sees the posting and a concise candidate-profile panel. Hide the
model verdict by default to avoid anchoring. Label with keyboard shortcuts:

- **Apply** — would actively pursue this job.
- **Stretch** — worth a closer look despite meaningful gaps.
- **Pass** — would not pursue it.
- **Insufficient information** — description/profile evidence is inadequate.

For `Stretch` and `Pass`, offer an optional one-click decisive reason:

- eligibility/dealbreaker;
- role specialization;
- seniority/scope;
- role or domain mismatch;
- location/compensation;
- personal preference; or
- other.

Reveal the model verdict after labeling and record agreement/disagreement. A
short optional note handles novel patterns without making every review slow.

### Sampling queues

Use recent, live, deduplicated jobs. Do not uniformly sample the complete
history.

Initial false-negative audit queue:

- 50% model rejects stratified across `skills`, `tech_stack`, `domain`, and
  `seniority`;
- 25% model matches, to establish false-positive baseline; and
- 25% contradictory or borderline cases, such as near-duplicate postings with
  different verdicts or positive verdicts admitting a major gap.

Label 50–75 jobs first. Re-label a blinded 10% sample later to measure reviewer
consistency. Use the resulting data to refine labels and sampling before trying
to optimize prompts.

## Dataset and metrics

Create immutable, deterministic splits by example family rather than randomly
splitting individual duplicates. Related or near-duplicate jobs must remain in
the same split.

- **Optimization set:** used by DSPy to search instructions and, later, demos.
- **Validation set:** used to choose an optimizer configuration.
- **Held-out regression set:** manually curated representative cases that must
  not influence prompt optimization. It should include C++/ROS perception
  rejects, valid robotics-UI/platform matches, autonomy/data-role boundary
  cases, and other high-risk specializations such as security, ML research, and
  low-level systems.

The primary metric is agreement with the human outcome, with false-positive
`match` verdicts weighted more heavily than false negatives. Report, at a
minimum:

- weighted outcome agreement;
- false-positive match rate;
- false-negative rate (human Apply/Stretch, model Reject);
- confusion matrix for Apply / Stretch / Pass;
- metrics stratified by model reason type and specialization; and
- token/cost per production evaluation.

The `Stretch` class is important: it represents genuine contextual uncertainty
without forcing a hard binary decision or concealing opportunities.

## DSPy experimentation

### Starting point

First express the existing one-call scorer as a DSPy module/signature with the
candidate profile, posting, and structured verdict fields. Optimize
instructions only (zero-shot) against the human-label metric.

This gives the main benefit of DSPy—systematic prompt iteration—without adding
few-shot examples to every production request. Optimization calls are offline;
the deployed scorer remains one call per job.

### Optimizer progression

1. With a small labeled set (roughly ten high-quality examples), test
   `BootstrapFewShot` to establish whether demonstrations help.
2. With a larger, representative set, compare instruction-only MIPROv2 against
   the baseline and a few-shot variant.
3. Use few-shot prompts only if their held-out quality gain exceeds their
   additional production token cost.

DSPy optimizer selection is empirical. Do not promote a compiled program merely
because it improves the examples used to construct it.

### Experiment logging

For every baseline and compiled program, retain:

- source prompt/program version and profile/rubric version;
- optimizer configuration, model, seed, and run cost;
- exact data split identifiers;
- aggregate metrics and per-example predictions;
- disagreements with the held-out regression set; and
- serialized compiled program/instructions.

## Incremental delivery order

1. Build the standalone labeler and snapshot/import/export path.
2. Create the first 50–75 blind human labels and freeze an initial regression
   subset.
3. Add the concise profile boundary and core-versus-specialization rubric
   wording to a candidate scorer branch.
4. Benchmark baseline versus revised one-call prompt on held-out labels.
5. Add the compact evidence audit to the one-call output and benchmark it.
6. Apply DSPy to optimize the one-call program; deploy only when it wins on
   held-out human labels and remains within token budget.
7. Measure how often positive results retain unresolved core gaps. Only then
   prototype and evaluate the conditional second-pass critic.
8. Consider a five-level user-facing presentation (`reject`, `low`, `stretch`,
   `match`, `strong`) after outcome accuracy is established; it is not a
   substitute for accurate contextual assessment.

## Non-goals

- A universal negative-skill taxonomy.
- A deterministic requirement-points formula as the final verdict.
- Automatically training on unreviewed model verdicts.
- Replacing the main application’s normal save/reject/apply actions with
  evaluation labels.
- Enabling a second model call for every posting before evidence shows that it
  pays for itself.
