"""Unit tests for the Profile Copilot and resume parsing utilities."""

from unittest.mock import patch

import pytest

from findajob.profile.copilot import (
    MemoChatMessage,
    MemoChatResult,
    chat_with_memo_copilot,
    parse_resume_file,
)


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


def test_memo_copilot_sends_complete_editor_text_and_conversation():
    markdown = "Achievement\n" * 3000 + "Final correction: five users, not direct reports."
    proposed = MemoChatResult(
        reply="Merged the repeated achievements.",
        updated_markdown="Built tools used by five people; no direct reports established.",
        changes_made=["Merged repetition; retained the correction."],
    )
    with (
        patch("findajob.profile.copilot.structured_model"),
        patch("findajob.profile.copilot.invoke_structured", return_value=proposed) as invoke,
    ):
        result = chat_with_memo_copilot(
            [MemoChatMessage(role="user", content="Compact this memo.")], markdown
        )
    assert result == proposed
    messages = invoke.call_args.kwargs["messages"]
    assert messages[1] == ("user", f"=== CURRENT CAREER MEMO ===\n{markdown}")
    assert messages[-1] == ("user", "Compact this memo.")


def test_memo_copilot_rejects_oversized_context_before_model_call():
    with patch("findajob.profile.copilot.structured_model") as model:
        with pytest.raises(ValueError, match="no source text has been truncated"):
            chat_with_memo_copilot([MemoChatMessage(role="user", content="Compact")], "x" * 90001)
    model.assert_not_called()


@pytest.mark.parametrize("markdown", ["", "   "])
def test_memo_copilot_rejects_empty_replacement(markdown: str):
    with (
        patch("findajob.profile.copilot.structured_model"),
        patch(
            "findajob.profile.copilot.invoke_structured",
            return_value=MemoChatResult(reply="Edited", updated_markdown=markdown),
        ),
        pytest.raises(ValueError, match="empty memo"),
    ):
        chat_with_memo_copilot([MemoChatMessage(role="user", content="Compact")], "Original")
