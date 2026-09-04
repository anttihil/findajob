"""SQLite persistence for the singleton candidate Profile and generated tailored resumes."""

import json
import sqlite3
from typing import Any

from careerradar.core.logger import get_logger
from careerradar.profile.models import (
    DEFAULT_PROFILE_VERSION,
    MASTER_PROFILE_ID,
    MasterEducation,
    MasterProject,
    MasterRole,
    MasterSkillCategory,
    Profile,
    RoleTargeting,
    TailoredResumePayload,
    WorkEligibility,
)

logger = get_logger()


def _get_connection(
    conn: sqlite3.Connection | None,
) -> tuple[sqlite3.Connection, Any, bool]:
    """Resolve active database connection and ownership."""
    if conn is not None:
        return conn, None, False
    from careerradar.core.database import Database

    db = Database()
    return db.conn, db, True


def load_profile(conn: sqlite3.Connection | None = None) -> Profile:
    """Load the singleton Profile from SQLite."""
    connection, db, owned = _get_connection(conn)
    try:
        row = connection.execute(
            """
            SELECT *
              FROM profile
             WHERE id = ?
             LIMIT 1
            """,
            (MASTER_PROFILE_ID,),
        ).fetchone()

        if row is None:
            return Profile()

        r_dict = dict(row)

        # Parse eligibility
        elig_raw = r_dict.get("eligibility_json")
        eligibility = (
            WorkEligibility.model_validate_json(elig_raw) if elig_raw else WorkEligibility()
        )

        # Parse targeting (backward compatibility)
        targ_raw = r_dict.get("targeting_json")
        targeting = RoleTargeting.model_validate_json(targ_raw) if targ_raw else RoleTargeting()

        # Parse dealbreakers
        d_raw = r_dict.get("dealbreakers_json")
        if d_raw:
            try:
                dealbreakers = json.loads(d_raw)
            except (json.JSONDecodeError, ValueError, TypeError):
                dealbreakers = targeting.dealbreakers
        else:
            dealbreakers = targeting.dealbreakers

        # Parse lists
        edu_raw = json.loads(r_dict.get("education_json") or "[]")
        skills_raw = json.loads(r_dict.get("skills_json") or "[]")
        exp_raw = json.loads(r_dict.get("experience_json") or "[]")
        proj_raw = json.loads(r_dict.get("projects_json") or "[]")

        exec_summary = r_dict.get("executive_summary") or r_dict.get("summary_guidance") or ""
        model_guidance = r_dict.get("model_guidance") or ""

        return Profile(
            name=r_dict.get("name") or "",
            email=r_dict.get("email") or "",
            phone=r_dict.get("phone") or "",
            location=r_dict.get("location") or "",
            github=r_dict.get("github") or "",
            linkedin=r_dict.get("linkedin") or "",
            website=r_dict.get("website") or "",
            executive_summary=exec_summary,
            model_guidance=model_guidance,
            dealbreakers=dealbreakers,
            summary_guidance=exec_summary,
            seniority=r_dict.get("seniority") or "Mid / Senior",
            years_experience=r_dict.get("years_experience") or 4.0,
            eligibility=eligibility,
            targeting=targeting,
            experience=[MasterRole(**r) for r in exp_raw],
            projects=[MasterProject(**p) for p in proj_raw],
            skills=[MasterSkillCategory(**s) for s in skills_raw],
            education=[MasterEducation(**e) for e in edu_raw],
        )
    finally:
        if owned and db:
            db.close()


def save_profile(
    profile: Profile,
    conn: sqlite3.Connection | None = None,
    render_prompt: bool = True,
) -> None:
    """Save the singleton Profile to SQLite and update the cached summary text."""
    from careerradar.profile.render import render_profile

    connection, db, owned = _get_connection(conn)
    try:
        summary_text = render_profile(profile) if render_prompt else ""
        eligibility_json = profile.eligibility.model_dump_json()
        targeting_json = profile.targeting.model_dump_json()
        dealbreakers_json = json.dumps(profile.dealbreakers)
        education_json = json.dumps([e.model_dump() for e in profile.education])
        skills_json = json.dumps([s.model_dump() for s in profile.skills])
        experience_json = json.dumps([r.model_dump() for r in profile.experience])
        projects_json = json.dumps([p.model_dump() for p in profile.projects])

        exec_summary = profile.executive_summary or profile.summary_guidance
        model_guidance = profile.model_guidance

        connection.execute(
            """
            INSERT OR REPLACE INTO profile (
                id, updated_at, name, email, phone, location, github, linkedin, website,
                summary_guidance, seniority, years_experience, eligibility_json,
                targeting_json, education_json, skills_json, experience_json, summary_text,
                executive_summary, model_guidance, dealbreakers_json, projects_json
            ) VALUES (
                ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?
            )
            """,
            (
                MASTER_PROFILE_ID,
                profile.name,
                profile.email,
                profile.phone,
                profile.location,
                profile.github,
                profile.linkedin,
                profile.website,
                exec_summary,
                profile.seniority,
                profile.years_experience,
                eligibility_json,
                targeting_json,
                education_json,
                skills_json,
                experience_json,
                summary_text,
                exec_summary,
                model_guidance,
                dealbreakers_json,
                projects_json,
            ),
        )
        if owned:
            connection.commit()
    finally:
        if owned and db:
            db.close()


