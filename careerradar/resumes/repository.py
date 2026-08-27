"""Database persistence for Master Resume Profile and Generated Tailored Resumes."""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from careerradar.core.database import Database
from careerradar.resumes.models import (
    MasterEducation,
    MasterRole,
    MasterSkillCategory,
    ResumeMasterProfile,
    TailoredResumePayload,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_master_profile(conn: sqlite3.Connection | None = None) -> ResumeMasterProfile:
    """Load the singleton Master Resume Profile from SQLite."""
    owned = conn is None
    db = None if conn else Database()
    connection = conn or db.conn  # type: ignore[union-attr]
    try:
        row = connection.execute(
            """
            SELECT name, email, phone, location, github, linkedin, website,
                   summary_guidance, education_json, skills_json, experience_json
              FROM resume_master_profile
             ORDER BY id DESC LIMIT 1
            """
        ).fetchone()

        if row is None:
            return ResumeMasterProfile()

        edu_raw = json.loads(row["education_json"] or "[]")
        skills_raw = json.loads(row["skills_json"] or "[]")
        exp_raw = json.loads(row["experience_json"] or "[]")

        return ResumeMasterProfile(
            name=row["name"] or "",
            email=row["email"] or "",
            phone=row["phone"] or "",
            location=row["location"] or "",
            github=row["github"] or "",
            linkedin=row["linkedin"] or "",
            website=row["website"] or "",
            summary_guidance=row["summary_guidance"] or "",
            education=[MasterEducation.model_validate(e) for e in edu_raw],
            skills=[MasterSkillCategory.model_validate(s) for s in skills_raw],
            experience=[MasterRole.model_validate(r) for r in exp_raw],
        )
    finally:
        if owned and db:
            db.close()


def save_master_profile(
    profile: ResumeMasterProfile, conn: sqlite3.Connection | None = None
) -> None:
    """Update or insert the Master Resume Profile in SQLite."""
    owned = conn is None
    db = None if conn else Database()
    connection = conn or db.conn  # type: ignore[union-attr]
    try:
        now = _now()
        edu_json = json.dumps([e.model_dump() for e in profile.education])
        skills_json = json.dumps([s.model_dump() for s in profile.skills])
        exp_json = json.dumps([r.model_dump() for r in profile.experience])

        count = connection.execute("SELECT COUNT(*) FROM resume_master_profile").fetchone()[0]
        if count == 0:
            connection.execute(
                """
                INSERT INTO resume_master_profile
                    (updated_at, name, email, phone, location, github, linkedin, website,
                     summary_guidance, education_json, skills_json, experience_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    profile.name,
                    profile.email,
                    profile.phone,
                    profile.location,
                    profile.github,
                    profile.linkedin,
                    profile.website,
                    profile.summary_guidance,
                    edu_json,
                    skills_json,
                    exp_json,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE resume_master_profile
                   SET updated_at = ?,
                       name = ?,
                       email = ?,
                       phone = ?,
                       location = ?,
                       github = ?,
                       linkedin = ?,
                       website = ?,
                       summary_guidance = ?,
                       education_json = ?,
                       skills_json = ?,
                       experience_json = ?
                """,
                (
                    now,
                    profile.name,
                    profile.email,
                    profile.phone,
                    profile.location,
                    profile.github,
                    profile.linkedin,
                    profile.website,
                    profile.summary_guidance,
                    edu_json,
                    skills_json,
                    exp_json,
                ),
            )
        connection.commit()
    finally:
        if owned and db:
            db.close()


def save_generated_resume(
    job_id: int,
    model: str,
    docx_path: str,
    pdf_path: str | None,
    payload: TailoredResumePayload,
    summary: str,
    ats_score: int | None = None,
    ats_verdict: str | None = None,
    ats_feedback: str | None = None,
    profile_version: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Persist a generated resume run into generated_resumes."""
    owned = conn is None
    db = None if conn else Database()
    connection = conn or db.conn  # type: ignore[union-attr]
    try:
        now = _now()
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO generated_resumes
                (job_id, profile_version, model, created_at, docx_path, pdf_path,
                 resume_json, summary, ats_score, ats_verdict, ats_feedback, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'generated')
            """,
            (
                job_id,
                profile_version,
                model,
                now,
                docx_path,
                pdf_path,
                payload.model_dump_json(),
                summary,
                ats_score,
                ats_verdict,
                ats_feedback,
            ),
        )
        connection.commit()
        return cursor.lastrowid or 0
    finally:
        if owned and db:
            db.close()


def get_resume_by_id(
    resume_id: int, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    """Fetch a single generated resume record by id."""
    owned = conn is None
    db = None if conn else Database()
    connection = conn or db.conn  # type: ignore[union-attr]
    try:
        row = connection.execute(
            """
            SELECT r.id, r.job_id, r.profile_version, r.model, r.created_at,
                   r.docx_path, r.pdf_path, r.resume_json, r.summary,
                   r.ats_score, r.ats_verdict, r.ats_feedback, r.status,
                   j.title AS job_title, j.company AS job_company, j.location AS job_location
              FROM generated_resumes r
              JOIN jobs j ON j.id = r.job_id
             WHERE r.id = ?
            """,
            (resume_id,),
        ).fetchone()

        if row is None:
            return None
        rec = dict(row)
        rec["resume"] = json.loads(rec.pop("resume_json"))
        return rec
    finally:
        if owned and db:
            db.close()


def get_latest_resume_for_job(
    job_id: int, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    """Fetch the most recent generated resume for a specific job."""
    owned = conn is None
    db = None if conn else Database()
    connection = conn or db.conn  # type: ignore[union-attr]
    try:
        row = connection.execute(
            """
            SELECT r.id, r.job_id, r.profile_version, r.model, r.created_at,
                   r.docx_path, r.pdf_path, r.resume_json, r.summary,
                   r.ats_score, r.ats_verdict, r.ats_feedback, r.status,
                   j.title AS job_title, j.company AS job_company, j.location AS job_location
              FROM generated_resumes r
              JOIN jobs j ON j.id = r.job_id
             WHERE r.job_id = ?
             ORDER BY r.id DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()

        if row is None:
            return None
        rec = dict(row)
        rec["resume"] = json.loads(rec.pop("resume_json"))
        return rec
    finally:
        if owned and db:
            db.close()


def list_generated_resumes(
    limit: int = 50, offset: int = 0, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    """List all generated resumes with pagination."""
    owned = conn is None
    db = None if conn else Database()
    connection = conn or db.conn  # type: ignore[union-attr]
    try:
        rows = connection.execute(
            """
            SELECT r.id, r.job_id, r.profile_version, r.model, r.created_at,
                   r.docx_path, r.pdf_path, r.summary, r.ats_score, r.ats_verdict,
                   r.status, j.title AS job_title, j.company AS job_company,
                   j.location AS job_location, j.status AS job_status
              FROM generated_resumes r
              JOIN jobs j ON j.id = r.job_id
             ORDER BY r.id DESC
             LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

        return [dict(r) for r in rows]
    finally:
        if owned and db:
            db.close()
