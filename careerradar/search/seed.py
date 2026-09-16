"""Populate scrape_cells from the configured search queries and locations.

Re-runnable after every edit to the targets. Existing cells keep their scrape history, so
editing targets does not reset coverage; cells no longer in the matrix are disabled rather
than deleted, which keeps cell_observations' foreign keys and past analytics auditable.

    find-a-job search seed-cells [--prune]

Every query declared in search_queries is seeded: the matrix is the whole plan, and what it
lists is what gets searched.
"""

from careerradar.core.config import load_config
from careerradar.core.database import Database
from careerradar.search.targets import load_targets

# Only used for the cycle-length line this command prints. The schedule lives in
# config.yaml (scheduler.search.schedule), defaulting to 4 runs/day.
RUNS_PER_DAY = 4


def seed_cells(prune: bool = False) -> int:
    config = load_config()
    scraper = config.get("scraper", {})
    enabled_sources = tuple(name for name, on in (scraper.get("sources") or {}).items() if on) or (
        "indeed",
    )

    targets = load_targets()
    problems = targets.validate()
    if problems:
        print("Search targets have problems; fix these first:")
        for problem in problems:
            print(f"  - {problem}")
        print("  (Tip: Configure targets via Dashboard or 'find-a-job target')")
        return 1

    specs = targets.cell_specs(sources=enabled_sources)

    db = Database()
    try:
        inserted, updated = db.seed_cells(specs)
        disabled = db.prune_cells(specs) if prune else 0
        total = len(db.get_cells())

        print(f"sources:        {', '.join(enabled_sources)}")
        print(f"specs planned:  {len(specs)}")
        print(f"cells inserted: {inserted}")
        print(f"cells refreshed: {updated}")
        if prune:
            print(f"cells disabled: {disabled}")
        print(f"cells enabled:  {total}")

        by_source: dict[str, int] = {}
        for cell in db.get_cells():
            by_source[cell.source] = by_source.get(cell.source, 0) + 1
        print(f"by source:      {by_source}")

        from careerradar.search.capacity import calculate_capacity

        active_queries = len(targets.queries)
        active_locations = sum(1 for loc in targets.locations.values() if loc.enabled)
        cap = calculate_capacity(active_queries, active_locations, config)
        print(
            f"\nmatrix: {active_queries} queries x {active_locations} locations "
            f"= {cap['search_pairs']} search pairs"
        )
        print(f"status: [{cap['zone'].upper()}] - {cap['message']}")
    finally:
        db.close()
    return 0
