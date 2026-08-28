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


def master_profile_to_scoring_profile(
    master: ResumeMasterProfile,
) -> Any:
    """Convert a ResumeMasterProfile into a canonical Profile for the scoring worker."""
    from careerradar.profile.models import Constraints, Preferences, Profile, Skill

    skills_map: dict[str, Skill] = {}

    for sr in master.skill_ratings:
        if isinstance(sr, dict) and "key" in sr:
            skills_map[sr["key"]] = Skill(
                key=sr["key"],
                label=sr.get("label") or sr["key"],
                level=int(sr.get("level", 2)),
                evidence=sr.get("evidence", ""),
                recency=sr.get("recency"),
            )
        elif isinstance(sr, Skill):
            skills_map[sr.key] = sr

    for cat in master.skills:
        for skill_name in cat.skills:
            clean_name = skill_name.strip()
            if not clean_name:
                continue
            key = clean_name.lower().replace(" ", "_").replace("-", "_").replace(".", "_")
            if key not in skills_map:
                skills_map[key] = Skill(
                    key=key,
                    label=clean_name,
                    level=2,
                    evidence="",
                )

    extracted_projects = []
    for role in master.experience:
        for proj in role.projects:
            bullets_text = " ".join(proj.bullets[:2])
            desc = f"{role.title} at {role.company}: {proj.name}"
            if proj.heading:
                desc += f" - {proj.heading}"
            if bullets_text:
                desc += f" ({bullets_text})"
            extracted_projects.append(desc[:200])

    constraints = Constraints(
        work_authorization=master.citizenship,
        locations=master.locations,
        willing_to_relocate=master.willing_to_relocate,
        comp_floor_usd=master.comp_floor_usd,
        languages=["English"],
    )

    preferences = Preferences(
        role_families=master.target_roles,
        industries=master.target_industries,
        work_mode=", ".join(master.work_modes) if master.work_modes else None,
    )

    return Profile(
        bio=master.summary_guidance or "Software engineer and platform builder.",
        years_experience=master.years_experience or 4.5,
        seniority=master.seniority or "Mid / Senior",
        skills=list(skills_map.values()),
        projects=extracted_projects,
        strengths=master.strengths,
        weaknesses=master.weaknesses,
        constraints=constraints,
        preferences=preferences,
        non_negotiables=master.dealbreakers,
        red_flags=[],
    )


def load_master_profile(conn: sqlite3.Connection | None = None) -> ResumeMasterProfile:
    """Load the singleton Master Resume Profile from SQLite."""
    owned = conn is None
    db = None if conn else Database()
    connection = conn or db.conn  # type: ignore[union-attr]
    try:
        row = connection.execute(
            """
            SELECT *
              FROM resume_master_profile
             ORDER BY id DESC LIMIT 1
            """
        ).fetchone()

        if row is None:
            return ResumeMasterProfile()

        r_dict = dict(row)
        edu_raw = json.loads(r_dict.get("education_json") or "[]")
        skills_raw = json.loads(r_dict.get("skills_json") or "[]")
        exp_raw = json.loads(r_dict.get("experience_json") or "[]")

        citizenship = json.loads(r_dict.get("citizenship_json") or '["Authorized to work in US"]')
        locations = json.loads(r_dict.get("locations_json") or '["Remote"]')
        target_roles = json.loads(
            r_dict.get("target_roles_json")
            or '["Software Engineer", "Platform Engineer", "Full-Stack Engineer"]'
        )
        work_modes = json.loads(r_dict.get("work_modes_json") or '["remote", "hybrid", "onsite"]')
        target_industries = json.loads(
            r_dict.get("target_industries_json")
            or '["Cloud Infrastructure", "Developer Tools", "AI / ML Applications"]'
        )
        dealbreakers = json.loads(
            r_dict.get("dealbreakers_json")
            or '["24-hour on-call site reliability rotations", "No remote flexibility"]'
        )
        strengths = json.loads(r_dict.get("strengths_json") or "[]")
        weaknesses = json.loads(r_dict.get("weaknesses_json") or "[]")
        skill_ratings = json.loads(r_dict.get("skill_ratings_json") or "[]")

        return ResumeMasterProfile(
            name=r_dict.get("name") or "",
            email=r_dict.get("email") or "",
            phone=r_dict.get("phone") or "",
            location=r_dict.get("location") or "",
            github=r_dict.get("github") or "",
            linkedin=r_dict.get("linkedin") or "",
            website=r_dict.get("website") or "",
            summary_guidance=r_dict.get("summary_guidance") or "",
            seniority=r_dict.get("seniority") or "Mid / Senior",
            years_experience=float(r_dict.get("years_experience") or 4.5),
            citizenship=citizenship,
            locations=locations,
            willing_to_relocate=bool(r_dict.get("willing_to_relocate", 1)),
            comp_floor_usd=r_dict.get("comp_floor_usd"),
            target_roles=target_roles,
            work_modes=work_modes,
            target_industries=target_industries,
            dealbreakers=dealbreakers,
            strengths=strengths,
            weaknesses=weaknesses,
            education=[MasterEducation.model_validate(e) for e in edu_raw],
            skills=[MasterSkillCategory.model_validate(s) for s in skills_raw],
            skill_ratings=skill_ratings,
            experience=[MasterRole.model_validate(r) for r in exp_raw],
            raw_achievements_md=r_dict.get("raw_achievements_md"),
        )
    finally:
        if owned and db:
            db.close()


