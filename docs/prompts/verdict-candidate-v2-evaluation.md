# Verdict candidate v2: held-out comparison

## Protocol

- Candidate: `verdict-candidate-v2.txt`, unchanged from the selected validation candidate.
- Provider/model: `deepseek` / `deepseek-v4-flash`; fit threshold: 70.
- Dataset: `verdict-v2-held-out`, exported from the local labeler database.
- Dataset checksum: `16ecfeb6d0935112a65beede79e4cee99443c1aba2f43754092b5daa7237229d`.
- Reserved all 60 previously unused optimization cards (53 posting families) before
  inspecting their labels or predictions. No family overlaps the 15 development cards.
- One paired comparison, with the runner's alternating variant order and production
  retry policy. No candidate revisions or best-run selection during this check.
- Review agreement, false-positive matches, and lost human Apply matches; report Stretch
  separately because the production schema cannot predict it.

## Results

Local run: `c8dce2a220664a30876ba6305f97f4c5`.
Full artifact: `data/labeler_evaluations/c8dce2a220664a30876ba6305f97f4c5.json`
(ignored by Git because it contains private profile and posting data).

| Metric | Baseline | Candidate |
| --- | ---: | ---: |
| Exact agreement | 49/60 (81.7%) | 50/60 (83.3%) |
| Pass-weighted agreement | 85% | 89% |
| Human Pass predicted Apply | 4/40 | 1/40 |
| Human Apply predicted Pass | 1/14 | 3/14 |
| Human Stretch predicted Apply / Pass | 3 / 3 | 3 / 3 |
| Scoring failures | 0 | 0 |
| Family-macro exact agreement | 82.5% | 81.1% |

Family-macro agreement is the mean of each family's exact agreement, giving each of
the 53 families equal weight instead of giving repeated postings additional weight.
All six Stretch labels count as incorrect under exact three-class agreement.

## Decision

Do not promote automatically. The candidate corrected three false-positive cards but
lost two previously correct Apply cards. Its small card-level improvement reverses
when families receive equal weight. This is evidence of a stricter decision rule and
a precision/recall tradeoff, rather than a clear general improvement.

Production verdict rules were not changed. These results are now known; if they guide
another prompt revision, this dataset becomes a regression/development set for that
revision, rather than an untouched final acceptance set.
