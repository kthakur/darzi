"""R-16 unverified bullets, R-17 open questions, R-18 pages, and the reader for
open questions in questions.md (used by D-21 and R-17).
"""

import copy
from pathlib import Path
from typing import Any

import pytest

from darzi.facts.check_meta import load_open_questions, open_questions
from darzi.facts.repo import load_master

from .checking import BASE_MAP, MASTER, findings, tailored
from .conftest import DATA


def found(rule: str, **kwargs: Any) -> list[tuple[str, str, str]]:
    """Return (rule, severity, location) of each finding of `rule` on the base."""
    return [
        (f.rule, f.severity, f.location)
        for f in findings(tailored(), **kwargs)
        if f.rule == rule
    ]


def _verified(bullet_id: str) -> bool:
    for entry in MASTER["roles"] + MASTER["projects"]:
        for bullet in entry["bullets"]:
            if bullet["id"] == bullet_id:
                return bullet.get("verified") is True
    raise KeyError(bullet_id)


# R-16 unverified bullets


def test_r16_one_warning_per_unverified_bullet() -> None:
    used = [b for e in BASE_MAP["entries"] for b in e.get("bullets", [])]
    unverified = [b for b in used if not _verified(b)]
    assert unverified == ["acme-research", "freelance-shop-sites", "plant-app-build"]
    warnings = found("R-16")
    assert len(warnings) == len(unverified)
    assert {sev for _, sev, _ in warnings} == {"warning"}
    messages = [f.message for f in findings(tailored()) if f.rule == "R-16"]
    assert messages[0] == (
        "Bullet plant-app-build is not verified. The owner may confirm it, or the "
        "selection may leave it out."
    )


def test_r16_verified_bullets_pass() -> None:
    master = copy.deepcopy(MASTER)
    for entry in master["roles"] + master["projects"]:
        for bullet in entry["bullets"]:
            bullet["verified"] = True
    assert found("R-16", master=master) == []


def test_r16_warnings_do_not_fail_the_run() -> None:
    result = findings(tailored())
    assert result and {f.severity for f in result} == {"warning"}


# R-17 open questions


def test_r17_entry_named_in_open_question() -> None:
    assert found("R-17", questions={"acme": ["7"]}) == [
        ("R-17", "error", "workExperience[0]")
    ]
    (finding,) = [
        f for f in findings(tailored(), questions={"acme": ["7"]}) if f.rule == "R-17"
    ]
    assert finding.message == (
        "acme is named in open question(s) 7 in questions.md. Leave it out until "
        "the owner answers."
    )


def test_r17_bullet_named_in_open_question() -> None:
    assert found("R-17", questions={"globex-portal": ["8"]}) == [
        ("R-17", "error", "workExperience[1]")
    ]


def test_r17_education_named_in_open_question() -> None:
    assert found("R-17", questions={"example-state-hci": ["9"]}) == [
        ("R-17", "error", "education[1]")
    ]


def test_r17_unused_fact_passes() -> None:
    questions = load_open_questions(DATA)
    assert questions == {"acme-accessibility": ["1"]}
    assert found("R-17", questions=questions) == []


# R-18 pages

MANIFEST = {
    "files": [{"path": "out/resume.pdf", "pages": 3}, {"path": "out/resume.docx"}]
}


@pytest.mark.parametrize(("max_pages", "count"), [(2, 1), (3, 0)])
def test_r18_pages(max_pages: int, count: int) -> None:
    the_map = {**BASE_MAP, "max_pages": max_pages}
    result = found("R-18", the_map=the_map, manifest=MANIFEST)
    assert result == [("R-18", "warning", "manifest.json: out/resume.pdf")] * count


def test_r18_no_manifest_yet() -> None:
    assert found("R-18", the_map={**BASE_MAP, "max_pages": 1}) == []


# The reader for open questions

QUESTIONS_MD = """# Questions

## A. Roles

- **12. Acme end date.** Text. Affects: acme.
- **15a. Two facts.** More text
  on two lines. Affects: acme, globex-portal.

## Settled by confirmed facts

- **9. Old.** Affects: vandelay.

## Resolved

- **20** (2026-01-15): Affects: plant-app.
"""


def test_parser_reads_only_open_questions() -> None:
    assert open_questions(QUESTIONS_MD) == {
        "acme": ["12", "15a"],
        "globex-portal": ["15a"],
    }


def test_parser_on_synthetic_questions_md() -> None:
    affected = load_open_questions(DATA)
    assert affected == {"acme-accessibility": ["1"]}
    master = load_master(DATA)
    entries = master["roles"] + master["projects"]
    ids = {e["id"] for e in entries + master["education"]}
    ids |= {b["id"] for e in entries for b in e["bullets"]}
    assert set(affected) <= ids, "every Affects: id must exist in master.yaml"


def test_missing_questions_md(tmp_path: Path) -> None:
    assert load_open_questions(tmp_path) == {}
