"""Unit tests for DOCX & PDF rendering."""

import os
from pathlib import Path

import docx

from careerradar.resumes.models import (
    ResumeEducation,
    ResumeRole,
    ResumeSkillCategory,
    ResumeSubsection,
    TailoredResumePayload,
)
from careerradar.resumes.renderer import convert_to_pdf, render_docx, verify_page_count


def test_render_docx_and_convert_pdf(tmp_path: Path):
    payload = TailoredResumePayload(
        name="Jane Doe",
        contact_line_1="San Francisco Bay Area | jane.doe@example.com | 555-019-2834",
        contact_line_2="github.com/janedoe | linkedin.com/in/janedoe",
        summary=(
            "Senior full-stack software engineer specialized in distributed systems, "
            "LLM orchestration, and modern cloud infrastructure."
        ),
        experience=[
            ResumeRole(
                title="Lead Software Engineer",
                company="Acme Cloud Systems",
                dates="Jan 2020 - present",
                subsections=[
                    ResumeSubsection(
                        heading="Designed platform infrastructure and web services:",
                        bullets=[
                            "Architected distributed web application handling 50k+ users.",
                            "Automated cloud migrations saving 30+ engineering hrs/week.",
                        ],
                    )
                ],
            ),
        ],
        skills=[
            ResumeSkillCategory(
                category="Infrastructure & Cloud",
                skills="AWS, Docker, Terraform, Kubernetes, Linux",
            ),
            ResumeSkillCategory(
                category="Full-Stack & Languages",
                skills="Python, TypeScript, FastAPI, React, Node.js, SQLite, PostgreSQL",
            ),
        ],
        education=[
            ResumeEducation(
                institution="State University",
                degree="BS in Computer Science",
            )
        ],
    )

    docx_path = str(tmp_path / "test_resume.docx")
    out_file = render_docx(payload, docx_path)
    assert os.path.exists(out_file)
    assert os.path.getsize(out_file) > 1000

    # Inspect docx contents
    doc = docx.Document(docx_path)
    text = " ".join(p.text for p in doc.paragraphs)
    assert "JANE DOE" in text or "Jane Doe" in text
    assert "Acme Cloud Systems" in text
    assert "EXPERIENCE" in text
    assert "SKILLS" in text
    assert "EDUCATION" in text

    # Test PDF conversion
    pdf_path = convert_to_pdf(docx_path, str(tmp_path))
    if pdf_path:
        assert os.path.exists(pdf_path)
        pages = verify_page_count(pdf_path)
        assert pages == 1
