"""`facts check`: the provenance rules R-13, R-20, R-21 and R-22."""

import copy
from typing import Any

import pytest

from darzi.facts.check_provenance import (
    claims,
    public_corpus,
    similarity,
    tech_terms,
    unsupported,
)

from .checking import ACME, MASTER, RULES, findings, found, tailored


@pytest.fixture
def base() -> dict[str, Any]:
    return tailored()


# Helpers


def test_similarity_values() -> None:
    assert similarity("Made the site.", "Made the site.") == 1.0
    assert similarity("Made the site.", "made THE site") == 1.0
    assert similarity("Made the site.", "Sang a tune.") < 0.3


def test_claims_skip_a_capital_at_the_start_of_a_sentence() -> None:
    found_claims = claims(
        'Planned flows. Cut wait time by 25%. "Ship It Now" UX.', set(), []
    )
    assert [(c.kind, c.text) for c in found_claims] == [
        ("number", "25"),
        ("name", "Ship"),
        ("name", "It"),
        ("name", "Now"),
        ("name", "UX"),
    ]


def test_claims_allowed_words_and_terms() -> None:
    found_claims = claims("Built it in Figma with React Native.", {"Figma"}, ["React"])
    assert [(c.kind, c.text) for c in found_claims] == [
        ("name", "Native"),
        ("term", "React"),
    ]


def test_unsupported_reports_each_claim_once() -> None:
    found_claims = claims("Cut 25% of 25 steps at Hooli and Hooli.", set(), [])
    assert [(c.kind, c.text) for c in unsupported(found_claims, "No source.")] == [
        ("number", "25"),
        ("name", "Hooli"),
    ]
    assert unsupported(found_claims, "Hooli cut 25 steps.") == []


def test_tech_terms_add_technical_fact_skills() -> None:
    terms = tech_terms(RULES, MASTER)
    assert terms[:5] == RULES["tech_terms"]["terms"]
    assert "Figma" in terms and "React" in terms
    master = copy.deepcopy(MASTER)
    for skill in master["skills"]:
        if skill["name"] == "Figma":
            skill["tags"] = ["design"]
    assert "Figma" not in tech_terms(RULES, master)


def test_public_corpus_leaves_out_private_text() -> None:
    corpus = public_corpus(MASTER)
    assert "Partner portal".lower() in corpus.lower()
    assert "Placed at Globex" not in corpus  # notes stay private
    assert "test.person@example.com" not in corpus  # contact data is not a fact
    master = copy.deepcopy(MASTER)
    master["roles"][0]["bullets"][0]["exclude_from"] = ["all"]
    assert "checkout flow" not in public_corpus(master)


def test_threshold_comes_from_rules(base: dict[str, Any]) -> None:
    assert RULES["provenance"]["similarity_threshold"] == 0.5
    base["workExperience"][ACME]["description"][0] = (
        "Designed a new checkout flow, which cut drop-off by 12%."
    )
    assert found(base, only="R-20") == []
    rules = copy.deepcopy(RULES)
    rules["provenance"]["similarity_threshold"] = 0.99
    assert found(base, only="R-20", rules=rules) == [
        ("R-20", "workExperience[0].description[0]")
    ]


# R-20 provenance


def test_r20_reworded_bullet_passes(base: dict[str, Any]) -> None:
    base["workExperience"][ACME]["description"][2] = (
        "Ran 20 user interviews that shaped the account settings pages."
    )
    assert found(base) == []


def test_r20_bullet_swapped_for_other_fact(base: dict[str, Any]) -> None:
    other = base["workExperience"][1]["description"][1]  # a Globex fact
    base["workExperience"][ACME]["description"][0] = other
    assert found(base, only="R-20") == [("R-20", "workExperience[0].description[0]")]
    (finding,) = [f for f in findings(base) if f.rule == "R-20"]
    assert "drifts from its source fact" in finding.message


def test_r20_bullet_with_no_source(base: dict[str, Any]) -> None:
    base["workExperience"][ACME]["description"].append("Planned the release notes.")
    assert found(base, only="R-20") == [("R-20", "workExperience[0].description[3]")]
    (finding,) = [f for f in findings(base) if f.rule == "R-20"]
    assert "has no source fact" in finding.message


# R-13 numbers and names


def test_r13_names_from_other_facts_allowed_in_summary(base: dict[str, Any]) -> None:
    base["summary"] += " Worked with Globex and Acme in Springfield."
    assert found(base) == []


