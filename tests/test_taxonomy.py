"""Taxonomy tests for the open-vocabulary skills container."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.profile.models import MasterSkillCategory, Profile
from careerradar.taxonomy.skills import Skill, Taxonomy, load_taxonomy

TEST_TAXONOMY_SPEC = {
    "version": 1,
    "categories": [
        "language",
        "frontend",
        "backend",
        "cloud",
        "devops",
        "data",
    ],
    "skills": {
        "golang": {
            "label": "Go",
            "category": "language",
        },
        "python": {
            "label": "Python",
            "category": "language",
        },
        "kubernetes": {
            "label": "Kubernetes",
            "category": "devops",
        },
        "aws": {
            "label": "AWS",
            "category": "cloud",
        },
        "postgresql": {
            "label": "PostgreSQL",
            "category": "backend",
        },
    },
}


class TaxonomyIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tax = Taxonomy(data=TEST_TAXONOMY_SPEC)

    def test_default_taxonomy_is_open_vocabulary_and_empty_without_yaml(self) -> None:
        tax = Taxonomy()
        self.assertEqual(len(tax), 0)
        self.assertEqual(tax.validate(), [])
        self.assertEqual(tax.categories, [])

    def test_load_taxonomy_returns_instance_without_yaml(self) -> None:
        tax = load_taxonomy()
        self.assertIsNotNone(tax)
        self.assertEqual(tax.validate(), [])

    def test_validates_clean(self) -> None:
        problems = self.tax.validate()
        self.assertEqual(problems, [], f"taxonomy problems: {problems}")

    def test_has_meaningful_size(self) -> None:
        self.assertGreater(len(self.tax), 3)

    def test_hash_is_stable_and_content_derived(self) -> None:
        tax2 = Taxonomy(data=TEST_TAXONOMY_SPEC)
        self.assertEqual(self.tax.hash, tax2.hash)
        self.assertEqual(len(self.tax.hash), 16)

    def test_categories_extracted_from_skills(self) -> None:
        self.assertIn("language", self.tax.categories)
        self.assertIn("cloud", self.tax.categories)


class SkillModelTests(unittest.TestCase):
    def test_skill_creation_and_defaults(self) -> None:
        s = Skill("machine_learning")
        self.assertEqual(s.key, "machine_learning")
        self.assertEqual(s.label, "Machine Learning")
        self.assertEqual(s.category, "other")
        self.assertEqual(s.effort, "medium")

    def test_skill_with_spec(self) -> None:
        s = Skill("pytorch", {"label": "PyTorch", "category": "ai_ml", "effort": "high"})
        self.assertEqual(s.label, "PyTorch")
        self.assertEqual(s.category, "ai_ml")
        self.assertEqual(s.effort, "high")


class EnrichFromProfileTests(unittest.TestCase):
    def test_enrich_from_profile_with_master_skill_categories(self) -> None:
        profile = Profile(
            name="Tester",
            skills=[MasterSkillCategory(category="Cloud", skills=["AWS", "GCP"])],
        )
        tax = Taxonomy()
        tax.enrich_from_profile(profile)
        self.assertIn("aws", tax)
        self.assertIn("gcp", tax)
        self.assertEqual(tax.label("aws"), "AWS")
        self.assertEqual(tax.category("aws"), "Cloud")

    def test_from_profile_with_domain_skills(self) -> None:
        profile = Profile(
            name="Tester",
            skills=[MasterSkillCategory(category="DevOps", skills=["Kubernetes"])],
        )
        tax = Taxonomy.from_profile(profile, domain_skills=["Helm", "ArgoCD"])
        self.assertIn("kubernetes", tax)
        self.assertIn("helm", tax)
        self.assertIn("argocd", tax)
        self.assertEqual(tax.category("helm"), "domain")


class TaxonomyContainerMethodsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tax = Taxonomy(data=TEST_TAXONOMY_SPEC)

    def test_container_access(self) -> None:
        self.assertIn("golang", self.tax)
        self.assertEqual(self.tax["golang"].label, "Go")
        self.assertEqual(self.tax.label("golang"), "Go")
        self.assertEqual(self.tax.label("unknown_skill"), "Unknown Skill")
        self.assertEqual(self.tax.category("golang"), "language")
        self.assertEqual(self.tax.category("unknown_skill"), "other")
        self.assertEqual(self.tax.effort("golang"), "medium")

    def test_iter_keys_values_items(self) -> None:
        self.assertEqual(len(list(self.tax)), len(self.tax))
        self.assertEqual(len(list(self.tax.keys())), len(self.tax))
        self.assertEqual(len(list(self.tax.values())), len(self.tax))
        self.assertEqual(len(list(self.tax.items())), len(self.tax))

    def test_clone(self) -> None:
        clone = self.tax.clone()
        self.assertEqual(len(clone), len(self.tax))
        self.assertEqual(clone.hash, self.tax.hash)


if __name__ == "__main__":
    unittest.main()
