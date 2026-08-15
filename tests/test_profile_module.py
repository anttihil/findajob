"""Profile: canonicalization, rendering, the adapter, and persistence.

These cover the parts where a bug is silent rather than loud. Every one of them was a real
defect during the rewrite:

  - skill keys that do not match the taxonomy, so the keyword layer sees an empty profile
  - canonicalizing a prose label before an already-valid key, inventing skills
  - a prompt prefix that varies between renders, destroying the prefix cache
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from collections.abc import Sequence
from typing import Any, cast

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.database import Database
from careerradar.core.migrations import migrate
from careerradar.profile.adapter import ProfileAdapter
from careerradar.profile.canonicalize import canonicalize_skills
from careerradar.profile.models import (
    LEVEL_CLAIMED,
    LEVEL_MENTIONED,
    LEVEL_STRONG,
    Constraints,
    Profile,
    Skill,
)
from careerradar.profile.render import render_profile
from careerradar.taxonomy.skills import load_taxonomy


def skill(
    key: str, label: str | None = None, level: int = LEVEL_CLAIMED, evidence: str = "test"
) -> Skill:
    return Skill(key=key, label=label or key, level=level, evidence=evidence)


class CanonicalizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tax = load_taxonomy()

    def test_off_taxonomy_keys_are_mapped_to_canonical_ones(self) -> None:
        skills, report = canonicalize_skills([skill("reactjs", "ReactJS", LEVEL_STRONG)], self.tax)
        self.assertEqual(skills[0].key, "react")
        self.assertIn(("reactjs", "react"), report["mapped"])

    def test_an_already_canonical_key_is_never_rewritten(self) -> None:
        """The bug this exists for: a prose label naming two skills.

        `claude_api` is a real taxonomy key, but its label mentions AWS Bedrock. Reading
        the label first returned ['aws', 'claude_api'] and took the first, so the profile
        gained AWS at Claude's level and lost Claude entirely.
        """
        skills, report = canonicalize_skills(
            [skill("claude_api", "Claude API (via AWS Bedrock)", LEVEL_CLAIMED)], self.tax
        )
        self.assertEqual(skills[0].key, "claude_api")
        self.assertEqual(report["mapped"], [])

    def test_terraform_is_not_rewritten_to_its_config_language(self) -> None:
        skills, _ = canonicalize_skills([skill("terraform", "Terraform / HCL")], self.tax)
        self.assertEqual(skills[0].key, "terraform")

    def test_unknown_skills_are_kept_rather_than_dropped(self) -> None:
        skills, report = canonicalize_skills(
            [skill("some_internal_tool", "Some Internal Tool")], self.tax
        )
        self.assertEqual([s.key for s in skills], ["some_internal_tool"])
        self.assertIn("some_internal_tool", report["unmatched"])

    def test_collisions_keep_the_stronger_claim_and_merge_evidence(self) -> None:
        skills, report = canonicalize_skills(
            [
                skill("react", "React", LEVEL_MENTIONED, evidence="mentioned once"),
                skill("reactjs", "ReactJS", LEVEL_STRONG, evidence="shipped an app"),
            ],
            self.tax,
        )
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].level, LEVEL_STRONG)
        self.assertIn("shipped an app", skills[0].evidence)
        self.assertIn("mentioned once", skills[0].evidence)
        self.assertIn("react", report["merged"])

    def test_every_key_in_a_canonicalized_profile_resolves(self) -> None:
        """The invariant the keyword layer depends on."""
        skills, _ = canonicalize_skills(
            [
                skill("reactjs", "ReactJS"),
                skill("d3js", "D3.js"),
                skill("ocrmypdf", "OCR (ocrmypdf)"),
                skill("python", "Python"),
            ],
            self.tax,
        )
        resolvable = [s.key for s in skills if s.key in self.tax]
        self.assertEqual(len(resolvable), len(skills))


class RenderTests(unittest.TestCase):
    def profile(self):
        return Profile(
            bio="An engineer.",
            years_experience=4,
            seniority="mid",
            skills=[
                skill("python", "Python", LEVEL_STRONG),
                skill("docker", "Docker", LEVEL_CLAIMED),
                skill("go", "Go", LEVEL_MENTIONED),
            ],
            strengths=["ships end to end"],
            weaknesses=["no observability experience"],
            constraints=Constraints(work_authorization=["United States"], comp_floor_usd=150000),
            non_negotiables=["no on-site relocation"],
        )

    def test_render_is_byte_stable_across_calls(self) -> None:
        """The prefix cache matches bytes. Instability is a ~50x input-cost regression."""
        profile = self.profile()
        self.assertEqual(render_profile(profile), render_profile(profile))

    def test_render_is_stable_under_skill_reordering(self) -> None:
        """Two profiles with the same skills in a different order must render identically.

        Otherwise the prefix changes whenever the model happens to emit skills in a
        different order, and every run pays the uncached rate.
        """
        a = self.profile()
        b = a.model_copy(update={"skills": list(reversed(a.skills))})
        self.assertEqual(render_profile(a), render_profile(b))

    def test_render_carries_the_parts_the_scorer_needs(self) -> None:
        text = render_profile(self.profile())
        self.assertIn("Python", text)
        self.assertIn("HONEST GAPS", text)  # so a stretch reads as a stretch
        self.assertIn("no observability experience", text)
        self.assertIn("HARD CONSTRAINTS", text)  # so a blocker is a blocker
        self.assertIn("$150,000", text)
        self.assertIn("NON-NEGOTIABLE", text)

    def test_render_contains_no_clock(self) -> None:
        """A timestamp anywhere in the prefix invalidates the cache on every call."""
        text = render_profile(self.profile())
        for marker in ("2026", "2025", "T00:", "GMT", "UTC"):
            self.assertNotIn(marker, text)


class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tax = load_taxonomy()
        self.adapter = ProfileAdapter(
            Profile(
                bio="b",
                skills=[
                    skill("python", "Python", LEVEL_STRONG),
                    skill("docker", "Docker", LEVEL_CLAIMED),
                    skill("go", "Go", LEVEL_MENTIONED),
                ],
            ),
            version=7,
            taxonomy=self.tax,
        )

    def test_level_and_has_match_the_old_semantics(self) -> None:
        self.assertEqual(self.adapter.level("python"), LEVEL_STRONG)
        self.assertEqual(self.adapter.level("nonexistent"), 0)
        self.assertTrue(self.adapter.has("go"))
        self.assertTrue(self.adapter.has("python", LEVEL_STRONG))
        self.assertFalse(self.adapter.has("go", LEVEL_CLAIMED))

    def test_keys_filters_by_level(self) -> None:
        self.assertEqual(self.adapter.keys(LEVEL_STRONG), {"python"})
        self.assertEqual(self.adapter.keys(LEVEL_MENTIONED), {"python", "docker", "go"})

    def test_supports_len_and_contains(self) -> None:
        self.assertEqual(len(self.adapter), 3)
        self.assertIn("python", self.adapter)
        self.assertNotIn("rust", self.adapter)

    def test_evidence_is_available_for_gap_analysis(self) -> None:
        self.assertEqual(self.adapter.evidence("python"), ["test"])
        self.assertEqual(self.adapter.evidence("absent"), [])


class StoreTests(unittest.TestCase):
    """Round-trip against a real migrated database."""

    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)
        self.conn.row_factory = sqlite3.Row
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self.tmp.name)

    class _Db:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def close(self) -> None:
            pass

    def _db(self) -> Database:
        # `_Db` is intentionally not a `Database` -- a minimal duck-typed stand-in so the
        # round trip does not need a second real connection to the same file.
        return cast(Database, self._Db(self.conn))

    def test_saving_twice_leaves_exactly_one_active_profile(self) -> None:
        from careerradar.profile.store import load_active, save_profile

        db = self._db()
        tax = load_taxonomy()
        profile = Profile(bio="v1", skills=[skill("python", "Python", LEVEL_STRONG)])

        v1 = save_profile(profile, model="m", db=db, taxonomy=tax)
        v2 = save_profile(profile.model_copy(update={"bio": "v2"}), model="m", db=db, taxonomy=tax)
        self.assertEqual((v1, v2), (1, 2))

        active = self.conn.execute("SELECT COUNT(*) FROM profiles WHERE is_active = 1").fetchone()[
            0
        ]
        self.assertEqual(active, 1)

        version, loaded, summary = cast("tuple[int, Profile, str]", load_active(db=db))
        self.assertEqual(version, 2)
        self.assertEqual(loaded.bio, "v2")
        self.assertIn("Python", summary)

    def test_summary_text_is_persisted_not_recomputed(self) -> None:
        """The stored prefix must survive a round trip byte-for-byte."""
        from careerradar.profile.store import load_active, save_profile

        db = self._db()
        profile = Profile(bio="b", skills=[skill("python", "Python", LEVEL_STRONG)])
        save_profile(profile, model="m", db=db, taxonomy=load_taxonomy())
        _, loaded, summary = cast("tuple[int, Profile, str]", load_active(db=db))
        self.assertEqual(summary, render_profile(loaded))

    def test_save_canonicalizes_so_bad_keys_cannot_be_persisted(self) -> None:
        from careerradar.profile.store import load_active, save_profile

        db = self._db()
        save_profile(
            Profile(bio="b", skills=[skill("reactjs", "ReactJS", LEVEL_STRONG)]),
            model="m",
            db=db,
            taxonomy=load_taxonomy(),
        )
        _, loaded, _ = cast("tuple[int, Profile, str]", load_active(db=db))
        self.assertEqual([s.key for s in loaded.skills], ["react"])


if __name__ == "__main__":
    unittest.main()


class CorpusTests(unittest.TestCase):
    """The corpus is an explicit list. A missing document must fail loudly.

    A silently-skipped document changes every skill level and therefore every score, and
    the only symptom is a profile that looks plausible but is thinner than it should be.
    """

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        with open(os.path.join(self.root, "achievements.md"), "w") as fh:
            fh.write("# Achievements\nShipped a platform.\n")
        os.makedirs(os.path.join(self.root, "resumes"))
        with open(os.path.join(self.root, "resumes", "curated.txt"), "w") as fh:
            fh.write("Jane Doe\nSoftware engineer.\n")

    def config(self, corpus: list[Any]) -> dict[str, Any]:
        return {"profile": {"corpus": corpus}}

    def test_reads_exactly_the_named_documents(self) -> None:
        from careerradar.profile.ingest import collect_documents

        docs = collect_documents(
            self.config(
                [
                    {"path": "achievements.md", "kind": "achievements"},
                    {"path": "resumes/curated.txt", "kind": "resume"},
                ]
            ),
            repo_root=self.root,
        )
        self.assertEqual([d.name for d in docs], ["achievements.md", "curated.txt"])
        self.assertEqual([d.kind for d in docs], ["achievements", "resume"])

    def test_an_unnamed_file_in_resumes_is_not_read(self) -> None:
        """The whole point: dropping a file into resumes/ must not change the profile."""
        from careerradar.profile.ingest import collect_documents

        with open(os.path.join(self.root, "resumes", "llm_generated.md"), "w") as fh:
            fh.write("# Inflated\nExpert in everything.\n")
        docs = collect_documents(
            self.config([{"path": "resumes/curated.txt"}]), repo_root=self.root
        )
        self.assertEqual([d.name for d in docs], ["curated.txt"])

    def test_a_missing_named_document_raises(self) -> None:
        from careerradar.profile.ingest import CorpusError, collect_documents

        with self.assertRaises(CorpusError) as caught:
            collect_documents(self.config([{"path": "resumes/gone.md"}]), repo_root=self.root)
        self.assertIn("resumes/gone.md", str(caught.exception))

    def test_a_document_marked_optional_may_be_absent(self) -> None:
        from careerradar.profile.ingest import collect_documents

        docs = collect_documents(
            self.config(
                [
                    {"path": "achievements.md"},
                    {"path": "resumes/gone.md", "optional": True},
                ]
            ),
            repo_root=self.root,
        )
        self.assertEqual(len(docs), 1)

    def test_an_empty_document_raises_rather_than_contributing_nothing(self) -> None:
        from careerradar.profile.ingest import CorpusError, collect_documents

        open(os.path.join(self.root, "resumes", "blank.txt"), "w").close()
        with self.assertRaises(CorpusError):
            collect_documents(self.config([{"path": "resumes/blank.txt"}]), repo_root=self.root)

    def test_a_bare_string_entry_is_accepted(self) -> None:
        from careerradar.profile.ingest import collect_documents

        docs = collect_documents(self.config(["achievements.md"]), repo_root=self.root)
        self.assertEqual([d.name for d in docs], ["achievements.md"])

    def test_corpus_hash_is_stable_under_ordering(self) -> None:
        from careerradar.profile.ingest import collect_documents, corpus_hash

        a = collect_documents(
            self.config(["achievements.md", "resumes/curated.txt"]), repo_root=self.root
        )
        b = collect_documents(
            self.config(["resumes/curated.txt", "achievements.md"]), repo_root=self.root
        )
        self.assertEqual(corpus_hash(a), corpus_hash(b))

    def test_corpus_hash_changes_when_content_changes(self) -> None:
        from careerradar.profile.ingest import collect_documents, corpus_hash

        spec = self.config(["achievements.md"])
        before = corpus_hash(collect_documents(spec, repo_root=self.root))
        with open(os.path.join(self.root, "achievements.md"), "a") as fh:
            fh.write("\nAnd another thing.\n")
        after = corpus_hash(collect_documents(spec, repo_root=self.root))
        self.assertNotEqual(before, after)


class StructuredOutputTests(unittest.TestCase):
    """The V4 failure that took down a finished interview.

    A forced tool choice is not a guarantee: V4 sometimes answers one with prose anyway,
    and `with_structured_output` reports that as a `None` parse. The graph then called
    `.model_dump()` on it and died with an AttributeError three frames from the cause,
    after fifteen answered questions.
    """

    def chain(self, responses: list[dict[str, Any]]) -> tuple[Any, Any]:
        from unittest import mock

        chain = mock.Mock()
        chain.invoke.side_effect = list(responses)
        model = mock.Mock()
        model.with_structured_output.return_value = chain
        return model, chain

    def prose(self, text: str = "Here is the profile you asked for.") -> dict[str, Any]:
        from unittest import mock

        raw = mock.Mock(
            response_metadata={"finish_reason": "stop"}, invalid_tool_calls=[], content=text
        )
        return {"parsed": None, "raw": raw, "parsing_error": None}

    def parsed(self) -> dict[str, Any]:
        from unittest import mock

        return {
            "parsed": Profile(bio="A engineer."),
            "raw": mock.Mock(response_metadata={"finish_reason": "tool_calls"}),
            "parsing_error": None,
        }

    def test_a_missing_tool_call_is_retried_rather_than_returned_as_none(self) -> None:
        from careerradar.core.llm import invoke_structured

        model, chain = self.chain([self.prose(), self.parsed()])
        result = invoke_structured(model, Profile, [("user", "go")], label="test")

        self.assertIsInstance(result, Profile)
        self.assertEqual(chain.invoke.call_count, 2)

    def test_the_retry_carries_a_nudge_the_first_attempt_did_not(self) -> None:
        from careerradar.core.llm import STRUCTURED_RETRY_NUDGE, invoke_structured

        model, chain = self.chain([self.prose(), self.parsed()])
        invoke_structured(model, Profile, [("user", "go")], label="test")

        first, second = (c.args[0] for c in chain.invoke.call_args_list)
        self.assertEqual(len(first), 1)
        self.assertEqual(second[-1], ("user", STRUCTURED_RETRY_NUDGE))

    def test_exhausting_the_retries_raises_instead_of_returning_none(self) -> None:
        from careerradar.core.llm import StructuredOutputError, invoke_structured

        model, chain = self.chain([self.prose("Sure, here goes.")] * 3)
        with self.assertRaises(StructuredOutputError) as caught:
            invoke_structured(model, Profile, [("user", "go")], label="Profile synthesize")

        self.assertEqual(chain.invoke.call_count, 3)
        # The message has to carry both halves: which step, and why it gave up.
        self.assertIn("Profile synthesize", str(caught.exception))
        self.assertIn("Sure, here goes.", str(caught.exception))

    def test_a_truncated_tool_call_is_reported_as_truncation_not_as_prose(self) -> None:
        """Retrying fixes prose; it does not fix a schema too big for the token budget."""
        from unittest import mock

        from careerradar.core.llm import _no_tool_call_reason

        raw = mock.Mock(
            response_metadata={"finish_reason": "length"}, invalid_tool_calls=[], content=""
        )
        self.assertIn("output token limit", _no_tool_call_reason(raw))

    def test_synthesize_surfaces_the_error_instead_of_an_attributeerror(self) -> None:
        from unittest import mock

        from careerradar.core.llm import StructuredOutputError
        from careerradar.profile.graph import ProfileState, node_synthesize

        model, _ = self.chain([self.prose()] * 3)
        state = cast(
            ProfileState,
            {
                "corpus": "docs",
                "model": "deepseek-v4-pro",
                "claims": {
                    "bio": "b",
                    "skills": [],
                    "apparent_strengths": [],
                    "contradictions": [],
                },
                "turns": [{"topic": "comp", "question": "?", "answer": "90k"}],
            },
        )
        with mock.patch("careerradar.profile.graph.structured_model", return_value=model):
            with self.assertRaises(StructuredOutputError):
                node_synthesize(state)


class FakeSnapshot:
    def __init__(self, next_nodes: tuple[str, ...], values: dict[str, Any]) -> None:
        self.next = tuple(next_nodes)
        self.values = values


class FakeGraph:
    """Records what `cmd_build` hands to `stream`, which is the whole question."""

    def __init__(
        self,
        parked_at: tuple[str, ...] = (),
        turns: Sequence[dict[str, Any]] = (),
    ) -> None:
        self.stream_inputs: list[Any] = []
        self.parked_at = parked_at
        self.turns = list(turns)
        self.finished = False

    def stream(self, stream_input: Any, thread: Any, stream_mode: str | None = None) -> Any:
        self.stream_inputs.append(stream_input)
        self.finished = True
        return iter(())

    def get_state(self, thread: Any) -> FakeSnapshot:
        if self.finished:
            return FakeSnapshot(
                (),
                {"approved": True, "turns": self.turns, "draft": Profile(bio="Done.").model_dump()},
            )
        return FakeSnapshot(self.parked_at, {"turns": self.turns})


class ResumeTests(unittest.TestCase):
    """`--resume` must continue the parked task, not re-enter the graph at START.

    Passing the input payload again re-runs ingest and extract, generates a *new* set of
    questions, and asks all of them -- appending the answers to the ones already recorded,
    because `turns` reduces by concatenation. The interview it was meant to rescue is the
    thing it destroys.
    """

    def build(self, graph: Any, **flags: Any) -> int:
        import argparse
        from unittest import mock

        from careerradar.profile.cli import cmd_build

        defaults = {"subcommand": "build", "resume": False, "force": True, "no_interview": False}
        args = argparse.Namespace(**{**defaults, **flags})
        document = mock.Mock(kind="resume", name="cv.md")
        with (
            mock.patch("careerradar.profile.cli.load_config", return_value={}),
            mock.patch("careerradar.profile.ingest.collect_documents", return_value=[document]),
            mock.patch("careerradar.profile.store.corpus_changed", return_value=True),
            mock.patch("careerradar.profile.store.save_profile", return_value=3),
            mock.patch("careerradar.profile.graph.open_checkpointer", return_value=mock.Mock()),
            mock.patch("careerradar.profile.graph.build_graph", return_value=graph),
        ):
            return cmd_build(args)

    def test_resume_continues_the_parked_task_rather_than_restarting(self) -> None:
        graph = FakeGraph(
            parked_at=("synthesize",), turns=[{"topic": "comp", "question": "?", "answer": "90k"}]
        )
        code = self.build(graph, resume=True)

        self.assertEqual(code, 0)
        # `None` is LangGraph's "continue the pending task"; a dict re-enters at START.
        self.assertEqual(graph.stream_inputs, [None])

    def test_a_fresh_build_still_passes_the_payload(self) -> None:
        graph = FakeGraph()
        self.build(graph)

        self.assertEqual(len(graph.stream_inputs), 1)
        self.assertIsInstance(graph.stream_inputs[0], dict)
        self.assertIn("max_questions", graph.stream_inputs[0])

    def test_resume_with_nothing_parked_reports_it_instead_of_rebuilding(self) -> None:
        graph = FakeGraph(parked_at=())
        code = self.build(graph, resume=True)

        self.assertEqual(code, 1)
        self.assertEqual(graph.stream_inputs, [])


class LevelDiffTests(unittest.TestCase):
    """The review screen's answer to "did answering fifteen questions change anything?".

    The profile that ignored an interview renders exactly like one that used it, so the
    silent no -- synthesis echoing the extracted levels straight back -- is the case these
    cover most carefully.
    """

    def diff(self, claim_levels: dict[str, int], draft_levels: dict[str, int]) -> dict[str, Any]:
        from careerradar.profile.graph import level_changes

        claims = {
            "skills": [
                {"key": k, "label": k.title(), "level": v, "evidence": "d"}
                for k, v in claim_levels.items()
            ]
        }
        draft = {
            "skills": [
                {"key": k, "label": k.title(), "level": v, "evidence": "i"}
                for k, v in draft_levels.items()
            ]
        }
        return level_changes(claims, draft)

    def test_a_raised_level_is_reported_with_both_ends(self) -> None:
        changes = self.diff({"selenium": 1}, {"selenium": 2})
        self.assertEqual(
            changes["raised"], [{"key": "selenium", "label": "Selenium", "from": 1, "to": 2}]
        )
        self.assertEqual(changes["lowered"], [])

    def test_a_lowered_level_is_not_quietly_folded_in_with_the_raises(self) -> None:
        """An interview that walks a claim back is the most important thing on the screen."""
        changes = self.diff({"kubernetes": 3}, {"kubernetes": 1})
        self.assertEqual(changes["lowered"][0]["from"], 3)
        self.assertEqual(changes["raised"], [])

    def test_a_skill_the_interview_introduced_counts_as_added_not_raised(self) -> None:
        changes = self.diff({}, {"selenium": 2})
        self.assertEqual([c["key"] for c in changes["added"]], ["selenium"])
        self.assertEqual(changes["raised"], [])

    def test_synthesis_that_echoes_the_claims_reports_nothing_moved(self) -> None:
        changes = self.diff({"go": 1, "python": 3}, {"go": 1, "python": 3})
        self.assertEqual((changes["raised"], changes["lowered"], changes["added"]), ([], [], []))
        self.assertEqual(changes["total"], 2)

    def test_the_no_change_case_says_so_in_words_a_reviewer_can_act_on(self) -> None:
        from careerradar.profile.cli import _render_level_changes

        rendered = _render_level_changes(self.diff({"go": 1}, {"go": 1}))
        self.assertIn("None", rendered)
        self.assertIn("re-synthesized", rendered)

    def test_changes_render_with_level_names_rather_than_numbers(self) -> None:
        from careerradar.profile.cli import _render_level_changes

        rendered = _render_level_changes(self.diff({"selenium": 1}, {"selenium": 2}))
        self.assertIn("familiar -> working", rendered)
        self.assertNotIn("1 -> 2", rendered)

    def test_missing_claims_do_not_break_the_review_screen(self) -> None:
        from careerradar.profile.graph import level_changes

        changes = level_changes(
            None, {"skills": [{"key": "go", "label": "Go", "level": 2, "evidence": "i"}]}
        )
        self.assertEqual(len(changes["added"]), 1)

    def test_the_review_interrupt_carries_the_diff(self) -> None:
        from unittest import mock

        import careerradar.profile.graph as graph_module

        captured: dict[str, Any] = {}

        def fake_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
            captured.update(payload)
            return {"approve": True}

        state = cast(
            graph_module.ProfileState,
            {
                "draft": {
                    "skills": [
                        {
                            "key": "selenium",
                            "label": "Selenium",
                            "level": 2,
                            "evidence": "interview",
                        }
                    ]
                },
                "claims": {
                    "skills": [
                        {"key": "selenium", "label": "Selenium", "level": 1, "evidence": "doc"}
                    ]
                },
            },
        )
        with mock.patch.object(graph_module, "interrupt", fake_interrupt):
            result = graph_module.node_review(state)

        self.assertTrue(result["approved"])
        self.assertEqual(captured["level_changes"]["raised"][0]["to"], 2)
