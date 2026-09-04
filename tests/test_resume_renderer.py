import os
from pathlib import Path

from careerradar.profile.models import (
    ResumeEducation,
    ResumeRole,
    ResumeSkillCategory,
    ResumeSubsection,
    TailoredResumePayload,
)
from careerradar.profile.renderer import compile_typst_to_pdf, render_typst, verify_page_count


def test_render_typst_and_compile_pdf(tmp_path: Path):

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
                    ),
                ],
            ),
        ],
        skills=[
            ResumeSkillCategory(
                category="Infrastructure & Cloud",
                skills="AWS, Docker, Terraform, Kubernetes, Linux",
            ),
        ],
        education=[
            ResumeEducation(
                institution="State University",
                degree="BS in Computer Science",
            )
        ],
    )

    typst_path = str(tmp_path / "test_resume.typ")
    out_file = render_typst(payload, typst_path)
    assert os.path.exists(out_file)
    assert os.path.getsize(out_file) > 100

    # Verify typst file content
    content = Path(out_file).read_text(encoding="utf-8")
    assert "Jane Doe" in content
    assert "#grid(" in content
    assert "row-gutter: 3pt," in content
    assert "Acme Cloud Systems" in content
    assert "EXPERIENCE" in content
    assert "SKILLS" in content
    assert "EDUCATION" in content

    # Verify native Typst PDF compilation
    pdf_path = compile_typst_to_pdf(typst_path, str(tmp_path))
    assert pdf_path is not None
    assert os.path.exists(pdf_path)
    pages = verify_page_count(pdf_path)
    assert pages == 1
