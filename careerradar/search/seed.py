"""Populate scrape_cells from roles.yaml.

Re-runnable after every taxonomy edit. Existing cells keep their scrape history, so editing
roles.yaml does not reset coverage; cells no longer in the taxonomy are disabled rather than
deleted, which keeps cell_observations' foreign keys and past analytics auditable.

    careerradar search seed-cells [--prune] [--queries-per-family N]
"""

from careerradar.core.config import load_config
from careerradar.core.database import Database
from careerradar.taxonomy.roles import load_roles


def seed_cells(prune: bool = False, queries_per_family: dict[str, int] | int | None = None) -> int:
    config = load_config()
    scraper = config.get("scraper", {})
    enabled_sources = tuple(name for name, on in (scraper.get("sources") or {}).items() if on) or (
        "indeed",
    )

    roles = load_roles()
    problems = roles.validate()
    if problems:
        print("roles.yaml has problems; fix these first:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    specs = roles.cell_specs(sources=enabled_sources, queries_per_family=queries_per_family)

    db = Database()
    try:
        inserted = db.seed_cells(specs)
        disabled = db.prune_cells(specs) if prune else 0
        total = len(db.get_cells())

        print(f"sources:        {', '.join(enabled_sources)}")
        print(f"specs planned:  {len(specs)}")
        print(f"cells inserted: {inserted}")
        if prune:
            print(f"cells disabled: {disabled}")
        print(f"cells enabled:  {total}")

        by_source: dict[str, int] = {}
        for cell in db.get_cells():
            by_source[cell.source] = by_source.get(cell.source, 0) + 1
        print(f"by source:      {by_source}")

        budgets = scraper.get("budgets") or {}
        per_day = sum((budgets.get(s, {}).get("searches_per_run", 0)) * 2 for s in enabled_sources)
        if per_day:
            print(
                f"\nat 2 runs/day ({per_day} cells/day) a full matrix cycle takes "
                f"~{total / per_day:.1f} days"
            )
            print(
                f"analytics.min_window_days is "
                f"{config.get('analytics', {}).get('min_window_days', 30)}"
            )
    finally:
        db.close()
    return 0
