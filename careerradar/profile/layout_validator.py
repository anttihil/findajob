"""Deterministic 1-page layout and vertical point budget validator."""

import math

from careerradar.profile.models import LayoutValidationResult, TailoredResumePayload

# Usable vertical printable height for Letter (11in = 792pt) with 0.5in top/bottom margins:
# 792 - 72 = 720pt printable height. Ceiling is 695pt to prevent page overflow.
MAX_PAGE_POINTS = 695.0

CHARS_PER_LINE_SUMMARY = 102
CHARS_PER_LINE_BULLET = 96
CHARS_PER_LINE_SKILL = 100
CHARS_PER_LINE_EDU = 100


def estimate_visual_lines(text: str, chars_per_line: int) -> int:
    """Estimate the number of visual rendered lines for a string."""
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return 0
    return max(1, math.ceil(len(cleaned) / chars_per_line))


def calculate_resume_points(payload: TailoredResumePayload) -> tuple[float, list[str]]:
    """Calculate the estimated vertical points and collect any layout violations."""
    violations: list[str] = []
    total_pts = 0.0

    # 1. Header (Name + Contact Line 1 + Contact Line 2)
    # Name: 28pt; Contact lines: 14pt + 16pt (includes after-spacing)
    header_pts = 58.0
    total_pts += header_pts

    # 2. Summary
    sum_len = len(payload.summary.strip())
    if sum_len > 340:
        violations.append(
            f"Summary is too long ({sum_len} chars > 340 max). Condense to 2-3 concise sentences."
        )
    sum_lines = estimate_visual_lines(payload.summary, CHARS_PER_LINE_SUMMARY)
    if sum_lines > 4:
        violations.append(f"Summary takes {sum_lines} visual lines (maximum is 3-4 lines).")
    # 12.5pt per line + 6pt margins
    sum_pts = (sum_lines * 12.5) + 6.0
    total_pts += sum_pts

    # 3. Section Headings (Experience, Skills, Education)
    # 11pt font + 6pt before + 2pt after = 19pt per section header
    section_headers_pts = 3 * 19.0
    total_pts += section_headers_pts

    # 4. Experience Section
    total_bullets = 0
    for r_idx, role in enumerate(payload.experience, 1):
        # Role title + dates line: 16pt for 1st role (4pt before), 22pt for subsequent (10pt before)
        total_pts += 22.0 if r_idx > 1 else 16.0

        for s_idx, sub in enumerate(role.subsections, 1):
            if sub.heading and sub.heading.strip():
                # Subheading line: 14pt for 1st sub (2pt before), 22pt for subsequent (10pt before)
                total_pts += 22.0 if s_idx > 1 else 14.0

            for b_idx, bullet in enumerate(sub.bullets, 1):
                total_bullets += 1
                b_len = len(bullet.strip())
                if b_len > 180:
                    violations.append(
                        f"Role {r_idx} bullet {b_idx} is too long ({b_len} chars > 180 max)."
                    )
                b_lines = estimate_visual_lines(bullet, CHARS_PER_LINE_BULLET)
                if b_lines > 3:
                    violations.append(
                        f"Role {r_idx} bullet {b_idx} wraps to {b_lines} lines (max 2-3 lines)."
                    )
                # If no heading and subsequent subsection, first bullet has 10pt space before
                if not (sub.heading and sub.heading.strip()) and s_idx > 1 and b_idx == 1:
                    total_pts += 10.0
                # 11.5pt per line + 1.5pt space after
                total_pts += (b_lines * 11.5) + 1.5

    if total_bullets > 14:
        violations.append(
            f"Total bullet count ({total_bullets}) exceeds recommended 1-page maximum (11-13)."
        )

    # 5. Skills Section
    if len(payload.skills) > 5:
        violations.append(f"Too many skill categories ({len(payload.skills)} > 5 max).")
    for cat in payload.skills:
        cat_text = f"{cat.category}: {cat.skills}"
        cat_lines = estimate_visual_lines(cat_text, CHARS_PER_LINE_SKILL)
        total_pts += (cat_lines * 11.5) + 1.5

    # 6. Education Section
    for edu in payload.education:
        edu_text = f"{edu.institution}, {edu.degree}"
        edu_lines = estimate_visual_lines(edu_text, CHARS_PER_LINE_EDU)
        total_pts += (edu_lines * 11.5) + 1.5

    if total_pts > MAX_PAGE_POINTS:
        violations.append(
            f"Estimated vertical height ({total_pts:.1f} pt) exceeds 1-page ceiling "
            f"({MAX_PAGE_POINTS:.1f} pt) by {total_pts - MAX_PAGE_POINTS:.1f} pt."
        )

    return total_pts, violations


def validate_resume_layout(payload: TailoredResumePayload) -> LayoutValidationResult:
    """Validate that a tailored resume payload fits cleanly onto a single page."""
    total_pts, violations = calculate_resume_points(payload)
    return LayoutValidationResult(
        is_valid=(len(violations) == 0),
        estimated_points=round(total_pts, 1),
        max_points=MAX_PAGE_POINTS,
        violations=violations,
    )
