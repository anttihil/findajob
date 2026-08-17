import { Link } from "wouter-preact";
import type { FilterQuery } from "../lib/filterQuery";

// Ported from the pagination block in `templates/tabs/dashboard.html`.
export function Pagination({ query, total, hasMore }: { query: FilterQuery; total: number; hasMore: boolean }) {
  if (total <= query.limit) return null;

  return (
    <nav class="feed-pagination">
      {query.offset > 0 && (
        <Link class="text-btn" href={query.page(query.offset - query.limit)}>
          <i class="fa-solid fa-chevron-left"></i> Newer
        </Link>
      )}
      <span class="results-count">
        {query.offset + 1}–{Math.min(query.offset + query.limit, total)} of {total}
      </span>
      {hasMore && (
        <Link class="text-btn" href={query.page(query.offset + query.limit)}>
          Older <i class="fa-solid fa-chevron-right"></i>
        </Link>
      )}
    </nav>
  );
}
