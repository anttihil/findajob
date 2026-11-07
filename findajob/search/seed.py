"""Populate scrape_cells from the configured search queries and locations.

Re-runnable after every edit to the targets. Existing cells keep their scrape history, so
editing targets does not reset coverage; cells no longer in the matrix are disabled rather
than deleted, which keeps cell_observations' foreign keys and past analytics auditable.

    findajob search seed-cells [--prune]

Every query declared in search_queries is seeded: the matrix is the whole plan, and what it
lists is what gets searched.
"""

from findajob.core.config import load_config
from findajob.core.database import Database
from findajob.core.search_schedule import search_schedules
from findajob.search.targets import load_targets


def seed_cells(prune: bool = False) -> int:
    config = load_config()
    enabled_sources = tuple(search_schedules(config))

    targets = load_targets()
    problems = targets.validate()
    if problems:
        print("Search targets have problems; fix these first:")
        for problem in problems:
            print(f"  - {problem}")
        print("  (Tip: Configure targets via Dashboard or 'findajob target')")
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

        from findajob.search.capacity import capacity_for_targets

        active_queries = len(targets.queries)
        locations_in_use = len({loc_id for _, loc_id in targets.search_pairs})
        cap = capacity_for_targets(targets, config)
        print(
            f"\nmatrix: {active_queries} queries, {locations_in_use} locations in use: "
            f"= {cap['search_pairs']} query-location combinations ({cap['total_cells']} cells)"
        )
        for source, estimate in cap["sources"].items():
            print(
                f"{source} sweep: {estimate['cycle_hours']}h "
                f"({estimate['runs_per_day']:g} runs/day)"
            )
        print(f"status: [{cap['zone'].upper()}] - {cap['message']}")
    finally:
        db.close()
    return 0
