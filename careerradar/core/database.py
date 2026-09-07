"""Database connection management and repository facade."""

import sqlite3
from typing import TYPE_CHECKING, Any, ClassVar

from careerradar.core import job_repository
from careerradar.core.job_repository import (
    DATE_POSTED_WINDOWS,
    FEED_FROM,
    FEED_ORDER_BY,
    JSON_COLUMNS,
    LIST_OMITTED_JOB_COLUMNS,
    LIVENESS_CASE,
    VERDICT_DETAIL_COLUMNS,
    VERDICT_LIST_COLUMNS,
    fuzzy_job_search,
)
from careerradar.core.paths import DB_PATH
from careerradar.scoring import repository as scoring_repo
from careerradar.search import repository as search_repo

if TYPE_CHECKING:
    from careerradar.search.scheduler import CellState


class Database:
    """Core database connection manager and repository facade."""

    _LIVENESS_CASE: ClassVar[str] = LIVENESS_CASE
    _FROM: ClassVar[str] = FEED_FROM
    _VERDICT_LIST_COLUMNS: ClassVar[tuple[str, ...]] = VERDICT_LIST_COLUMNS
    _VERDICT_DETAIL_COLUMNS: ClassVar[tuple[str, ...]] = VERDICT_DETAIL_COLUMNS
    _LIST_OMITTED_JOB_COLUMNS: ClassVar[frozenset[str]] = LIST_OMITTED_JOB_COLUMNS
    _JSON_COLUMNS: ClassVar[tuple[str, ...]] = JSON_COLUMNS
    DATE_POSTED_WINDOWS: ClassVar[dict[str, float]] = DATE_POSTED_WINDOWS
    _FEED_ORDER_BY: ClassVar[str] = FEED_ORDER_BY
    POSTING_COLUMNS: ClassVar[list[str]] = search_repo.POSTING_COLUMNS

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or DB_PATH
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("fuzzy_search", 7, fuzzy_job_search)
        self._jobs_columns: list[str] | None = None
        self.create_tables()

    def create_tables(self) -> None:
        """Bring the schema up to date via the versioned migration runner."""
        from careerradar.core.migrations import migrate

        migrate(self.conn)

    # =====================================================================================
    # Job Feed Queries (delegating to core.job_repository)
    # =====================================================================================

    def _select_columns(self, detail: bool) -> str:
        cols_sql, self._jobs_columns = job_repository.select_columns(
            self.conn, detail, self._jobs_columns
        )
        return cols_sql

    def _feed_filters(self, **kwargs: Any) -> tuple[str, list[Any]]:
        return job_repository.feed_filters(**kwargs)

    def query_jobs(
        self,
        status: str | None = None,
        country: str | None = None,
        role_family: str | None = None,
        seniority: str | None = None,
        source: str | None = None,
        is_remote: bool | None = None,
        has_salary: bool | None = None,
        include_duplicates: bool = False,
        min_score: int | None = None,
        pipeline_state: str | None = None,
        liveness: str | None = None,
        job_id: int | None = None,
        fit: bool | None = None,
        reason_type: str | None = None,
        date_posted: str | None = None,
        q: str | None = None,
        limit: int = 200,
        offset: int = 0,
        detail: bool = False,
    ) -> dict[str, Any]:
        result, self._jobs_columns = job_repository.query_jobs(
            conn=self.conn,
            status=status,
            country=country,
            role_family=role_family,
            seniority=seniority,
            source=source,
            is_remote=is_remote,
            has_salary=has_salary,
            include_duplicates=include_duplicates,
            min_score=min_score,
            pipeline_state=pipeline_state,
            liveness=liveness,
            job_id=job_id,
            fit=fit,
            reason_type=reason_type,
            date_posted=date_posted,
            q=q,
            limit=limit,
            offset=offset,
            detail=detail,
            cached_columns=self._jobs_columns,
        )
        return result

    def job_ids_for(
        self,
        status: str | None = None,
        country: str | None = None,
        role_family: str | None = None,
        seniority: str | None = None,
        source: str | None = None,
        is_remote: bool | None = None,
        has_salary: bool | None = None,
        include_duplicates: bool = False,
        min_score: int | None = None,
        pipeline_state: str | None = None,
        liveness: str | None = None,
        job_id: int | None = None,
        fit: bool | None = None,
        reason_type: str | None = None,
        date_posted: str | None = None,
        q: str | None = None,
        limit: int = 200,
        offset: int = 0,
        detail: bool | None = None,
    ) -> list[int]:
        return job_repository.job_ids_for(
            conn=self.conn,
            db_path=self.db_path,
            status=status,
            country=country,
            role_family=role_family,
            seniority=seniority,
            source=source,
            is_remote=is_remote,
            has_salary=has_salary,
            include_duplicates=include_duplicates,
            min_score=min_score,
            pipeline_state=pipeline_state,
            liveness=liveness,
            job_id=job_id,
            fit=fit,
            reason_type=reason_type,
            date_posted=date_posted,
            q=q,
            limit=limit,
            offset=offset,
            detail=detail,
        )

    def update_job_status(self, job_id: int, status: str) -> bool:
        return job_repository.update_job_status(self.conn, job_id, status)

    def get_stats(self) -> dict[str, Any]:
        return job_repository.get_stats(self.conn, self.db_path)

    def status_counts(self) -> dict[str, int]:
        return job_repository.status_counts(self.conn)

    def _compute_stats(self) -> dict[str, Any]:
        return job_repository.compute_stats(self.conn)

    # =====================================================================================
    # Observability & Scoring (delegating to scoring.repository)
    # =====================================================================================

    def get_observability_stats(self) -> dict[str, Any]:
        return scoring_repo.get_observability_stats(self.conn)

    def query_observability_verdicts(
        self,
        q: str | None = None,
        fit: str | None = None,
        reason_type: str | None = None,
        model: str | None = None,
        sort: str = "tokens_out_desc",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        return scoring_repo.query_observability_verdicts(
            self.conn,
            q=q,
            fit=fit,
            reason_type=reason_type,
            model=model,
            sort=sort,
            limit=limit,
            offset=offset,
        )

    # =====================================================================================
    # Scrape cells (delegating to search.repository)
    # =====================================================================================

    def seed_cells(self, specs: list[dict[str, Any]]) -> tuple[int, int]:
        return search_repo.seed_cells(self.conn, specs)

    def prune_cells(self, specs: list[dict[str, Any]]) -> int:
        return search_repo.prune_cells(self.conn, specs)

    def get_cells(self, source: str | None = None, enabled_only: bool = True) -> list["CellState"]:
        return search_repo.get_cells(self.conn, source=source, enabled_only=enabled_only)

    def record_cell_attempt(
        self,
        cell_id: int,
        observed_at: str,
        hours_old: int | None,
        requested: int,
        returned: int = 0,
        new_unique: int = 0,
        saturated: int = 0,
        status: str = "ok",
        error: str | None = None,
        backoff_until: str | None = None,
        ewma: float | None = None,
    ) -> None:
        search_repo.record_cell_attempt(
            self.conn,
            cell_id=cell_id,
            observed_at=observed_at,
            hours_old=hours_old,
            requested=requested,
            returned=returned,
            new_unique=new_unique,
            saturated=saturated,
            status=status,
            error=error,
            backoff_until=backoff_until,
            ewma=ewma,
        )

    def update_cell_quality(self, cell_id: int, fit_score: float) -> None:
        search_repo.update_cell_quality(self.conn, cell_id, fit_score)

    # =====================================================================================
    # Source circuit-breaker state (delegating to search.repository)
    # =====================================================================================

    def get_source_backoff(self, source: str) -> str | None:
        return search_repo.get_source_backoff(self.conn, source)

    def get_source_trips(self, source: str) -> int:
        return search_repo.get_source_trips(self.conn, source)

    def set_source_backoff(
        self, source: str, until: str, reason: str | None = None, escalate: bool = False
    ) -> None:
        search_repo.set_source_backoff(
            self.conn, source=source, until=until, reason=reason, escalate=escalate
        )

    def reset_source_trips(self, source: str) -> None:
        search_repo.reset_source_trips(self.conn, source)

    # =====================================================================================
    # Sync runs and cell observations (delegating to search.repository)
    # =====================================================================================

    def start_sync_run(
        self,
        mode: str,
        taxonomy_hash: str | None = None,
        plan_hash: str | None = None,
    ) -> int | None:
        return search_repo.start_sync_run(
            self.conn, mode=mode, taxonomy_hash=taxonomy_hash, plan_hash=plan_hash
        )

    def finish_sync_run(self, run_id: int, status: str, **counters: Any) -> None:
        search_repo.finish_sync_run(self.conn, run_id, status, **counters)

    def record_observation(
        self,
        run_id: int | None,
        task: dict[str, Any],
        observed_at: str,
        returned: int = 0,
        new_unique: int = 0,
        saturated: int = 0,
        descriptions_full: int = 0,
        status: str = "ok",
        error: str | BaseException | None = None,
        duration_ms: int | None = None,
        requests_made: int | None = None,
    ) -> None:
        search_repo.record_observation(
            self.conn,
            run_id=run_id,
            task=task,
            observed_at=observed_at,
            returned=returned,
            new_unique=new_unique,
            saturated=saturated,
            descriptions_full=descriptions_full,
            status=status,
            error=error,
            duration_ms=duration_ms,
            requests_made=requests_made,
        )

    # =====================================================================================
    # Enriched posting upsert & deduplication (delegating to search.repository)
    # =====================================================================================

    def upsert_posting(
        self,
        posting: dict[str, Any],
        run_id: int | None = None,
        taxonomy_hash: str | None = None,
    ) -> tuple[int | None, bool]:
        return search_repo.upsert_posting(
            self.conn, posting=posting, run_id=run_id, taxonomy_hash=taxonomy_hash
        )

    def find_duplicate(self, content_hash: str | None, exclude_id: int | None = None) -> int | None:
        return search_repo.find_duplicate(self.conn, content_hash, exclude_id=exclude_id)

    def mark_duplicate(self, job_id: int, canonical_id: int) -> None:
        search_repo.mark_duplicate(self.conn, job_id, canonical_id)

    def replace_job_skills(self, job_id: int, skills: dict[str, dict[str, Any]]) -> None:
        search_repo.replace_job_skills(self.conn, job_id, skills)

    def close(self) -> None:
        self.conn.close()
