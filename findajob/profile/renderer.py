"""Typst rendering engine for 1-page tailored resumes."""

import os
from pathlib import Path

import pypdf
from pypdf.errors import PdfReadError

from findajob.core.logger import get_logger
from findajob.core.paths import generated_resumes_dir
from findajob.profile.models import Profile, TailoredResumePayload

logger = get_logger()


def escape_typst(text: str) -> str:
    """Escape special Typst markup characters in user-provided text."""
    if not text:
        return ""
    replacements = [
        ("\\", "\\\\"),
        ("@", "\\@"),
        ("<", "\\<"),
        (">", "\\>"),
        ("$", "\\$"),
        ("#", "\\#"),
        ("[", "\\["),
        ("]", "\\]"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def _resume_layout_preamble() -> list[str]:
    """Return the shared Typst layout rules for every resume export."""
    return [
        "#set page(",
        '  paper: "us-letter",',
        "  margin: (x: 0.5in, top: 0.5in, bottom: 0.5in),",
        ")",
        "#set text(",
        '  font: "Liberation Sans",',
        "  size: 11pt,",
        '  fill: rgb("#000000"),',
        ")",
        "#set par(justify: false, leading: 0.50em)",
        "",
        "// Helper: Section heading with bottom rule (tight underline)",
        "#let section-heading(title) = {",
        "  block(",
        "    width: 100%,",
        '    stroke: (bottom: 1pt + rgb("#000000")),',
        "    inset: (bottom: 2pt),",
        "    above: 14pt,",
        "    below: 10pt,",
        '  )[#text(11pt, weight: "bold")[#upper(title)]]',
        "}",
        "",
    ]


def _resume_header(name: str, contact_lines: list[str]) -> list[str]:
    """Return the shared left-aligned resume header and divider."""
    lines = [
        "// --- Header ---",
        "#grid(",
        "  row-gutter: 3pt,",
        f'  text(20pt, weight: "bold")[{escape_typst(name)}],',
    ]
    lines.extend(f"  text()[{escape_typst(contact_line)}]," for contact_line in contact_lines)
    lines.extend(
        [
            ")",
            "",
            '#line(length: 100%, stroke: 1pt + rgb("#000000"))',
            "#v(2pt)",
        ]
    )
    return lines


def generate_typst_source(payload: TailoredResumePayload) -> str:
    """Generate high-fidelity 1-page Typst markup source code from resume payload."""
    lines: list[str] = [
        "// ============================================================================",
        f"// Tailored 1-Page Resume: {escape_typst(payload.name)}",
        "// Generated with Typst - Fast, deterministic typesetting engine",
        "// ============================================================================",
        "",
    ]
    contact_lines = [payload.contact_line_1]
    if payload.contact_line_2 and payload.contact_line_2.strip():
        contact_lines.append(payload.contact_line_2)
    lines.extend(_resume_layout_preamble())
    lines.extend(_resume_header(payload.name, contact_lines))
    lines.extend(
        [
            "",
            "// --- Summary ---",
            f"#text()[{escape_typst(payload.summary)}]",
            "",
            "// --- Experience ---",
            '#section-heading("EXPERIENCE")',
        ]
    )

    for r_idx, role in enumerate(payload.experience):
        if r_idx > 0:
            lines.append("#v(4pt)")
        lines.extend(
            [
                "#grid(",
                "  columns: (1fr, auto),",
                f"  [*{escape_typst(role.title)}*, {escape_typst(role.company)}],",
                f"  text()[{escape_typst(role.dates)}],",
                ")",
            ]
        )
        for sub in role.subsections:
            if sub.heading and sub.heading.strip():
                lines.extend(
                    [
                        f'#text(style: "italic")[{escape_typst(sub.heading)}]',
                    ]
                )
            lines.append("#list(")
            lines.append("  tight: true,")
            for bullet in sub.bullets:
                lines.append(f"  [{escape_typst(bullet)}],")
            lines.append(")")

    lines.extend(
        [
            "",
            "// --- Skills ---",
            '#section-heading("SKILLS")',
        ]
    )
    for cat in payload.skills:
        lines.append(f"*{escape_typst(cat.category)}:* {escape_typst(cat.skills)} \\")

    lines.extend(
        [
            "",
            "// --- Education ---",
            '#section-heading("EDUCATION")',
        ]
    )
    for edu in payload.education:
        lines.extend(
            [
                "#grid(",
                "  columns: (1fr, auto),",
                f"  [*{escape_typst(edu.institution)}*, {escape_typst(edu.degree)}],",
                "  [],",
                ")",
            ]
        )

    return "\n".join(lines)


def generate_master_profile_typst_source(profile: Profile) -> str:
    """Generate a general-purpose resume from the complete master profile.

    Unlike tailored resumes, this deliberately includes all saved experience and
    standalone projects. It is therefore suitable for a general application or
    as an editable starting point for a bespoke resume.
    """
    contact = " | ".join(part for part in (profile.location, profile.email, profile.phone) if part)
    links = " | ".join(part for part in (profile.github, profile.linkedin, profile.website) if part)
    lines = [
        "// ============================================================================",
        f"// General-purpose Master Profile Resume: {escape_typst(profile.name)}",
        "// Generated from the complete CareerRadar master profile",
        "// ============================================================================",
        "",
    ]
    lines.extend(_resume_layout_preamble())
    lines.extend(_resume_header(profile.name, [line for line in (contact, links) if line]))
    if profile.executive_summary:
        lines.extend(["", escape_typst(profile.executive_summary)])

    if profile.experience:
        lines.extend(["", '#section-heading("EXPERIENCE")'])
        for role_index, role in enumerate(profile.experience):
            if role_index > 0:
                lines.append("#v(4pt)")
            lines.extend(
                [
                    "#grid(",
                    "  columns: (1fr, auto),",
                    f"  [*{escape_typst(role.title)}*, {escape_typst(role.company)}],",
                    f"  text()[{escape_typst(role.dates)}],",
                    ")",
                ]
            )
            for project in role.projects:
                if project.heading:
                    lines.append(f'#text(style: "italic")[{escape_typst(project.heading)}]')
                if project.url:
                    lines.append(f"#text(size: 9pt)[{escape_typst(project.url)}]")
                if project.bullets:
                    lines.extend(["#list(tight: true,"])
                    lines.extend(f"  [{escape_typst(bullet)}]," for bullet in project.bullets)
                    lines.append(")")

    if profile.projects:
        lines.extend(["", '#section-heading("PROJECTS")'])
        for project in profile.projects:
            if project.heading:
                lines.append(f"*{escape_typst(project.heading)}*")
            if project.url:
                lines.append(f"#text(size: 9pt)[{escape_typst(project.url)}]")
            if project.bullets:
                lines.extend(["#list(tight: true,"])
                lines.extend(f"  [{escape_typst(bullet)}]," for bullet in project.bullets)
                lines.append(")")

    if profile.skills:
        lines.extend(["", '#section-heading("SKILLS")'])
        for category in profile.skills:
            category_name = escape_typst(category.category)
            skills = escape_typst(", ".join(category.skills))
            lines.append(f"*{category_name}:* {skills} \\")

    if profile.education:
        lines.extend(["", '#section-heading("EDUCATION")'])
        for education in profile.education:
            detail = f", {escape_typst(education.details)}" if education.details else ""
            institution = escape_typst(education.institution)
            degree = escape_typst(education.degree)
            lines.extend(
                [
                    "#grid(",
                    "  columns: (1fr, auto),",
                    f"  [*{institution}*, {degree}{detail}],",
                    "  [],",
                    ")",
                ]
            )

    return "\n".join(lines) + "\n"


def render_typst(
    payload: TailoredResumePayload,
    output_path: str,
) -> str:
    """Render the tailored resume into a high-fidelity 1-page Typst (.typ) markup file."""
    source = generate_typst_source(payload)
    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(source, encoding="utf-8")
    logger.info("Rendered resume typst saved to %s", output_path)
    return str(out_p)


def render_master_profile_typst(profile: Profile, output_path: str) -> str:
    """Render a general-purpose master-profile resume to a Typst file."""
    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(generate_master_profile_typst_source(profile), encoding="utf-8")
    logger.info("Rendered master profile Typst saved to %s", output_path)
    return str(out_p)


def compile_typst_to_pdf(
    typst_path: str,
    output_dir: str | None = None,
    output_pdf_path: str | None = None,
) -> str | None:
    """Compile a .typ file to PDF using the native typst compiler."""
    import typst

    if output_pdf_path:
        pdf_path = output_pdf_path
    else:
        out_dir = output_dir or os.path.dirname(typst_path) or generated_resumes_dir()
        stem = Path(typst_path).stem
        pdf_path = os.path.join(out_dir, f"{stem}.pdf")

    Path(pdf_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        typst.compile(typst_path, output=pdf_path)
        if os.path.exists(pdf_path):
            logger.info("Typst compiled %s -> %s", typst_path, pdf_path)
            return pdf_path
        logger.warning("Typst compilation succeeded but file not found at %s", pdf_path)
        return None
    except (OSError, RuntimeError) as exc:
        logger.warning("Typst PDF compilation failed for %s: %s", typst_path, exc)
        return None


def convert_to_pdf(source_path: str, output_dir: str | None = None) -> str | None:
    """Convert a Typst resume source file (.typ) to PDF."""
    if source_path.endswith(".typ"):
        return compile_typst_to_pdf(source_path, output_dir=output_dir)
    logger.warning("Unsupported resume source format for PDF conversion: %s", source_path)
    return None


def verify_page_count(pdf_path: str) -> int:
    """Check page count of rendered PDF."""
    try:
        reader = pypdf.PdfReader(pdf_path)
        return len(reader.pages)
    except (PdfReadError, OSError) as exc:
        logger.warning("Failed reading PDF page count for %s: %s", pdf_path, exc)
        return 0
