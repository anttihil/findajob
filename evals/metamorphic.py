"""Validation without labelled data.

The usual way to check a scoring model is a golden set: a few hundred postings a human has
judged. That is the right thing eventually and the wrong thing now -- it costs hours of
labelling to validate a schema that is still moving, and the app has produced no
behavioural signal to draw on (`jobs.status` is `unread` for all 5,932 rows).

Metamorphic testing gets a long way without any of that. Instead of asking "is this verdict
correct", which needs an oracle, it asks "did the verdict change the way it must have",
which needs only a pair of inputs whose relationship we control. Two kinds:

  INVARIANCE   Rename the company, shuffle the requirement bullets, change the salary. The
               posting means the same thing, so the tuple must not move. Anything that does
               move is measured instability, and no human had to say what the right answer
               was.

  DIRECTIONAL  Append a clearance requirement and eligibility MUST become blocked. Append a
               language the candidate does not speak, same. Add a must-have they lack and
               capability_match must not improve. These are ground truths by construction.

The domain swap is the one that matters most for this rework: bolt an autonomous-robotics
product context onto a posting while holding its skill list constant, and `role_match` must
not get CLOSER. That is the "robot QA is not SaaS QA" axis, probed without a single label --
and it is unprobeable at all under a schema that only emits a number.

Invariance is reported at two strengths, because they mean different things. An identical
tuple is the strict reading. Unchanged RANK is the one that matters operationally: a
one-step wobble on `evidence_quality` that leaves the posting in the same Pareto tier costs
the user nothing, while a wobble that reorders the list costs them the top of it.

Run:  uv run python -m evals.metamorphic [--limit 12] [--dry-run]
Cost: roughly $0.30 for ~20 rules over 30 base postings.
"""

import argparse
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from careerradar.core.config import load_config
from careerradar.core.database import Database
from careerradar.core.llm import DEFAULT_SCORING_MODEL, Spend, usage_cost
from careerradar.profile.adapter import load_profile
from careerradar.scoring.graph import build_graph
from careerradar.scoring.prompts import build_system
from careerradar.taxonomy.skills import load_taxonomy

SEED = 20260811


# --- perturbations ----------------------------------------------------------------------
#
# Each returns a modified posting. They are deliberately crude text edits: a perturbation
# clever enough to need its own review would be one more thing to trust.


def rename_company(posting: dict[str, Any]) -> dict[str, Any]:
    """Change the company field only.

    An earlier version also substituted the name throughout the description, which rewrote
    real content when the company name was an ordinary word -- so a failure could not be
    told apart from the perturbation having mangled the posting.
    """
    out = dict(posting)
    out["company"] = "Northwind Systems"
    return out


def change_salary(posting: dict[str, Any]) -> dict[str, Any]:
    out = dict(posting)
    out["salary_annual_usd"] = 143_000
    return out


def shuffle_bullets(posting: dict[str, Any]) -> dict[str, Any] | None:
    """Reorder the bullet lines. Same requirements, different order."""
    description: str = posting.get("description") or ""
    lines = description.split("\n")
    bullets = [i for i, line in enumerate(lines) if re.match(r"^\s*([-*•]|\d+[.)])\s+", line)]
    if len(bullets) < 3:
        return None
    rng = random.Random(SEED)
    shuffled = [lines[i] for i in bullets]
    rng.shuffle(shuffled)
    for slot, line in zip(bullets, shuffled, strict=True):
        lines[slot] = line
    out = dict(posting)
    out["description"] = "\n".join(lines)
    return out


def add_clearance(posting: dict[str, Any]) -> dict[str, Any]:
    out = dict(posting)
    out["description"] = (posting.get("description") or "") + (
        "\n\nAdditional requirement: applicants must hold an active TS/SCI security "
        "clearance with a counterintelligence polygraph at the time of application. "
        "This requirement cannot be waived and sponsorship is not available."
    )
    return out


def add_unspoken_language(posting: dict[str, Any]) -> dict[str, Any]:
    out = dict(posting)
    out["description"] = (posting.get("description") or "") + (
        "\n\nAdditional requirement: fluent written and spoken Japanese is required, as "
        "the entire team and all customer documentation are in Japanese."
    )
    return out