def save_master_profile(
    profile: ResumeMasterProfile,
    conn: sqlite3.Connection | None = None,
    sync_scoring: bool = True,
) -> None:
    """Update or insert the Master Resume Profile in SQLite, and sync scoring profile."""
    owned = conn is None
    db = None if conn else Database()
    connection = conn or db.conn  # type: ignore[union-attr]
    try:
        now = _now()
        edu_json = json.dumps([e.model_dump() for e in profile.education])
        skills_json = json.dumps([s.model_dump() for s in profile.skills])
        exp_json = json.dumps([r.model_dump() for r in profile.experience])

        citizenship_json = json.dumps(profile.citizenship)
        locations_json = json.dumps(profile.locations)
        target_roles_json = json.dumps(profile.target_roles)
        work_modes_json = json.dumps(profile.work_modes)
        target_industries_json = json.dumps(profile.target_industries)
        dealbreakers_json = json.dumps(profile.dealbreakers)
        strengths_json = json.dumps(profile.strengths)
        weaknesses_json = json.dumps(profile.weaknesses)
        skill_ratings_json = json.dumps(profile.skill_ratings)

        count = connection.execute("SELECT COUNT(*) FROM resume_master_profile").fetchone()[0]
        if count == 0:
            connection.execute(
                """
                INSERT INTO resume_master_profile
                    (updated_at, name, email, phone, location, github, linkedin, website,
                     summary_guidance, seniority, years_experience, citizenship_json,
                     locations_json, willing_to_relocate, comp_floor_usd, target_roles_json,
                     work_modes_json, target_industries_json, dealbreakers_json,
                     strengths_json, weaknesses_json, skill_ratings_json,
                     education_json, skills_json, experience_json, raw_achievements_md)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?)
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
                    profile.seniority,
                    profile.years_experience,
                    citizenship_json,
                    locations_json,
                    1 if profile.willing_to_relocate else 0,
                    profile.comp_floor_usd,
                    target_roles_json,
                    work_modes_json,
                    target_industries_json,
                    dealbreakers_json,
                    strengths_json,
                    weaknesses_json,
                    skill_ratings_json,
                    edu_json,
                    skills_json,
                    exp_json,
                    profile.raw_achievements_md,
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
                       seniority = ?,
                       years_experience = ?,
                       citizenship_json = ?,
                       locations_json = ?,
                       willing_to_relocate = ?,
                       comp_floor_usd = ?,
                       target_roles_json = ?,
                       work_modes_json = ?,
                       target_industries_json = ?,
                       dealbreakers_json = ?,
                       strengths_json = ?,
                       weaknesses_json = ?,
                       skill_ratings_json = ?,
                       education_json = ?,
                       skills_json = ?,
                       experience_json = ?,
                       raw_achievements_md = ?
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
                    profile.seniority,
                    profile.years_experience,
                    citizenship_json,
                    locations_json,
                    1 if profile.willing_to_relocate else 0,
                    profile.comp_floor_usd,
                    target_roles_json,
                    work_modes_json,
                    target_industries_json,
                    dealbreakers_json,
                    strengths_json,
                    weaknesses_json,
                    skill_ratings_json,
                    edu_json,
                    skills_json,
                    exp_json,
                    profile.raw_achievements_md,
                ),
            )
        connection.commit()

        if sync_scoring:
            try:
                from careerradar.profile.repository import save_profile

                scoring_profile = master_profile_to_scoring_profile(profile)
                db_obj = db or Database()
                save_profile(scoring_profile, model="master-profile-sync", db=db_obj)
            except (sqlite3.Error, ValueError, RuntimeError) as e:
                import logging

                logging.getLogger("careerradar").warning(
                    "Failed to auto-sync scoring profile: %s", e
                )
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
