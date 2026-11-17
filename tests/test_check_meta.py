"""The reader for open questions in questions.md (used by D-21 and R-17).

Work item W-3 adds the tests of R-16, R-17 and R-18 to this file.
"""

from pathlib import Path

from darzi.facts.check_meta import load_open_questions, open_questions
from darzi.facts.repo import load_master

from .conftest import DATA

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
