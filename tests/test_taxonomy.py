"""Taxonomy tests, centred on a false-positive gauntlet.

Naive `\\b`-alternation skill matching fails in two directions, and both are represented
here as explicit cases:

  False negatives -- `\\bc\\+\\+\\b` never matches "C++", because there is no word character
  after the plus signs for `\\b` to anchor against. Same for "C#" and ".NET".

  False positives -- "go to market", "R&D", "a ray of hope", and "spark joy" all appear in
  real job ads. Counting them as Go, R, Ray, and Spark would corrupt demand estimates for
  four skills at once, and would do so invisibly.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.taxonomy.skills import load_taxonomy


class TaxonomyIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tax = load_taxonomy()

    def test_validates_clean(self) -> None:
        problems = self.tax.validate()
        self.assertEqual(problems, [], f"taxonomy problems: {problems}")

    def test_has_meaningful_size(self) -> None:
        self.assertGreater(len(self.tax), 100)

    def test_hash_is_stable_and_content_derived(self) -> None:
        self.assertEqual(self.tax.hash, load_taxonomy().hash)
        self.assertEqual(len(self.tax.hash), 16)

    def test_every_skill_has_a_known_category(self) -> None:
        for skill in self.tax.skills.values():
            self.assertIn(skill.category, self.tax.categories)

    def test_strict_aliases_always_have_context(self) -> None:
        """A strict alias without context words would match on capitalization alone."""
        for skill in self.tax.skills.values():
            if skill.strict_aliases:
                self.assertTrue(skill.context, f"{skill.key} has strict_aliases but no context")


class LiteralMatchTests(unittest.TestCase):
    """Tokens that word-boundary matching cannot express."""

    def setUp(self) -> None:
        self.tax = load_taxonomy()

    def _found(self, text: str) -> set[str]:
        return set(self.tax.extract(text))

    def test_cplusplus_is_found(self) -> None:
        self.assertIn("cplusplus", self._found("Strong C++ experience required."))
        self.assertIn("cplusplus", self._found("Experience with CPP and embedded work."))

    def test_csharp_is_found(self) -> None:
        self.assertIn("csharp", self._found("We build in C# on .NET 8."))
        self.assertIn("csharp", self._found("C Sharp developer wanted"))

    def test_dotnet_is_found(self) -> None:
        self.assertIn("dotnet", self._found("ASP.NET Core services"))
        self.assertIn("dotnet", self._found("Built on .NET and Azure"))

    def test_cplusplus_does_not_leak_into_csharp(self) -> None:
        found = self._found("Strong C++ experience required.")
        self.assertNotIn("csharp", found)

    def test_plain_c_plus_plus_not_matched_as_something_else(self) -> None:
        """ "C++" must not be read as a bare "C" mention producing a different skill."""
        found = self._found("C++ only, no managed languages.")
        self.assertIn("cplusplus", found)


class FalsePositiveGauntletTests(unittest.TestCase):
    """Phrases lifted from the shape of real job ads that must NOT produce a match."""

    def setUp(self) -> None:
        self.tax = load_taxonomy()

    def _found(self, text: str) -> set[str]:
        return set(self.tax.extract(text))

    def test_go_the_verb_is_not_the_go_language(self) -> None:
        for phrase in [
            "We go to market fast and iterate.",
            "Ready to go the extra mile for our customers.",
            "You will go deep on customer problems.",
            "Let's go build something great.",
        ]:
            self.assertNotIn("golang", self._found(phrase), phrase)

    def test_go_the_language_is_found_with_context(self) -> None:
        for phrase in [
            "Backend services written in Go with goroutines and gRPC.",
            "Experience with Golang required.",
            "Go microservices using the Gin framework.",
        ]:
            self.assertIn("golang", self._found(phrase), phrase)

    def test_r_and_d_is_not_the_r_language(self) -> None:
        for phrase in [
            "Join our R&D team.",
            "This is an R&D-heavy role.",
        ]:
            self.assertNotIn("r_lang", self._found(phrase), phrase)

    def test_r_the_language_is_found_with_context(self) -> None:
        self.assertIn(
            "r_lang",
            self._found("Statistical modelling in R using tidyverse and ggplot."),
        )

    def test_ray_the_noun_is_not_the_ray_framework(self) -> None:
        for phrase in [
            "A ray of sunshine in a fast-paced team.",
            "Ray Kurzweil is often quoted here.",
        ]:
            self.assertNotIn("ray", self._found(phrase), phrase)

    def test_ray_the_framework_is_found_with_context(self) -> None:
        self.assertIn(
            "ray",
            self._found("Distributed training with Ray Serve on Anyscale."),
        )

    def test_spark_the_verb_is_not_apache_spark(self) -> None:
        for phrase in [
            "Help spark joy in our users.",
            "This role will spark your curiosity.",
        ]:
            self.assertNotIn("spark", self._found(phrase), phrase)

    def test_spark_the_engine_is_found_with_context(self) -> None:
        self.assertIn(
            "spark",
            self._found("Build ETL data pipelines with Apache Spark and PySpark."),
        )

    def test_uv_light_is_not_the_python_packager(self) -> None:
        for phrase in [
            "Experience with UV light sterilisation systems.",
            "UV mapping for 3D assets.",
        ]:
            self.assertNotIn("uv_tool", self._found(phrase), phrase)

    def test_uv_the_packager_is_found_with_context(self) -> None:
        self.assertIn(
            "uv_tool",
            self._found("Python dependency management with uv and pyproject.toml."),
        )

    def test_shell_the_noun_is_not_shell_scripting(self) -> None:
        self.assertNotIn(
            "shell", self._found("We used to work at Shell before founding this company.")
        )

    def test_shell_scripting_is_found_with_context(self) -> None:
        self.assertIn(
            "shell",
            self._found("Comfortable with shell scripting for Linux automation."),
        )

    def test_a_prose_paragraph_with_no_tech_yields_nothing(self) -> None:
        prose = (
            "We are a mission-driven team that values curiosity, kindness, and a bias "
            "toward action. You will go far here if you care about people."
        )
        self.assertEqual(self._found(prose), set())


class ExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tax = load_taxonomy()

    def test_in_title_flag_is_set(self) -> None:
        found = self.tax.extract(
            "We need Kubernetes and Terraform skills.",
            title="Senior Kubernetes Platform Engineer",
        )
        self.assertTrue(found["kubernetes"]["in_title"])
        self.assertFalse(found["terraform"]["in_title"])

    def test_presence_counted_once_regardless_of_repetition(self) -> None:
        once = self.tax.extract("Python.")
        many = self.tax.extract("Python Python Python python PYTHON.")
        self.assertEqual(set(once), set(many))

    def test_shared_alias_maps_to_both_skills(self) -> None:
        """GKE genuinely implies both Google Cloud and Kubernetes."""
        found = set(self.tax.extract("Deploying to GKE in production."))
        self.assertIn("gcp", found)
        self.assertIn("kubernetes", found)

    def test_empty_and_none_text_are_safe(self) -> None:
        self.assertEqual(self.tax.extract(""), {})
        self.assertEqual(self.tax.extract(None), {})

    def test_realistic_posting_extracts_expected_stack(self) -> None:
        posting = """
        Senior Platform Engineer

        You will own our AWS infrastructure (EC2, S3, IAM, VPC) using Terraform and
        Ansible. Our services run in Docker on Kubernetes (EKS). CI/CD is GitHub Actions.
        We use Python and Go for tooling, PostgreSQL for storage, and Prometheus plus
        Grafana for observability. Kafka experience is a plus.
        """
        found = set(self.tax.extract(posting, title="Senior Platform Engineer"))
        for expected in [
            "aws",
            "ec2",
            "s3",
            "iam",
            "vpc",
            "terraform",
            "ansible",
            "docker",
            "kubernetes",
            "ci_cd",
            "github_actions",
            "python",
            "golang",
            "postgresql",
            "prometheus",
            "grafana",
            "kafka",
        ]:
            self.assertIn(expected, found, f"missed {expected}")


class BlockerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tax = load_taxonomy()

    def test_eu_work_authorization_detected(self) -> None:
        found = self.tax.extract_blockers(
            "Applicants must hold EU work authorization at time of application."
        )
        self.assertIn("eu_work_authorization", found)

    def test_no_sponsorship_detected(self) -> None:
        found = self.tax.extract_blockers("We are unable to offer visa sponsorship.")
        self.assertIn("us_work_authorization", found)

    def test_local_language_requirement_detected(self) -> None:
        found = self.tax.extract_blockers("Fluent Swedish is required for this role.")
        self.assertIn("local_language", found)

    def test_security_clearance_detected(self) -> None:
        found = self.tax.extract_blockers("Active TS/SCI security clearance required.")
        self.assertIn("security_clearance", found)

    def test_ordinary_posting_has_no_blockers(self) -> None:
        self.assertEqual(self.tax.extract_blockers("We are hiring a backend engineer."), [])


class CanonicalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tax = load_taxonomy()

    def test_resume_surfaces_map_to_canonical_keys(self) -> None:
        keys = self.tax.canonicalize(["ReactJS", "PostgreSQL", "vLLM", "nginx"])
        for expected in ["react", "postgresql", "vllm", "nginx"]:
            self.assertIn(expected, keys)

    def test_aliases_collapse_to_one_key(self) -> None:
        keys = self.tax.canonicalize(["React", "ReactJS", "react.js"])
        self.assertEqual(keys.count("react"), 1)

    def test_prose_noise_is_dropped(self) -> None:
        keys = self.tax.canonicalize(
            [
                "academic stakeholders",
                "and business leaders",
                "timeline management",
                "user adoption workflows",
            ]
        )
        self.assertEqual(keys, [])

    def test_unmapped_surfaces_are_reported_for_taxonomy_growth(self) -> None:
        _, unmapped = self.tax.canonicalize_verbose(["Python", "Blorpengine 9000"])
        self.assertIn("Blorpengine 9000", unmapped)

    def test_language_human_user_levels_are_explicit(self) -> None:
        """No resume states language proficiency, so these must come from the taxonomy."""
        self.assertEqual(self.tax["finnish"].user_level, 3)
        self.assertEqual(self.tax["english"].user_level, 3)
        self.assertEqual(self.tax["swedish"].user_level, 0)


if __name__ == "__main__":
    unittest.main()