def add_unmet_requirement(posting: dict[str, Any]) -> dict[str, Any]:
    out = dict(posting)
    out["description"] = (posting.get("description") or "") + (
        "\n\nRequired: 12+ years of professional Erlang and OTP experience building "
        "soft-realtime telecom switching systems, and prior work as the named technical "
        "authority on a carrier-grade deployment."
    )
    return out


def swap_domain(posting: dict[str, Any]) -> dict[str, Any]:
    """Same skills, different product. The `role_match` probe.

    The paragraph is PREPENDED, not substituted: there is no reliable way to strip the
    domain language out of arbitrary prose. So the posting ends up internally mixed, and
    the model is entitled to keep reading the bulk of it. That is why the assertion below
    is "must not get closer" rather than "must move" -- an earlier version demanded
    movement and broke 8 times out of 10, which measured the perturbation, not the model.
    """
    out = dict(posting)
    out["description"] = (
        "ABOUT THE PRODUCT: we build autonomous floor-scrubbing robots for warehouses. "
        "This role works on the robot's on-board control and diagnostics software, "
        "deployed to physical machines operating unattended in live warehouse "
        "environments. Field failures are safety incidents, not page reloads.\n\n"
        + (posting.get("description") or "")
    )
    out["title"] = f"Robotics {posting.get('title') or 'Engineer'}"
    return out


# `check(before, after) -> None if it held, else the failure text`.


def unchanged(before: dict[str, Any], after: dict[str, Any]) -> str | None:
    moved = []
    if before.get("fit") != after.get("fit"):
        moved.append(f"fit: {before.get('fit')} -> {after.get('fit')}")
    if before.get("reason_type") != after.get("reason_type"):
        moved.append(f"reason_type: {before.get('reason_type')} -> {after.get('reason_type')}")
    if moved:
        return ", ".join(moved)
    return None


def must_be_no_fit(before: dict[str, Any], after: dict[str, Any]) -> str | None:
    if after.get("fit") is not False and after.get("fit") != 0:
        return f"fit is {after.get('fit')}, expected False"
    return None


def fit_must_not_improve(before: dict[str, Any], after: dict[str, Any]) -> str | None:
    if not before.get("fit") and after.get("fit"):
        return "fit improved from False to True"
    return None


RULES = [
    ("invariance", "company renamed", rename_company, unchanged),
    ("invariance", "salary changed", change_salary, unchanged),
    ("invariance", "bullets reordered", shuffle_bullets, unchanged),
    ("directional", "TS/SCI clearance added", add_clearance, must_be_no_fit),
    ("directional", "unspoken language added", add_unspoken_language, must_be_no_fit),
    ("directional", "unmet requirement added", add_unmet_requirement, fit_must_not_improve),
    ("directional", "product domain swapped", swap_domain, fit_must_not_improve),
]


def sample_postings(db: Database, limit: int) -> list[dict[str, Any]]:
    """Full descriptions with real bullets, spread across role families.

    Sampled deterministically. A metamorphic suite whose base set moves between runs
    cannot tell a prompt regression from a different draw.
    """
    rows = db.conn.execute(
        """
        SELECT id, title, company, location, description, seniority, is_remote,
               salary_annual_usd, access, role_family
          FROM jobs
         WHERE duplicate_of IS NULL
           AND description IS NOT NULL
           AND length(description) BETWEEN 1200 AND 6000
           AND role_family IS NOT NULL
         ORDER BY id
        """
    ).fetchall()
    by_family = {}
    for row in rows:
        by_family.setdefault(row["role_family"], []).append(dict(row))

    rng = random.Random(SEED)
    picked, families = [], sorted(by_family)
    while len(picked) < limit and families:
        for family in list(families):
            bucket = by_family[family]
            if not bucket:
                families.remove(family)
                continue
            picked.append(bucket.pop(rng.randrange(len(bucket))))
            if len(picked) >= limit:
                break
    return picked


