"""Unit tests for FastAPI Profile and Resume Builder endpoints."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from careerradar.profile.models import Profile
from careerradar.web.app import app


@patch("careerradar.profile.repository.load_profile")
def test_get_master_profile(mock_load: MagicMock):
    mock_load.return_value = Profile(name="Jane Doe", email="jane.doe@example.com")

    client = TestClient(app)
    resp = client.get("/api/resume-builder/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Jane Doe"
    assert data["email"] == "jane.doe@example.com"

    # Also test /api/profile endpoint
    resp2 = client.get("/api/profile")
    assert resp2.status_code == 200
    assert resp2.json()["name"] == "Jane Doe"

    # Also test /api/profile/vector endpoint
    resp3 = client.get("/api/profile/vector")
    assert resp3.status_code == 200
    vdata = resp3.json()
    assert vdata["version"] == 1
    assert vdata["profile"]["name"] == "Jane Doe"
    assert "summary_text" in vdata
    assert isinstance(vdata["skills_vector"], list)


@patch("careerradar.profile.repository.save_profile")
def test_update_master_profile(mock_save: MagicMock):
    client = TestClient(app)
    payload = {
        "name": "Jane Doe Updated",
        "email": "jane@example.com",
        "phone": "555-1234",
        "location": "San Francisco",
        "github": "github.com/janedoe",
        "linkedin": "linkedin.com/in/janedoe",
        "website": "example.dev",
        "summary_guidance": "Lead architect.",
        "education": [],
        "skills": [],
        "experience": [],
    }
    resp = client.put("/api/resume-builder/profile", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["profile"]["name"] == "Jane Doe Updated"
    mock_save.assert_called_once()


@patch("careerradar.profile.builder.build_resume_for_job")
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


@patch("careerradar.profile.repository.list_tailored_resumes")
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

    resp_filtered = client.get("/api/resumes?job_id=5")
    assert resp_filtered.status_code == 200
    assert mock_list.call_count == 2
    _args, kwargs = mock_list.call_args
    assert kwargs.get("job_id") == 5


@patch("careerradar.profile.copilot.extract_profile_from_resume_text")
@patch("careerradar.profile.copilot.parse_resume_file")
def test_upload_resume_success(mock_parse: MagicMock, mock_extract: MagicMock):
    import io

    mock_parse.return_value = "Jane Doe Software Engineer 5 years experience Python Go"
    mock_extract.return_value = Profile(name="Jane Doe", email="jane@example.com")

    client = TestClient(app)
    file_bytes = b"Sample resume content"
    files = {"file": ("resume.pdf", io.BytesIO(file_bytes), "application/pdf")}

    resp = client.post("/api/profile/upload-resume", files=files)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["filename"] == "resume.pdf"
    assert data["profile"]["name"] == "Jane Doe"
    mock_parse.assert_called_once_with(file_bytes, "resume.pdf")
    mock_extract.assert_called_once()


def test_upload_resume_missing_file():
    client = TestClient(app)
    resp = client.post("/api/profile/upload-resume", data={"other_field": "val"})
    assert resp.status_code == 400
    data = resp.json()
    assert "Missing 'file' in upload form payload." in data["detail"]


@patch("careerradar.profile.copilot.parse_resume_file")
def test_upload_resume_empty_content(mock_parse: MagicMock):
    import io

    mock_parse.return_value = "   "
    client = TestClient(app)
    files = {"file": ("resume.txt", io.BytesIO(b""), "text/plain")}
    resp = client.post("/api/profile/upload-resume", files=files)
    assert resp.status_code == 400
    data = resp.json()
    assert "Uploaded file contained no readable text." in data["detail"]
