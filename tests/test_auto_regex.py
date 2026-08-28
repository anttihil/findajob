"""Unit tests for auto-compiled plain string aliases in RoleTaxonomy."""

from careerradar.taxonomy.roles import RoleFamily, RoleTaxonomy, compile_aliases


def test_compile_plain_string_alias():
    patterns = compile_aliases(["AI Engineer", "Machine Learning Specialist"])
    assert len(patterns) == 2

    # Should match case-insensitively
    assert patterns[0].search("Senior AI Engineer (Remote)")
    assert patterns[0].search("ai-engineer")
    assert patterns[0].search("ai engineer")

    # Should not match partial unrelated words
    assert not patterns[0].search("Bonsai Engineering")


def test_role_family_with_plain_aliases():
    spec = {
        "label": "Prompt Engineer",
        "aliases": ["Prompt Engineer", "LLM Specialist"],
        "query_terms": ["Prompt Engineer"],
        "enabled": True,
    }
    family = RoleFamily("prompt_engineer", spec, order=0)
    assert family.key == "prompt_engineer"

    pos, is_weak = family.find("Lead Prompt Engineer - Core AI")
    assert pos is not None
    assert pos == 5
    assert not is_weak


def test_role_taxonomy_from_dict():
    data = {
        "families": {
            "ai_engineer": {
                "label": "AI Engineer",
                "aliases": ["AI Engineer", "GenAI Engineer"],
                "query_terms": ["AI Engineer"],
            },
            "backend_engineer": {
                "label": "Backend Engineer",
                "aliases": ["Backend Engineer", "Java Developer"],
                "query_terms": ["Backend Engineer"],
            },
        },
        "locations": [
            {
                "id": "us_remote",
                "label": "Remote, United States",
                "country": "US",
                "is_remote": True,
                "access": "remote",
            }
        ],
    }
    tax = RoleTaxonomy(spec_dict=data)
    assert len(tax.families) == 2
    assert "ai_engineer" in tax
    assert tax.classify("Senior GenAI Engineer")[0] == "ai_engineer"
    assert tax.classify("Backend Engineer (Go)")[0] == "backend_engineer"

    # cell_specs
    specs = tax.cell_specs()
    assert len(specs) == 4  # 2 families x 1 location x 2 sources
