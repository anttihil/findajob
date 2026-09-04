"""Typst rendering engine for 1-page tailored resumes."""

import os
from pathlib import Path

import pypdf
from pypdf.errors import PdfReadError

from careerradar.core.logger import get_logger
from careerradar.core.paths import GENERATED_RESUMES_DIR
from careerradar.profile.models import TailoredResumePayload

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


def generate_typst_source(payload: TailoredResumePayload) -> str:
    """Generate high-fidelity 1-page Typst markup source code from resume payload."""
    lines: list[str] = [
        "// ============================================================================",
        f"// Tailored 1-Page Resume: {escape_typst(payload.name)}",
        "// Generated with Typst - Fast, deterministic typesetting engine",
        "// ============================================================================",
        "",
        "#set page(",
        '  paper: "us-letter",',
        "  margin: (x: 0.5in, top: 0.5in, bottom: 0.5in),",
        ")",
        "#set text(",
        '  font: ("Arial", "Liberation Sans", "DejaVu Sans"),',
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
        "// --- Header ---",
        "#grid(",
        "  row-gutter: 3pt,",
        f'  text(20pt, weight: "bold")[{escape_typst(payload.name)}],',
        f"  text()[{escape_typst(payload.contact_line_1)}],",
    ]
    if payload.contact_line_2 and payload.contact_line_2.strip():
        lines.append(f"  text()[{escape_typst(payload.contact_line_2)}],")
    lines.extend(
        [
            ")",
            "",
            "#v(2pt)",
            '#line(length: 100%, stroke: 1pt + rgb("#000000"))',
            "#v(2pt)",
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
                f'  text(11pt, fill: rgb("#000000"))[{escape_typst(role.dates)}],',
                ")",
            ]
        )
        for sub in role.subsections:
            if sub.heading and sub.heading.strip():
                lines.extend(
                    [
                        "#v(1pt)",
                        f'#text(style: "italic")[{escape_typst(sub.heading)}]',
                    ]
                )
            lines.append("#v(1pt)")
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
        out_dir = output_dir or os.path.dirname(typst_path) or GENERATED_RESUMES_DIR
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
