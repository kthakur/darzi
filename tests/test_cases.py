"""The 14 edit cases give the table in docs/SPEC.md section 10."""

import re
from pathlib import Path

import pytest

from .cases import EXPECTED, Case
from .checking import findings, tailored

SPEC = Path(__file__).resolve().parent.parent / "docs" / "SPEC.md"


def _spec_cases() -> dict[str, tuple[str, set[str]]]:
    """Return {case number: (pass or fail, error rules)} from docs/SPEC.md."""
    rows = re.findall(
        r"^\| (\d\d) \| .+? \| (pass|fail) \| ([^|]*?) ?\|$",
        SPEC.read_text(encoding="utf-8"),
        re.M,
    )
    return {
        number: (result, set(re.findall(r"R-\d+", rules)))
        for number, result, rules in rows
    }


def _number(case: Case) -> str:
    return case.__name__.split("_")[1]


def test_spec_table_is_parsed() -> None:
    table = _spec_cases()
    assert sorted(table) == [f"{n:02d}" for n in range(1, 15)]
    assert table["11"] == ("fail", {"R-12", "R-20", "R-21"})


def test_cases_equal_the_spec_table() -> None:
    table = _spec_cases()
    assert sorted(_number(c) for c in EXPECTED) == sorted(table)
    for case, rules in EXPECTED.items():
        result, spec_rules = table[_number(case)]
        assert rules == spec_rules, case.__name__
        assert (result == "pass") == (not rules), case.__name__


@pytest.mark.parametrize("case", list(EXPECTED), ids=lambda c: c.__name__)
def test_case_gives_exactly_its_rules(case: Case) -> None:
    edited = case(tailored())
    errors = {f.rule for f in findings(edited) if f.severity == "error"}
    assert errors == EXPECTED[case]
