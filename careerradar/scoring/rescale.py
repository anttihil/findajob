"""Recompute the score from stored ordinals. No API calls.

This is the payoff for storing the tuple rather than the number. Tuning the projection in
`scale.py` -- moving a grid cell, changing what a thin posting costs -- used to mean
re-scoring the corpus at a few dollars and an hour; now it is a table scan. Which in turn
is why the projection can stay honest about being invented: nothing is riding on getting
it right the first time.

Prints the band migration before writing, because a scale change that moves 3,000 postings
between bands is a different decision from one that moves 30, and the number is the only
thing that distinguishes them.
"""

from typing import Any

from careerradar.core.database import Database
from careerradar.scoring import rubric, scale


def rescale(
    profile_version: int | None = None, dry_run: bool = False, db: Database | None = None
) -> int:
    owned = db is None
    db = db or Database()
    try:
        params: list[Any] = []
        where = "WHERE scale_version >= 1"
        if profile_version is not None:
            where += " AND profile_version = ?"
            params.append(profile_version)

        rows = db.conn.execute(
            f"SELECT id, job_id, fit_score, verdict, scale_version, "
            f"{', '.join(rubric.DIMENSIONS)} FROM job_verdicts {where}",
            params,
        ).fetchall()

        if not rows:
            skipped = db.conn.execute(
                "SELECT COUNT(*) FROM job_verdicts WHERE COALESCE(scale_version, 0) = 0"
            ).fetchone()[0]
            print("No verdicts carry ordinals yet.")
            if skipped:
                print(
                    f"{skipped:,} verdict(s) are at scale 0 -- the model emitted those "
                    "numbers directly, so there is nothing to recompute from."
                )
                print("Re-score them with:  careerradar score run")
            return 0

        migration: dict[tuple[str, str], int] = {}
        changed = 0
        updates: list[tuple[int, str, int, int, int, int]] = []
        for row in rows:
            ordinals = {name: row[name] for name in rubric.DIMENSIONS}
            if any(value is None for value in ordinals.values()):
                continue
            projected = scale.project(ordinals)
            if (
                projected["fit_score"] != row["fit_score"]
                or projected["scale_version"] != row["scale_version"]
            ):
                changed += 1
            if projected["verdict"] != row["verdict"]:
                cell = (row["verdict"], projected["verdict"])
                migration[cell] = migration.get(cell, 0) + 1
            updates.append(
                (
                    projected["fit_score"],
                    projected["verdict"],
                    projected["pareto_tier"],
                    projected["scale_version"],
                    row["job_id"],
                    row["id"],
                )
            )

        print(f"verdicts with ordinals: {len(updates):,}")
        print(f"score changes:          {changed:,}")
        if migration:
            print()
            print("band migration")
            for (before, after), n in sorted(migration.items(), key=lambda kv: -kv[1]):
                print(f"  {before:<16} -> {after:<16} {n:>6,}")
        else:
            print("no band changes")

        if dry_run:
            print("\n(dry run -- nothing written)")
            return 0

        for fit, verdict, tier, version, job_id, verdict_id in updates:
            db.conn.execute(
                "UPDATE job_verdicts SET fit_score = ?, verdict = ?, pareto_tier = ?, "
                "scale_version = ? WHERE id = ?",
                (fit, verdict, tier, version, verdict_id),
            )
            # `jobs.fit_score` is a denormalized copy for sorting without a join; it has to
            # move with the verdict or the two disagree.
            db.conn.execute("UPDATE jobs SET fit_score = ? WHERE id = ?", (fit, job_id))
        db.conn.commit()
        print(f"\nrewritten at scale v{scale.SCALE_VERSION}")
        return 0
    finally:
        if owned:
            db.close()
