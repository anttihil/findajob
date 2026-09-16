"""The `findajob profile` command-line interface."""

import argparse
import os
import sys
import textwrap
from typing import Any

from careerradar.core.config import load_config
from careerradar.core.llm import get_model_for_role
from careerradar.core.logger import get_logger
from careerradar.profile.copilot import extract_profile_from_resume_text, parse_resume_file
from careerradar.profile.render import render_profile
from careerradar.profile.repository import list_versions, load_profile, save_profile

logger = get_logger()

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def _supports_colour() -> bool:
    return sys.stdout.isatty()


def _style(text: Any, code: str) -> str:
    return f"{code}{text}{RESET}" if _supports_colour() else str(text)


def _wrap(text: Any, indent: str = "  ") -> str:
    return "\n".join(
        textwrap.fill(line, width=88, initial_indent=indent, subsequent_indent=indent)
        if line.strip()
        else ""
        for line in str(text).split("\n")
    )


def cmd_show(_args: argparse.Namespace) -> int:
    profile = load_profile()
    if not profile.name and not profile.summary_guidance:
        print("No active profile configured. Set up your profile with:")
        print("  findajob profile build <path/to/resume.pdf>")
        return 1

    print(_style(f"Profile: {profile.name or 'Unnamed'}", BOLD))
    if profile.email or profile.phone or profile.location:
        print(f"  {profile.email} | {profile.phone} | {profile.location}")
    print()
    print(_style("SUMMARY / POSITIONING", BOLD))
    print(_wrap(profile.summary_guidance or "None provided."))
    print()
    print(_style("SKILLS", BOLD))
    for cat in profile.skills:
        print(f"  {_style(cat.category, BOLD)}: {', '.join(cat.skills)}")
    print()
    print(_style("EXPERIENCE", BOLD))
    for role in profile.experience:
        print(f"  {_style(role.title, BOLD)} at {role.company} ({role.dates})")
        for proj in role.projects:
            if proj.heading:
                print(f"    {proj.heading}")
            for bullet in proj.bullets:
                print(f"      • {bullet}")
    print()
    print(_style("PROMPT PREFIX", BOLD))
    rendered = render_profile(profile)
    print(_wrap(f"{len(rendered)} characters rendered for scoring prefix."))
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    resume_file = args.file
    config = load_config()
    model = config.get("profile", {}).get("model") or get_model_for_role("agent")

    if not os.path.isfile(resume_file):
        print(f"Error: resume file not found: {resume_file}")
        return 1

    with open(resume_file, "rb") as f:
        content = f.read()
    text = parse_resume_file(content, os.path.basename(resume_file))
    if not text.strip():
        print("Error: resume file contained no readable text.")
        return 1

    print(f"Extracting candidate profile using model {model}...")
    existing = load_profile()
    profile = extract_profile_from_resume_text(
        text, model_name=model, existing_profile=existing if existing.name else None
    )
    save_profile(profile)
    print(_style(f"Successfully extracted and saved profile for {profile.name}!", BOLD))
    print(f"  Skills: {len(profile.skills)} categories")
    print(f"  Roles: {len(profile.experience)} work history entries")
    print("Run `findajob profile show` to inspect.")
    return 0


def cmd_history(_args: argparse.Namespace) -> int:
    versions = list_versions()
    print(f"{'ver':>4}  {'active':^6}  {'name':<30}")
    for row in versions:
        print(f"{row['version']:>4}  {'  *   ' if row['is_active'] else '      '}  {row['name']}")
    return 0


def run_profile_command(args: argparse.Namespace) -> int:
    handlers = {"show": cmd_show, "build": cmd_build, "history": cmd_history}
    handler = handlers.get(getattr(args, "subcommand", "show") or "show", cmd_show)
    return handler(args)
