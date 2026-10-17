# Source-Scoped Search Queries Plan

## Goal

Allow scheduled search queries to target Indeed, LinkedIn, or both independently. This
supports a narrower, higher-signal Indeed strategy without reducing the broader LinkedIn
search plan.

## Current behavior

`search_queries` stores only `id`, `query`, and `enabled`. When search cells are seeded,
every enabled query is combined with every enabled location and every globally enabled
source. Consequently, editing a query changes both Indeed and LinkedIn.

One-off source-specific searches are already available through:

```bash
findajob search query --source indeed --query "Forward Deployed Engineer" --location "Los Angeles, CA"
```

They should remain unchanged and can be used to test query terms before scheduling them.

## Design

Add a source mapping table rather than a JSON column:

```sql
CREATE TABLE search_query_sources (
    query_id INTEGER NOT NULL REFERENCES search_queries(id) ON DELETE CASCADE,
    source   TEXT NOT NULL,
    PRIMARY KEY (query_id, source)
);
```

The mapping provides normalized source filtering, preserves the existing `search_queries`
identity, and permits new job boards later without another schema change.

Global source configuration remains a master enable/disable switch. A scheduled cell is
created only when both the source is globally enabled and the query is mapped to that source.

## Implementation steps

1. Add a migration that creates `search_query_sources` and backfills every existing query
   to both `indeed` and `linkedin`.

   This is intentionally behavior-preserving: deployment must not silently reduce the
   current search plan. Retain existing query IDs so cell history and market analytics
   continue to join to their original query records.

2. Extend the search-target repository.

   - Return `sources` with each query from `get_queries()`.
   - Accept a source list in add/update operations.
   - Validate sources against supported scheduled boards.
   - Default a missing source list to both sources for backward-compatible callers.

3. Update target-plan generation.

   `TargetPlan.cell_specs()` should use each query's mapped sources, intersected with the
   globally enabled sources, instead of creating the full query × location × source matrix.

4. Reseed and prune cells after source-scope changes.

   Adding Indeed to a query creates its missing Indeed cells. Removing Indeed disables only
   that query's Indeed cells; it must not delete cells or their observation history. Existing
   query-text edits should continue to preserve cell history as they do now.

5. Extend the API, CLI, and dashboard.

   - Add `sources` to target-query create/update payloads and responses.
   - Let the query CLI specify a source scope when creating or editing scheduled queries.
   - Add Indeed and LinkedIn controls to each query in the Targets UI, plus a source filter.
   - Display existing/backfilled queries as enabled for both boards.

6. Make yield analytics source-aware.

   Group query performance by `(source, query)` so the product can evaluate whether a term
   such as `Forward Deployed Engineer` is useful specifically on Indeed rather than only in
   the aggregate.

7. Add tests.

   - Migration backfills legacy queries for both sources.
   - An Indeed-only query generates no LinkedIn cells.
   - A LinkedIn-only query generates no Indeed cells.
   - Removing a source disables only its corresponding cells and preserves observations.
   - A globally disabled source produces no scheduled cells even when mapped to a query.
   - CRUD/API/UI flows preserve and update source scopes correctly.
   - Yield results separate identical query text by source.

## Rollout

1. Deploy the behavior-preserving migration and verify the scheduled-cell count is unchanged.
2. Use one-off Indeed searches to validate candidate terms.
3. Change selected scheduled queries to Indeed-only, initially favoring narrow terms such as:

   - `Forward Deployed Engineer`
   - `Agentic AI Engineer`
   - `Platform Engineer`
   - `Backend Engineer Python`
   - `Full Stack Engineer AI`

4. Keep broader discovery terms on LinkedIn.
5. Review source-query yield after several scheduled runs, then pause or refine low-yield
   Indeed terms.

## Acceptance criteria

- Existing installations retain the same scheduled matrix immediately after migration.
- A user can assign each scheduled query to Indeed, LinkedIn, or both.
- Cell histories are never deleted solely because a source assignment changes.
- Dashboard and analytics clearly report source-specific query performance.