def load_active(db: Any | None = None) -> tuple[int, Profile, str] | None:
    """Compatibility loader returning (version, profile, summary_text) for scoring."""
    owned = db is None
    database = db
    if database is None:
        from careerradar.core.database import Database

        database = Database()
    try:
        row = database.conn.execute(
            "SELECT summary_text FROM profile WHERE id = ? LIMIT 1",
            (MASTER_PROFILE_ID,),
        ).fetchone()
        prof = load_profile(conn=database.conn)
        summary_text = (row["summary_text"] if row else "") or ""
        if not summary_text:
            from careerradar.profile.render import render_profile

            summary_text = render_profile(prof)
        return (DEFAULT_PROFILE_VERSION, prof, summary_text)
    finally:
        if owned and database:
            database.close()


def load_active_row(db: Any | None = None) -> dict[str, Any] | None:
    """Return raw profile dict for dashboard status views."""
    owned = db is None
    database = db
    if database is None:
        from careerradar.core.database import Database

        database = Database()
    try:
        row = database.conn.execute(
            "SELECT * FROM profile WHERE id = ? LIMIT 1",
            (MASTER_PROFILE_ID,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if owned and database:
            database.close()


def list_versions(_conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Return active profile versions."""
    return [{"version": DEFAULT_PROFILE_VERSION, "is_active": 1, "name": "Current Profile"}]


# --- Generated Resumes Persistence ------------------------------------------------------


def save_tailored_resume(
    job_id: int,
    model: str,
    docx_path: str = "",
    pdf_path: str | None = None,
    resume: TailoredResumePayload | None = None,
    summary: str = "",
    ats_score: int | None = None,
    ats_verdict: str | None = None,
    ats_feedback: str | None = None,
    status: str = "generated",
    typst_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Save a generated tailored resume record linked to a job."""
    connection, db, owned = _get_connection(conn)
    try:
        resume_json = resume.model_dump_json() if resume else "{}"
        cur = connection.execute(
            """
            INSERT INTO generated_resumes (
                job_id, model, created_at, docx_path, pdf_path, resume_json,
                summary, ats_score, ats_verdict, ats_feedback, status, typst_path
            ) VALUES (
                ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
            """,
            (
                job_id,
                model,
                docx_path,
                pdf_path,
                resume_json,
                summary,
                ats_score,
                ats_verdict,
                ats_feedback,
                status,
                typst_path,
            ),
        )
        if owned:
            connection.commit()
        return int(cur.lastrowid or 0)
    finally:
        if owned and db:
            db.close()


def get_latest_tailored_resume(
    job_id: int, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    """Retrieve the most recent tailored resume record for a job."""
    connection, db, owned = _get_connection(conn)
    try:
        row = connection.execute(
            """
            SELECT *
              FROM generated_resumes
             WHERE job_id = ?
             ORDER BY id DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if not row:
            return None
        res = dict(row)
        if res.get("resume_json"):
            try:
                res["resume"] = json.loads(res["resume_json"])
            except json.JSONDecodeError:
                res["resume"] = {}
        return res
    finally:
        if owned and db:
            db.close()


def get_resume_by_id(
    resume_id: int, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    """Retrieve a specific generated resume record by id."""
    connection, db, owned = _get_connection(conn)
    try:
        row = connection.execute(
            """
            SELECT *
              FROM generated_resumes
             WHERE id = ?
            """,
            (resume_id,),
        ).fetchone()
        if not row:
            return None
        res = dict(row)
        if res.get("resume_json"):
            try:
                res["resume"] = json.loads(res["resume_json"])
            except json.JSONDecodeError:
                res["resume"] = {}
        return res
    finally:
        if owned and db:
            db.close()


def update_resume_artifacts(
    resume_id: int,
    typst_path: str,
    pdf_path: str | None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Update artifact paths for an existing generated resume record."""
    connection, db, owned = _get_connection(conn)
    try:
        connection.execute(
            "UPDATE generated_resumes SET typst_path = ?, pdf_path = ? WHERE id = ?",
            (typst_path, pdf_path, resume_id),
        )
        if owned:
            connection.commit()
    finally:
        if owned and db:
            db.close()


def list_tailored_resumes(
    limit: int = 50,
    job_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """List recent tailored resumes with target job details."""
    connection, db, owned = _get_connection(conn)
    try:
        where = "WHERE r.job_id = ?" if job_id is not None else ""
        params: tuple[Any, ...] = (job_id, limit) if job_id is not None else (limit,)
        rows = connection.execute(
            f"""
            SELECT r.*, j.title as job_title, j.company as job_company,
                   j.location as job_location
              FROM generated_resumes r
              LEFT JOIN jobs j ON r.job_id = j.id
             {where}
             ORDER BY r.id DESC
             LIMIT ?
            """,
            params,
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            if d.get("resume_json"):
                try:
                    d["resume"] = json.loads(d["resume_json"])
                except json.JSONDecodeError:
                    d["resume"] = {}
            results.append(d)
        return results
    finally:
        if owned and db:
            db.close()


# Backward compatibility aliases
load_master_profile = load_profile
save_master_profile = save_profile
save_generated_resume = save_tailored_resume
get_latest_resume_for_job = get_latest_tailored_resume
list_generated_resumes = list_tailored_resumes