def test_r13_new_name_in_bullet(base: dict[str, Any]) -> None:
    base["workExperience"][ACME]["description"][0] += " Shared with Hooli."
    assert found(base, only="R-13") == [("R-13", "workExperience[0].description[0]")]


def test_r13_name_of_its_own_entry_passes(base: dict[str, Any]) -> None:
    base["workExperience"][ACME]["description"][0] += " Shared with Acme."
    assert found(base, only="R-13") == []


def test_r13_new_name_in_summary(base: dict[str, Any]) -> None:
    base["summary"] += " Built tools for Hooli."
    assert found(base) == [("R-13", "summary")]


def test_r13_new_number(base: dict[str, Any]) -> None:
    base["summary"] += " Cut costs by 45%."
    assert found(base) == [("R-13", "summary")]
    text = base["workExperience"][ACME]["description"][1]
    base["workExperience"][ACME]["description"][1] = text.replace("40", "45")
    assert found(base, only="R-13") == [
        ("R-13", "summary"),
        ("R-13", "workExperience[0].description[1]"),
    ]


def test_r13_headline(base: dict[str, Any]) -> None:
    base["personalInfo"]["title"] = "Product Designer at Hooli"
    assert found(base) == [("R-13", "personalInfo.title")]


# R-21 new technology terms


def test_r21_fact_skill_in_summary_passes(base: dict[str, Any]) -> None:
    base["summary"] += " Builds apps in Swift. Tests flows in Figma."
    assert found(base) == []


def test_r21_new_term_in_summary(base: dict[str, Any]) -> None:
    base["summary"] += " Ships on Kubernetes."
    assert found(base) == [("R-21", "summary")]


def test_r21_new_term_in_bullet(base: dict[str, Any]) -> None:
    base["workExperience"][ACME]["description"][0] += " Built in Python."
    assert found(base, only="R-21") == [("R-21", "workExperience[0].description[0]")]
    assert found(base, only="R-13") == []  # the term is reported once, as a term


def test_r21_term_of_the_source_bullet_passes(base: dict[str, Any]) -> None:
    base["workExperience"][1]["description"][0] = (
        "Designed the partner portal with the engineering team, in React."
    )
    assert found(base) == []


# R-22 cover letter


JOB = "Product Designer, Payments team, Springfield. Work with Initrode Cloud."


def test_r22_facts_and_job_words_pass(base: dict[str, Any]) -> None:
    letter = (
        "Hello,\n\n"
        "I want to join the Payments team at Initrode Cloud.\n"
        "At Acme I designed a new checkout flow in Figma.\n\n"
        "Thank you,\nTest Person\n"
    )
    assert found(base, letter, JOB) == []


@pytest.mark.parametrize(
    "line",
    [
        "I redesigned checkout at Hooli.",
        "I cut support costs by 45%.",
        "I run Kubernetes.",
    ],
)
def test_r22_unsupported_claims(base: dict[str, Any], line: str) -> None:
    assert found(base, line, JOB, only="R-22") == [("R-22", "cover_letter.md:1")]


def test_r22_job_post_words_need_the_job_post(base: dict[str, Any]) -> None:
    letter = "I want to join the Payments team."
    assert found(base, letter, None, only="R-22") == [("R-22", "cover_letter.md:1")]
    assert found(base, letter, JOB, only="R-22") == []


def test_r22_allowed_words(base: dict[str, Any]) -> None:
    letter = "Dear Hiring Manager,"
    result = [f for f in findings(base, letter, JOB) if f.severity == "error"]
    assert [(f.rule, f.location) for f in result] == [("R-22", "cover_letter.md:1")] * 2
    assert "'Hiring'" in result[0].message
    assert "'Manager'" in result[1].message
    rules = copy.deepcopy(RULES)
    rules["cover_letter_claims"]["allowed_words"] = ["Hiring", "Manager"]
    assert found(base, letter, JOB, rules=rules) == []


def test_r22_public_contact_may_sign_the_letter(base: dict[str, Any]) -> None:
    letter = "Test Person\ntest.person@example.com | 555-0100\n"
    assert found(base, letter, JOB) == []
    other = "Test Person\n555-0123\n"
    assert found(base, other, JOB, only="R-22") == [("R-22", "cover_letter.md:2")]


def test_r22_no_letter(base: dict[str, Any]) -> None:
    assert found(base, None, JOB, only="R-22") == []
    assert found(base, "", JOB, only="R-22") == []
