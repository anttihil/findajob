"""Facade runner for generating tailored resumes."""

from typing import Any

from careerradar.core.database import Database
from careerradar.core.llm import DEFAULT_AGENT_MODEL
from careerradar.core.logger import get_logger
from careerradar.resumes.graph import ResumeState, build_resume_graph
from careerradar.resumes.models import ResumeMasterProfile
from careerradar.resumes.repository import get_resume_by_id, load_master_profile

logger = get_logger()


def build_resume_for_job(
    job_id: int,
    model: str = DEFAULT_AGENT_MODEL,
    master_profile: ResumeMasterProfile | None = None,
    db: Database | None = None,
) -> dict[str, Any]:
    """Run the Actor-Critic Resume Builder graph for a single job."""
    owned = db is None
    database = db or Database()
    try:
        job_res = database.query_jobs(job_id=job_id, detail=True)
        jobs = job_res.get("jobs") or []
        if not jobs:
            raise ValueError(f"Job with id {job_id} not found.")
        job = jobs[0]

        profile = master_profile or load_master_profile(database.conn)
        graph = build_resume_graph()

        initial_state: ResumeState = {
            "job": job,
            "master_profile": profile,
            "model_name": model,
        }

        logger.info(
            "Starting resume build for job %d (%s at %s)",
            job_id,
            job.get("title"),
            job.get("company"),
        )
        final_state: dict[str, Any] = graph.invoke(initial_state)

        saved_id = final_state.get("saved_id")
        if saved_id:
            record = get_resume_by_id(saved_id, database.conn)
            if record:
                return record

        return {
            "job_id": job_id,
            "docx_path": final_state.get("docx_path"),
            "pdf_path": final_state.get("pdf_path"),
            "resume": final_state.get("resume_payload"),
            "ats_verdict": final_state.get("ats_verdict"),
        }
    finally:
        if owned:
            database.close()
