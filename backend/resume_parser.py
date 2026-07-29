"""Parse resume markdown and achievements.md into skill evidence.

Two responsibilities, deliberately kept apart:

  1. *Candidate extraction* (this module) turns markdown into generous surface strings.
     It over-produces on purpose -- "AWS (EC2, S3)" yields AWS, EC2, S3, and
     "Ansible/Trellis" yields the joined form plus both halves.
  2. *Canonicalization* (backend/taxonomy.py) maps those surfaces onto canonical skill
     keys and silently drops anything unrecognized.

The previous implementation did both at once, which is why it deleted parentheticals --
losing EC2/S3/IAM/VPC/SSM and FastAPI/Flask entirely -- and why it needed a hardcoded
keyword allowlist to compensate.
"""

import os
import re

# Every skills-section heading actually used across the resume set. The old regex matched
# only "## Skills", which is present in just 3 of the 7 files; the other 4 silently fell
# through to a hardcoded keyword list.
SKILLS_HEADINGS = [
    "Skills",
    "Technical Competencies",
    "Core Competencies",
    "Technical & Professional Competencies",
]

_HEADING_ALTERNATION = "|".join(re.escape(h) for h in SKILLS_HEADINGS)
SKILLS_SECTION_RE = re.compile(
    rf"^##\s+(?:{_HEADING_ALTERNATION})\s*$(.*?)(?=^##\s|\Z)",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)

# "*   **Frontend:** ReactJS, ..." / "* **Full-Stack Development:** Go (Golang), ..."
BULLET_RE = re.compile(r"^\s*[*\-+]\s+(.*)$", re.MULTILINE)
CATEGORY_PREFIX_RE = re.compile(r"^\s*\*\*(?P<category>[^*]+?):?\*\*\s*:?\s*")

# Fragments that are never skills on their own, mostly produced by splitting compounds.
STOP_CANDIDATES = {
    "", "and", "or", "the", "a", "an", "etc", "basic", "advanced", "legacy",
    "ci", "cd", "prototype", "packaging", "e", "g", "i",
}

MIN_CANDIDATE_LENGTH = 2


