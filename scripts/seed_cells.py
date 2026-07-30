"""Populate scrape_cells from roles.yaml.

Re-runnable after every taxonomy edit. Existing cells keep their scrape history, so editing
roles.yaml does not reset coverage; cells no longer in the taxonomy are disabled rather than
deleted, which keeps cell_observations' foreign keys and past analytics auditable.

    uv run python -m scripts.seed_cells [--prune] [--queries-per-family N]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import load_config  # noqa: E402
from backend.database import Database  # noqa: E402
from backend.roles import load_roles  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description="Seed the scrape cell matrix.")
    parser.add_argument("--prune", action="store_true",
                        help="disable cells no longer present in roles.yaml")
    parser.add_argument("--queries-per-family", type=int, default=1,
                        help="search terms per family (default 1; more expands the matrix "
                             "and lengthens the full-cycle time)")
    args = parser.parse_args(argv)

    config = load_config()
    scraper = config.get("scraper", {})
    enabled_sources = tuple(
        name for name, on in (scraper.get("sources") or {}).items() if on
    ) or ("indeed",)

    roles = load_roles()
    problems = roles.validate()
    if problems:
        print("roles.yaml has problems; fix these first:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    specs = roles.cell_specs(
        sources=enabled_sources, queries_per_family=args.queries_per_family
    )

    db = Database()
    try:
        inserted = db.seed_cells(specs)
        disabled = db.prune_cells(specs) if args.prune else 0
        total = len(db.get_cells())

        print(f"sources:        {', '.join(enabled_sources)}")
        print(f"specs planned:  {len(specs)}")
        print(f"cells inserted: {inserted}")
        if args.prune:
            print(f"cells disabled: {disabled}")
        print(f"cells enabled:  {total}")

        by_source = {}
        for cell in db.get_cells():
            by_source[cell.source] = by_source.get(cell.source, 0) + 1
        print(f"by source:      {by_source}")

        budgets = scraper.get("budgets") or {}
        per_day = sum(
            (budgets.get(s, {}).get("searches_per_run", 0)) * 2
            for s in enabled_sources
        )
        if per_day:
            print(f"\nat 2 runs/day ({per_day} cells/day) a full matrix cycle takes "
                  f"~{total / per_day:.1f} days")
            print(f"analytics.min_window_days is "
                  f"{config.get('analytics', {}).get('min_window_days', 30)}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
