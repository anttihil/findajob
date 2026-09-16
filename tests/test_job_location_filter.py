"""Location filtering for the dashboard job feed."""

from __future__ import annotations

from pathlib import Path

from findajob.core.database import Database


def test_location_filter_matches_case_insensitive_substrings(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "jobs.db"))
    try:
        db.conn.executemany(
            """
            INSERT INTO jobs (job_key, title, company, location, country, url, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "helsinki",
                    "Engineer",
                    "North",
                    "Helsinki, Finland",
                    "FI",
                    "https://example.test/1",
                    "unread",
                ),
                (
                    "remote",
                    "Engineer",
                    "Everywhere",
                    "Remote",
                    "US",
                    "https://example.test/2",
                    "unread",
                ),
            ],
        )
        db.conn.commit()

        helsinki = db.query_jobs(location="HELSINKI")["jobs"]
        assert [job["job_key"] for job in helsinki] == ["helsinki"]
        assert [job["job_key"] for job in db.query_jobs(location="mote")["jobs"]] == ["remote"]
    finally:
        db.close()
