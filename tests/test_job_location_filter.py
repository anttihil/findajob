"""Search-location provenance filtering for the dashboard job feed."""

from __future__ import annotations

from pathlib import Path

from findajob.core.database import Database


def test_location_filter_matches_scrape_cell_provenance(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "jobs.db"))
    try:
        db.conn.executemany(
            """
            INSERT INTO scrape_cells
                (id, source, location_id, query, search_label, country, indeed_country,
                 is_remote, distance, created_at)
            VALUES (?, 'indeed', ?, 'Engineer', ?, 'FI', 'finland', 0, 50, '2026-01-01T00:00:00Z')
            """,
            [
                (1, "helsinki", "Helsinki, Finland"),
                (2, "us_remote", "United States"),
            ],
        )
        db.conn.executemany(
            """
            INSERT INTO jobs (
                job_key, title, company, location, country, url, status, scrape_cell_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "helsinki",
                    "Engineer",
                    "North",
                    "Espoo, Finland",
                    "FI",
                    "https://example.test/1",
                    "unread",
                    1,
                ),
                (
                    "remote",
                    "Engineer",
                    "Everywhere",
                    "Remote",
                    "US",
                    "https://example.test/2",
                    "unread",
                    2,
                ),
            ],
        )
        db.conn.commit()

        helsinki = db.query_jobs(location="helsinki")["jobs"]
        assert [job["job_key"] for job in helsinki] == ["helsinki"]
        assert db.query_jobs(location="espoo")["jobs"] == []
    finally:
        db.close()
