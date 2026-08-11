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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.migrations import migrate  # noqa: E402
from careerradar.profile.adapter import ProfileAdapter  # noqa: E402
from careerradar.profile.canonicalize import canonicalize_skills  # noqa: E402
from careerradar.profile.models import (  # noqa: E402
    LEVEL_CLAIMED,
    LEVEL_MENTIONED,
    LEVEL_STRONG,
    Constraints,
    Profile,
    Skill,
)
from careerradar.profile.render import render_profile  # noqa: E402
from careerradar.taxonomy.skills import load_taxonomy  # noqa: E402


def skill(key, label=None, level=LEVEL_CLAIMED, evidence="test"):
    return Skill(key=key, label=label or key, level=level, evidence=evidence)


class CanonicalizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tax = load_taxonomy()

    def test_off_taxonomy_keys_are_mapped_to_canonical_ones(self):
        skills, report = canonicalize_skills(
            [skill("reactjs", "ReactJS", LEVEL_STRONG)], self.tax
        )
        self.assertEqual(skills[0].key, "react")
        self.assertIn(("reactjs", "react"), report["mapped"])

    def test_an_already_canonical_key_is_never_rewritten(self):
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

    def test_terraform_is_not_rewritten_to_its_config_language(self):
        skills, _ = canonicalize_skills([skill("terraform", "Terraform / HCL")], self.tax)
        self.assertEqual(skills[0].key, "terraform")

    def test_unknown_skills_are_kept_rather_than_dropped(self):
        skills, report = canonicalize_skills(
            [skill("some_internal_tool", "Some Internal Tool")], self.tax
        )
        self.assertEqual([s.key for s in skills], ["some_internal_tool"])
        self.assertIn("some_internal_tool", report["unmatched"])

    def test_collisions_keep_the_stronger_claim_and_merge_evidence(self):
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

    def test_every_key_in_a_canonicalized_profile_resolves(self):
        """The invariant the keyword layer depends on."""
        skills, _ = canonicalize_skills(
            [skill("reactjs", "ReactJS"), skill("d3js", "D3.js"),
             skill("ocrmypdf", "OCR (ocrmypdf)"), skill("python", "Python")],
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
            constraints=Constraints(work_authorization=["United States"],
                                    comp_floor_usd=150000),
            non_negotiables=["no on-site relocation"],
        )

    def test_render_is_byte_stable_across_calls(self):
        """The prefix cache matches bytes. Instability is a ~50x input-cost regression."""
        profile = self.profile()
        self.assertEqual(render_profile(profile), render_profile(profile))

    def test_render_is_stable_under_skill_reordering(self):
        """Two profiles with the same skills in a different order must render identically.

        Otherwise the prefix changes whenever the model happens to emit skills in a
        different order, and every run pays the uncached rate.
        """
        a = self.profile()
        b = a.model_copy(update={"skills": list(reversed(a.skills))})
        self.assertEqual(render_profile(a), render_profile(b))

    def test_render_carries_the_parts_the_scorer_needs(self):
        text = render_profile(self.profile())
        self.assertIn("Python", text)
        self.assertIn("HONEST GAPS", text)          # so a stretch reads as a stretch
        self.assertIn("no observability experience", text)
        self.assertIn("HARD CONSTRAINTS", text)     # so a blocker is a blocker
        self.assertIn("$150,000", text)
        self.assertIn("NON-NEGOTIABLE", text)

    def test_render_contains_no_clock(self):
        """A timestamp anywhere in the prefix invalidates the cache on every call."""
        text = render_profile(self.profile())
        for marker in ("2026", "2025", "T00:", "GMT", "UTC"):
            self.assertNotIn(marker, text)


class AdapterTests(unittest.TestCase):
    def setUp(self):
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

    def test_level_and_has_match_the_old_semantics(self):
        self.assertEqual(self.adapter.level("python"), LEVEL_STRONG)
        self.assertEqual(self.adapter.level("nonexistent"), 0)
        self.assertTrue(self.adapter.has("go"))
        self.assertTrue(self.adapter.has("python", LEVEL_STRONG))
        self.assertFalse(self.adapter.has("go", LEVEL_CLAIMED))

    def test_keys_filters_by_level(self):
        self.assertEqual(self.adapter.keys(LEVEL_STRONG), {"python"})
        self.assertEqual(self.adapter.keys(LEVEL_MENTIONED), {"python", "docker", "go"})

    def test_supports_len_and_contains(self):
        self.assertEqual(len(self.adapter), 3)
        self.assertIn("python", self.adapter)
        self.assertNotIn("rust", self.adapter)

    def test_evidence_is_available_for_gap_analysis(self):
        self.assertEqual(self.adapter.evidence("python"), ["test"])
        self.assertEqual(self.adapter.evidence("absent"), [])