def _split_top_level(text, separators=(",", ";")):
    """Split on separators that are not inside parentheses or backticks.

    "AWS (EC2, S3), Terraform" must split into two items, not four -- so a naive
    text.split(",") is wrong here.
    """
    parts = []
    buf = []
    depth = 0
    in_code = False

    for char in text:
        if char == "`":
            in_code = not in_code
            buf.append(char)
        elif char in "([{" and not in_code:
            depth += 1
            buf.append(char)
        elif char in ")]}" and not in_code:
            depth = max(0, depth - 1)
            buf.append(char)
        elif char in separators and depth == 0 and not in_code:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)

    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _clean_surface(text):
    """Strip markdown decoration from a candidate surface string."""
    text = text.replace("`", "")
    text = re.sub(r"\*\*|__|(?<!\S)\*(?!\S)", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().strip(".,;:").strip()


def _expand_item(item, out):
    """Expand one comma-separated item into candidate surfaces.

    Handles the three compound shapes that appear in these resumes:
      "AWS (EC2, S3, IAM)"   -> AWS, EC2, S3, IAM
      "Python (FastAPI/Flask)"-> Python, FastAPI/Flask, FastAPI, Flask
      "Ansible/Trellis"       -> Ansible/Trellis, Ansible, Trellis
    """
    item = item.strip()
    if not item:
        return

    match = re.match(r"^(?P<head>[^(]+?)\s*\((?P<inner>.+)\)\s*$", item, re.DOTALL)
    if match:
        _emit(match.group("head"), out)
        for inner in _split_top_level(match.group("inner")):
            _expand_item(inner, out)
        return

    # Strip a trailing parenthetical qualifier that isn't a list, e.g.
    # "Create React App (legacy)" -> keep the head, discard the qualifier.
    stripped = re.sub(r"\s*\([^)]*\)\s*$", "", item).strip()
    if stripped and stripped != item:
        _expand_item(stripped, out)
        return

    _emit(item, out)

    # Emit slash-separated halves as well as the joined form. "CI/CD" stays useful as a
    # whole while "Ansible/Trellis" and "TypeScript/JavaScript" need splitting -- rather
    # than guessing which is which, emit all forms and let the taxonomy keep what it knows.
    if "/" in item:
        for half in item.split("/"):
            half = half.strip()
            if half:
                _emit(half, out)


def _emit(surface, out):
    cleaned = _clean_surface(surface)
    if len(cleaned) < MIN_CANDIDATE_LENGTH:
        return
    if cleaned.lower() in STOP_CANDIDATES:
        return
    if cleaned not in out:
        out.append(cleaned)


def extract_skill_candidates(value):
    """Turn one skills-bullet value into candidate surface strings.

    Over-produces by design; the taxonomy filters.
    """
    out = []
    for item in _split_top_level(value):
        _expand_item(item, out)
    return out


def parse_skills_section(content):
    """Extract {category: [candidates]} from a resume's skills section."""
    match = SKILLS_SECTION_RE.search(content)
    if not match:
        return {}

    categories = {}
    for bullet in BULLET_RE.findall(match.group(1)):
        prefix = CATEGORY_PREFIX_RE.match(bullet)
        if prefix:
            category = _clean_surface(prefix.group("category"))
            value = bullet[prefix.end():]
        else:
            category = "General"
            value = bullet
        candidates = extract_skill_candidates(value)
        if candidates:
            categories.setdefault(category, []).extend(candidates)
    return categories


# Inline emphasis in experience bullets is a weaker but real signal: **vLLM**, `ocrmypdf`.
INLINE_EMPHASIS_RE = re.compile(r"\*\*([^*]{2,40})\*\*|`([^`]{2,40})`")


def extract_inline_mentions(content):
    """Collect bold/backticked terms from prose, excluding the skills section itself."""
    body = SKILLS_SECTION_RE.sub("", content)
    mentions = []
    for bold, code in INLINE_EMPHASIS_RE.findall(body):
        term = bold or code
        # Bold lead-ins like "**Automated Data & Site Migration Engine:**" are prose
        # headings, not skills. Skip anything sentence-shaped.
        if term.endswith(":") or len(term.split()) > 4:
            continue
        _emit(term, mentions)
    return mentions


class ResumeParser:
    """Parses the resume markdown set into per-file skill candidates.

    The public shape ({filename: {"title", "skills"}}) is preserved so backend/main.py
    and the matcher keep working. `canonicalize` is injected rather than imported so this
    module stays independently testable.
    """

    def __init__(self, resumes_dir=None, canonicalize=None):
        if resumes_dir is None:
            resumes_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "resumes",
            )
        self.resumes_dir = resumes_dir
        self.canonicalize = canonicalize

    def parse_all(self):
        if not os.path.exists(self.resumes_dir):
            print(f"Resumes directory not found at: {self.resumes_dir}")
            return {}

        resumes = {}
        for filename in sorted(os.listdir(self.resumes_dir)):
            if filename.endswith(".md"):
                path = os.path.join(self.resumes_dir, filename)
                resumes[filename] = self.parse_file(path)
        return resumes

    def parse_file(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()

        title = os.path.basename(path).replace(".md", "").replace("_", " ").title()
        first_line = content.split("\n", 1)[0]
        if first_line.startswith("# ") and "hiltunen" not in first_line.lower():
            title = first_line[2:].strip()

        categories = parse_skills_section(content)
        candidates = []
        for values in categories.values():
            for value in values:
                if value not in candidates:
                    candidates.append(value)

        inline = [m for m in extract_inline_mentions(content) if m not in candidates]

        skills = candidates + inline
        if self.canonicalize:
            skills = self.canonicalize(skills)

        return {
            "title": title,
            "skills": sorted(skills),
            "categories": categories,
            "candidates": candidates,
            "inline_mentions": inline,
            "has_skills_section": bool(categories),
        }


# --- achievements.md -------------------------------------------------------------------
# The richest source in the repo: a categorized technology table plus, per project, an
# italic metadata line carrying a date range, the stack, and commit/PR volume. That gives
# recency and depth weighting per skill, which no resume file provides.

TECH_TABLE_RE = re.compile(
    r"^##\s+Technology Summary\s*$(.*?)(?=^##\s|\Z)",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)
TABLE_ROW_RE = re.compile(r"^\|\s*(?P<category>[^|]+?)\s*\|\s*(?P<items>[^|]+?)\s*\|\s*$",
                          re.MULTILINE)

PROJECT_HEADING_RE = re.compile(
    r"^###\s+\d+\.\s+(?P<name>.+?)(?:\s+\(`(?P<repo>[^`]+)`\))?\s*$", re.MULTILINE
)
# "*Jan 2025 – present | TypeScript, Python, PostgreSQL | 266 commits, 67 merged PRs*"
# Four date shapes occur in practice, and the year may appear only on the second month:
#   "Jan 2025 – present"   "Feb – Mar 2026"   "Mar 2026"   "Ongoing"
# The pipe-delimited tech field is required, which correctly excludes prose italics such as
# "*Ongoing side work — useful for ... framing*" that carry no stack.
_MONTH = r"[A-Z][a-z]{2}"
_DATE_POINT = rf"(?:Ongoing|present|{_MONTH}(?:\s+\d{{4}})?)"
PROJECT_META_RE = re.compile(
    rf"^\*(?P<dates>{_DATE_POINT}(?:\s*[–—\-]\s*{_DATE_POINT})?)"
    r"\s*\|\s*(?P<tech>[^|]+?)"
    r"(?:\s*\|\s*(?P<volume>.+?))?\*\s*$",
    re.MULTILINE,
)
# "present" and "Ongoing" both mean the work is live, which is what recency weighting needs.
CURRENT_MARKERS = ("present", "ongoing")
COMMITS_RE = re.compile(r"(\d+)\s+commits?", re.IGNORECASE)
PRS_RE = re.compile(r"(\d+)\s+merged\s+PRs?", re.IGNORECASE)


def parse_technology_summary(content):
    """Extract {category: [candidates]} from the Technology Summary table."""
    match = TECH_TABLE_RE.search(content)
    if not match:
        return {}

    categories = {}
    for row in TABLE_ROW_RE.finditer(match.group(1)):
        category = _clean_surface(row.group("category"))
        items = row.group("items").strip()
        # Skip the header and the |---|---| separator.
        if category.lower() in {"category", ""} or set(items) <= set("-: "):
            continue
        candidates = extract_skill_candidates(items)
        if candidates:
            categories[category] = candidates
    return categories


def parse_projects(content):
    """Extract per-project stack, date range, and commit/PR volume."""
    headings = list(PROJECT_HEADING_RE.finditer(content))
    projects = []

    for index, heading in enumerate(headings):
        start = heading.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
        block = content[start:end]

        meta = PROJECT_META_RE.search(block)
        if not meta:
            continue

        volume = meta.group("volume") or ""
        commits = COMMITS_RE.search(volume)
        prs = PRS_RE.search(volume)
        dates = meta.group("dates")

        projects.append({
            "name": _clean_surface(heading.group("name")),
            "repo": heading.group("repo"),
            "dates": dates,
            "is_current": any(m in dates.lower() for m in CURRENT_MARKERS),
            "tech": extract_skill_candidates(meta.group("tech")),
            "commits": int(commits.group(1)) if commits else 0,
            "merged_prs": int(prs.group(1)) if prs else 0,
        })
    return projects


def build_skill_evidence(projects):
    """Aggregate per-skill evidence strength from project metadata.

    A skill used in a current project with 266 commits is stronger evidence than one
    named once in a bullet, and the matcher should be able to tell those apart.
    """
    evidence = {}
    for project in projects:
        for surface in project["tech"]:
            record = evidence.setdefault(surface, {
                "projects": [],
                "commits": 0,
                "merged_prs": 0,
                "is_current": False,
            })
            record["projects"].append(project["name"])
            record["commits"] += project["commits"]
            record["merged_prs"] += project["merged_prs"]
            record["is_current"] = record["is_current"] or project["is_current"]
    return evidence


class AchievementsParser:
    """Parses achievements.md for the canonical stack and per-skill evidence weight."""

    def __init__(self, path, canonicalize=None):
        self.path = path
        self.canonicalize = canonicalize

    def parse(self):
        if not os.path.exists(self.path):
            return {"technology_summary": {}, "projects": [], "skill_evidence": {}}

        with open(self.path, "r", encoding="utf-8") as handle:
            content = handle.read()

        summary = parse_technology_summary(content)
        projects = parse_projects(content)
        evidence = build_skill_evidence(projects)

        candidates = []
        for values in summary.values():
            for value in values:
                if value not in candidates:
                    candidates.append(value)

        skills = self.canonicalize(candidates) if self.canonicalize else candidates

        return {
            "technology_summary": summary,
            "projects": projects,
            "skill_evidence": evidence,
            "skills": sorted(skills),
            "candidates": candidates,
        }


if __name__ == "__main__":
    parser = ResumeParser()
    for name, info in parser.parse_all().items():
        flag = "" if info["has_skills_section"] else "  <-- NO SKILLS SECTION MATCHED"
        print(f"{name}: {len(info['skills'])} skills{flag}")
        print(f"  categories: {list(info['categories'])}")
        print(f"  sample: {', '.join(info['skills'][:12])}")
