from typing import Any

from careerradar.core.database import Database
from careerradar.scoring import repository as scoring_repo


def collect(
    profile_version: int | None = None,
    scale_version: int | None = None,  # noqa: ARG001 - kept for signature compatibility
    db: Database | None = None,
) -> dict[str, Any] | None:
    owned = db is None
    db = db or Database()
    try:
        return scoring_repo.get_verdict_distribution_stats(db.conn, profile_version=profile_version)
    finally:
        if owned:
            db.close()


def render(stats: dict[str, Any] | None, width: int = 40) -> str:
    if stats is None:
        return "No active profile. Build one with:  careerradar profile build"
    if not stats.get("total"):
        return f"No verdicts stored for profile v{stats['profile_version']}."

    total = stats["total"]
    fit_count = stats["fit_count"]
    no_fit_count = stats["no_fit_count"]
    out = []

    out.append(f"profile v{stats['profile_version']} · {total:,} verdicts · ${stats['cost']:.4f}")
    out.append("")
    out.append("fit breakdown:")
    out.append(f"  fit (>= 90% match):  {fit_count:>6}  ({fit_count / total:5.1%})")
    out.append(f"  no fit:              {no_fit_count:>6}  ({no_fit_count / total:5.1%})")
    if stats.get("unscored_fit_count"):
        unscored = stats["unscored_fit_count"]
        out.append(f"  legacy/unscored fit: {unscored:>6}  ({unscored / total:5.1%})")

    out.append("")
    out.append("reason_type distribution:")
    reasons = stats.get("reasons") or {}
    peak = max(reasons.values()) if reasons else 1
    for rtype, count in reasons.items():
        bar = "#" * max(1, round(count / peak * width)) if count else ""
        out.append(f"  {rtype:<16} {count:>6}  ({count / total:5.1%})  {bar}")

    return "\n".join(out)


def run_stats(profile_version: int | None = None, scale_version: int | None = None) -> int:
    print(render(collect(profile_version, scale_version=scale_version)))
    return 0