class StoreTests(unittest.TestCase):
    """Round-trip against a real migrated database."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)
        self.conn.row_factory = sqlite3.Row
        migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    class _Db:
        def __init__(self, conn):
            self.conn = conn

        def close(self):
            pass

    def test_saving_twice_leaves_exactly_one_active_profile(self):
        from careerradar.profile.store import load_active, save_profile

        db = self._Db(self.conn)
        tax = load_taxonomy()
        profile = Profile(bio="v1", skills=[skill("python", "Python", LEVEL_STRONG)])

        v1 = save_profile(profile, model="m", db=db, taxonomy=tax)
        v2 = save_profile(profile.model_copy(update={"bio": "v2"}),
                          model="m", db=db, taxonomy=tax)
        self.assertEqual((v1, v2), (1, 2))

        active = self.conn.execute(
            "SELECT COUNT(*) FROM profiles WHERE is_active = 1"
        ).fetchone()[0]
        self.assertEqual(active, 1)

        version, loaded, summary = load_active(db=db)
        self.assertEqual(version, 2)
        self.assertEqual(loaded.bio, "v2")
        self.assertIn("Python", summary)

    def test_summary_text_is_persisted_not_recomputed(self):
        """The stored prefix must survive a round trip byte-for-byte."""
        from careerradar.profile.store import load_active, save_profile

        db = self._Db(self.conn)
        profile = Profile(bio="b", skills=[skill("python", "Python", LEVEL_STRONG)])
        save_profile(profile, model="m", db=db, taxonomy=load_taxonomy())
        _, loaded, summary = load_active(db=db)
        self.assertEqual(summary, render_profile(loaded))

    def test_save_canonicalizes_so_bad_keys_cannot_be_persisted(self):
        from careerradar.profile.store import load_active, save_profile

        db = self._Db(self.conn)
        save_profile(
            Profile(bio="b", skills=[skill("reactjs", "ReactJS", LEVEL_STRONG)]),
            model="m", db=db, taxonomy=load_taxonomy(),
        )
        _, loaded, _ = load_active(db=db)
        self.assertEqual([s.key for s in loaded.skills], ["react"])


if __name__ == "__main__":
    unittest.main()


class CorpusTests(unittest.TestCase):
    """The corpus is an explicit list. A missing document must fail loudly.

    A silently-skipped document changes every skill level and therefore every score, and
    the only symptom is a profile that looks plausible but is thinner than it should be.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        with open(os.path.join(self.root, "achievements.md"), "w") as fh:
            fh.write("# Achievements\nShipped a platform.\n")
        os.makedirs(os.path.join(self.root, "resumes"))
        with open(os.path.join(self.root, "resumes", "curated.txt"), "w") as fh:
            fh.write("Jane Doe\nSoftware engineer.\n")

    def config(self, corpus):
        return {"profile": {"corpus": corpus}}

    def test_reads_exactly_the_named_documents(self):
        from careerradar.profile.ingest import collect_documents

        docs = collect_documents(
            self.config([
                {"path": "achievements.md", "kind": "achievements"},
                {"path": "resumes/curated.txt", "kind": "resume"},
            ]),
            repo_root=self.root,
        )
        self.assertEqual([d.name for d in docs], ["achievements.md", "curated.txt"])
        self.assertEqual([d.kind for d in docs], ["achievements", "resume"])

    def test_an_unnamed_file_in_resumes_is_not_read(self):
        """The whole point: dropping a file into resumes/ must not change the profile."""
        from careerradar.profile.ingest import collect_documents

        with open(os.path.join(self.root, "resumes", "llm_generated.md"), "w") as fh:
            fh.write("# Inflated\nExpert in everything.\n")
        docs = collect_documents(
            self.config([{"path": "resumes/curated.txt"}]), repo_root=self.root
        )
        self.assertEqual([d.name for d in docs], ["curated.txt"])

    def test_a_missing_named_document_raises(self):
        from careerradar.profile.ingest import CorpusError, collect_documents

        with self.assertRaises(CorpusError) as caught:
            collect_documents(
                self.config([{"path": "resumes/gone.md"}]), repo_root=self.root
            )
        self.assertIn("resumes/gone.md", str(caught.exception))

    def test_a_document_marked_optional_may_be_absent(self):
        from careerradar.profile.ingest import collect_documents

        docs = collect_documents(
            self.config([
                {"path": "achievements.md"},
                {"path": "resumes/gone.md", "optional": True},
            ]),
            repo_root=self.root,
        )
        self.assertEqual(len(docs), 1)

    def test_an_empty_document_raises_rather_than_contributing_nothing(self):
        from careerradar.profile.ingest import CorpusError, collect_documents

        open(os.path.join(self.root, "resumes", "blank.txt"), "w").close()
        with self.assertRaises(CorpusError):
            collect_documents(
                self.config([{"path": "resumes/blank.txt"}]), repo_root=self.root
            )

    def test_a_bare_string_entry_is_accepted(self):
        from careerradar.profile.ingest import collect_documents

        docs = collect_documents(self.config(["achievements.md"]), repo_root=self.root)
        self.assertEqual([d.name for d in docs], ["achievements.md"])

    def test_corpus_hash_is_stable_under_ordering(self):
        from careerradar.profile.ingest import collect_documents, corpus_hash

        a = collect_documents(
            self.config(["achievements.md", "resumes/curated.txt"]), repo_root=self.root
        )
        b = collect_documents(
            self.config(["resumes/curated.txt", "achievements.md"]), repo_root=self.root
        )
        self.assertEqual(corpus_hash(a), corpus_hash(b))

    def test_corpus_hash_changes_when_content_changes(self):
        from careerradar.profile.ingest import collect_documents, corpus_hash

        spec = self.config(["achievements.md"])
        before = corpus_hash(collect_documents(spec, repo_root=self.root))
        with open(os.path.join(self.root, "achievements.md"), "a") as fh:
            fh.write("\nAnd another thing.\n")
        after = corpus_hash(collect_documents(spec, repo_root=self.root))
        self.assertNotEqual(before, after)
