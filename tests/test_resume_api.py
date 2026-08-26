"""Unit tests for FastAPI Resume Builder endpoints."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from careerradar.web.app import app


@patch("careerradar.resumes.repository.load_master_profile")
def test_get_master_profile(mock_load: MagicMock):
    from careerradar.resumes.models import ResumeMasterProfile

    mock_load.return_value = ResumeMasterProfile(name="Jane Doe", email="jane.doe@example.com")

    client = TestClient(app)
    resp = client.get("/api/resume-builder/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Jane Doe"
    assert data["email"] == "jane.doe@example.com"


@patch("careerradar.resumes.repository.save_master_profile")
def test_update_master_profile(mock_save: MagicMock):
    client = TestClient(app)
    payload = {
        "name": "Jane Doe Updated",
        "email": "user@new.com",
        "phone": "555-1234",
        "location": "Los Angeles",
        "github": "github.com/user",
        "linkedin": "linkedin.com/in/user",
        "website": "user.dev",
        "summary_guidance": "Lead architect.",
        "education": [],
        "skills": [],
        "experience": [],
        "raw_achievements_md": "",
    }
    resp = client.put("/api/resume-builder/profile", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["profile"]["name"] == "Jane Doe Updated"
    mock_save.assert_called_once()


@patch("careerradar.resumes.builder.build_resume_for_job")
def test_generate_resume_endpoint(mock_build: MagicMock):
    mock_build.return_value = {
        "id": 1,
        "job_id": 5,
        "docx_path": "/tmp/resume.docx",
        "pdf_path": "/tmp/resume.pdf",
        "ats_score": 9,
        "ats_verdict": "passed",
        "summary": "Tailored summary",
    }
    client = TestClient(app)
    resp = client.post("/api/jobs/5/resume/generate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["record"]["ats_score"] == 9


@patch("careerradar.resumes.repository.list_generated_resumes")
def test_list_resumes_endpoint(mock_list: MagicMock):
    mock_list.return_value = [
        {
            "id": 1,
            "job_id": 5,
            "job_title": "Backend Engineer",
            "job_company": "Stripe",
            "ats_score": 9,
            "ats_verdict": "passed",
        }
    ]
    client = TestClient(app)
    resp = client.get("/api/resumes")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["job_company"] == "Stripe"
