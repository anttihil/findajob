# Completion

- For code, tests, dependencies, or configuration changes, use the repository's
  [verification skill](.agents/skills/verification/SKILL.md).
- Run `make verify` after the final edit. Fix failures within scope and rerun;
  earlier results do not verify later changes. Add regression coverage when behavior changes.
- Report the verification result and any skipped, failing, or unavailable checks.
  If verification is blocked, hand off as blocked rather than claiming completion.
- Do not bypass hooks or weaken checks to obtain a pass. Documentation-only tasks
  need `git diff --check`; the stop hook may still verify an uncached repository.
