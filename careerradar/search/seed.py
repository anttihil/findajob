"""Populate scrape_cells from target roles and locations.

Re-runnable after every taxonomy edit. Existing cells keep their scrape history, so editing
targets does not reset coverage; cells no longer in the taxonomy are disabled rather than
deleted, which keeps cell_observations' foreign keys and past analytics auditable.

    careerradar search seed-cells [--prune]

Every query_term declared in target_queries is seeded: the matrix is the whole plan, and
what it lists is what gets searched.
"""

from careerradar.core.config import load_config
from careerradar.core.database import Database
from careerradar.taxonomy.roles import load_roles

# Only used for the cycle-length line this command prints. The schedule lives in
# config.yaml (scheduler.search.schedule), defaulting to 4 runs/day.
RUNS_PER_DAY = 4


def seed_cells(prune: bool = False) -> int:
    config = load_config()
    scraper = config.get("scraper", {})
    enabled_sources = tuple(name for name, on in (scraper.get("sources") or {}).items() if on) or (
        "indeed",
    )

    roles = load_roles()
    problems = roles.validate()
    if problems:
        print("Search targets / role taxonomy has problems; fix these first:")
        for problem in problems:
            print(f"  - {problem}")
        print("  (Tip: Configure targets via Dashboard or 'careerradar target')")
        return 1

    specs = roles.cell_specs(sources=enabled_sources)

    db = Database()
    try:
        inserted, updated = db.seed_cells(specs)
        disabled = db.prune_cells(specs) if prune else 0
        total = len(db.get_cells())

        print(f"sources:        {', '.join(enabled_sources)}")
        print(f"specs planned:  {len(specs)}")
        print(f"cells inserted: {inserted}")
        print(f"cells re-enabled: {updated}")
        if prune:
            print(f"cells disabled: {disabled}")
        print(f"cells enabled:  {total}")

        by_source: dict[str, int] = {}
        for cell in db.get_cells():
            by_source[cell.source] = by_source.get(cell.source, 0) + 1
        print(f"by source:      {by_source}")

        from careerradar.search.capacity import calculate_capacity

        active_queries = sum(len(f.query_terms) for f in roles.families.values() if f.enabled)
        active_locations = sum(1 for loc in roles.locations.values() if loc.enabled)
        cap = calculate_capacity(active_queries, active_locations, config)
        print(
            f"\nmatrix: {active_queries} queries x {active_locations} locations "
            f"= {cap['search_pairs']} search pairs"
        )
        print(f"status: [{cap['zone'].upper()}] - {cap['message']}")
    finally:
        db.close()
    return 0