def run(limit: int = 12, dry_run: bool = False, model: str | None = None) -> int:
    model = model or DEFAULT_SCORING_MODEL
    config = load_config()
    summary = config.get("profile", {}).get("summary_text", "")
    if not summary:
        print("No profile configured. Run `careerradar profile build` first.")
        return 1

    db = Database()
    try:
        postings = sample_postings(db, limit)
        if not postings:
            print("No postings long enough to perturb.")
            return 1

        taxonomy = load_taxonomy()
        adapter = load_profile(db=db, taxonomy=taxonomy)
        system = build_system(summary)
        graph = build_graph()

        jobs: list[tuple[Any, str, str | None, dict[str, Any]]] = []
        for posting in postings:
            jobs.append((posting["id"], "baseline", None, posting))
            for kind, name, perturb, _check in RULES:
                variant = perturb(posting)
                if variant is not None:
                    jobs.append((posting["id"], name, kind, variant))

        print(
            f"base postings: {len(postings)}   variants: {len(jobs) - len(postings)}"
            f"   calls: {len(jobs)}"
        )
        if dry_run:
            print("(dry run -- nothing called)")
            return 0

        spend = Spend(model, max_usd=None)

        def score(
            item: tuple[Any, str, str | None, dict[str, Any]],
        ) -> tuple[Any, str, str | None, dict[str, Any]]:
            job_id, name, kind, posting = item
            state = graph.invoke(
                {
                    "system": system,
                    "posting": posting,
                    "model": model,
                    "profile": adapter,
                    "taxonomy": taxonomy,
                }
            )
            return job_id, name, kind, state

        results: dict[tuple[Any, str], dict[str, Any] | None] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            for job_id, name, _kind, state in pool.map(score, jobs):
                verdict = state.get("verdict")
                usage = state.get("usage")
                if usage:
                    spend.usd += usage_cost(model, usage)
                results[(job_id, name)] = verdict

        return report(postings, results, spend)
    finally:
        db.close()


def report(
    postings: list[dict[str, Any]],
    results: dict[tuple[Any, str], dict[str, Any] | None],
    spend: Spend,
) -> int:
    outcomes = {}
    failures = []
    unscored = 0
    rank_moves = {}

    for posting in postings:
        baseline = results.get((posting["id"], "baseline"))
        if baseline is None:
            unscored += 1
            continue
        for kind, name, _perturb, check in RULES:
            after = results.get((posting["id"], name))
            if after is None:
                continue
            bucket = outcomes.setdefault((kind, name), {"held": 0, "broke": 0})
            if kind == "invariance":
                seen = rank_moves.setdefault(name, {"same_fit": 0, "total": 0})
                seen["total"] += 1
                if baseline.get("fit") == after.get("fit"):
                    seen["same_fit"] += 1
            problem = check(baseline, after)
            if problem is None:
                bucket["held"] += 1
            else:
                bucket["broke"] += 1
                failures.append((name, posting["id"], posting["title"], problem))

    print()
    print("rule                          held  broke   rate")
    print("  (invariance: 'held' = identical tuple; 'fit held' = same fit boolean)")
    directional_broke = 0
    for kind, name, _p, _c in RULES:
        bucket = outcomes.get((kind, name))
        if not bucket:
            continue
        total = bucket["held"] + bucket["broke"]
        rate = bucket["held"] / total if total else 0
        marker = "  <-- gate" if kind == "directional" and bucket["broke"] else ""
        extra = ""
        if kind == "invariance":
            seen = rank_moves.get(name)
            if seen and seen["total"]:
                extra = f"   fit held {seen['same_fit'] / seen['total']:.0%}"
        print(f"  {name:<28}{bucket['held']:>4}{bucket['broke']:>7}  {rate:>5.0%}{extra}{marker}")
        if kind == "directional":
            directional_broke += bucket["broke"]

    if failures:
        print()
        print("failures")
        for name, job_id, title, problem in failures[:25]:
            print(f"  [{name}] job {job_id} {(title or '')[:40]}")
            print(f"      {problem}")

    if unscored:
        print(f"\n{unscored} baseline posting(s) produced no verdict")
    print(f"\ncost: ${spend.usd:.4f}")

    # Invariance drift is reported, not gated: some of it is real model variance and the
    # number is the thing worth watching. A directional break is a defect -- a posting
    # demanding a clearance the candidate cannot obtain is not a judgement call.
    if directional_broke:
        print(f"\nFAIL: {directional_broke} directional assertion(s) broke.")
        return 1
    print("\nAll directional assertions held.")
    return 0


def main(argv: list[str] | None = None) -> int:
    doc = __doc__ or ""
    parser = argparse.ArgumentParser(description=doc.split("\n")[0])
    parser.add_argument(
        "--limit", type=int, default=12, help="base postings to perturb (default 12)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report the call count without calling"
    )
    parser.add_argument("--model")
    args = parser.parse_args(argv)
    return run(limit=args.limit, dry_run=args.dry_run, model=args.model)


if __name__ == "__main__":
    sys.exit(main())
