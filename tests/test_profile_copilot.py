"""Unit tests for the Profile Copilot and resume parsing utilities."""

from careerradar.profile.copilot import parse_resume_file


def test_parse_resume_file_txt():
    content = b"Jane Doe\nSoftware Engineer\nExperience: 4.0 years\nSkills: Python, AWS"
    parsed = parse_resume_file(content, "resume.txt")
    assert "Jane Doe" in parsed
    assert "Software Engineer" in parsed
    assert "Python, AWS" in parsed


def test_parse_resume_file_md():
    content = b"# Jane Doe\n\n## Experience\n- Built AI systems using FastAPI and Docker"
    parsed = parse_resume_file(content, "resume.md")
    assert "Jane Doe" in parsed
    assert "FastAPI" in parsed
