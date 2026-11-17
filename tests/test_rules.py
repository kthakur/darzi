"""Checks for rules.yaml (D-10 to D-13) and data checks on the synthetic facts."""

import copy
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from darzi.facts.patterns import find_regex, find_terms
from darzi.facts.repo import load_master, load_rules
from darzi.facts.rules import rule_entries, validate_rules
from darzi.schemas import RULES, load_schema

from .conftest import DATA

SPEC = Path(__file__).resolve().parent.parent / "docs" / "SPEC.md"


@pytest.fixture(scope="module")
def rules() -> dict[str, Any]:
    return load_rules(DATA)


@pytest.fixture(scope="module")
def rules_schema() -> dict[str, Any]:
    return load_schema(RULES)


def _spec_rule_table() -> dict[str, tuple[list[str], str]]:
    """Return {rule id: (rules.yaml keys, severity)} from docs/SPEC.md section 10."""
    rows = re.findall(
        r"^\| ((?:R|PV)-\d+) \| (.+?) \| (error|warning) \|$",
        SPEC.read_text(encoding="utf-8"),
        re.M,
    )
    return {rule: (re.findall(r"`([a-z_]+)`", keys), sev) for rule, keys, sev in rows}


def test_synthetic_rules_yaml_is_valid(
    rules: dict[str, Any], rules_schema: dict[str, Any]
) -> None:
    assert validate_rules(rules, rules_schema) == []


def test_spec_rule_table_is_parsed() -> None:
    table = _spec_rule_table()
    assert len([r for r in table if r.startswith("R-")]) == 22
    assert "PV-3" in table
    assert all(keys for keys, _ in table.values())


# D-10: the keys that the rule table of docs/SPEC.md section 10 names


def test_d10_keys_present(rules: dict[str, Any]) -> None:
    keys = {key for keys, _ in _spec_rule_table().values() for key in keys}
    assert sorted(keys - set(rules)) == []


# D-11: id and severity of each entry equal the rule table


def test_d11_each_key_has_the_id_and_severity_of_the_table(
    rules: dict[str, Any],
) -> None:
    for rule_id, (keys, severity) in _spec_rule_table().items():
        for key in keys:
            assert rules[key]["id"] == rule_id, key
            assert rules[key]["severity"] == severity, key


def test_d11_every_rule_has_an_entry_with_its_severity(
    rules: dict[str, Any],
) -> None:
    entries = rule_entries(rules)
    for rule_id, (_, severity) in _spec_rule_table().items():
        matching = [e for e in entries.values() if e["id"] == rule_id]
        assert matching, f"{rule_id} has no entry in rules.yaml"
        assert {e["severity"] for e in matching} == {severity}, rule_id


def test_d11_no_unknown_rule_ids(rules: dict[str, Any]) -> None:
    ids = {e["id"] for e in rule_entries(rules).values()}
    assert ids <= set(_spec_rule_table())


Change = Callable[[dict[str, Any]], object]

BROKEN: list[tuple[Change, str]] = [
    (lambda r: r["banned_words"].pop("reason"), "D-11"),
    (lambda r: r["banned_words"].update(severity="fatal"), "D-11"),
    (lambda r: r.pop("tech_terms"), "D-11"),
    (lambda r: r["years_patterns"]["regex"].append("(unclosed"), "D-13"),
    (lambda r: r["agents_hard_rules"][10].update(not_checked=None), "D-11"),
    (lambda r: r["agents_hard_rules"][1]["entries"].append("nope"), "D-12"),
    (lambda r: r["agents_hard_rules"][10].pop("not_checked"), "D-12"),
]


@pytest.mark.parametrize(("change", "rule"), BROKEN)
def test_broken_rules_are_reported(
    rules: dict[str, Any], rules_schema: dict[str, Any], change: Change, rule: str
) -> None:
    broken = copy.deepcopy(rules)
    change(broken)
    findings = validate_rules(broken, rules_schema)
    assert findings and {f.rule for f in findings} == {rule}


def test_d11_message_names_the_rules_schema(
    rules: dict[str, Any], rules_schema: dict[str, Any]
) -> None:
    broken = copy.deepcopy(rules)
    broken["banned_words"].pop("reason")
    (finding,) = validate_rules(broken, rules_schema)
    assert finding.location == "rules.yaml: banned_words"
    assert finding.message.endswith("Fix it to match the rules schema.")


def test_d12_unknown_entry_message(
    rules: dict[str, Any], rules_schema: dict[str, Any]
) -> None:
    broken = copy.deepcopy(rules)
    broken["agents_hard_rules"][1]["entries"].append("nope")
    (finding,) = validate_rules(broken, rules_schema)
    assert finding.location == "rules.yaml: agents_hard_rules.1"
    assert "'nope' does not exist" in finding.message


def test_d13_empty_term(rules: dict[str, Any], rules_schema: dict[str, Any]) -> None:
    broken = copy.deepcopy(rules)
    broken["banned_words"]["terms"].append("   ")
    findings = validate_rules(broken, rules_schema)
    assert [(f.rule, f.location) for f in findings] == [
        ("D-13", "rules.yaml: banned_words.terms")
    ]


# D-13: patterns


def test_d13_no_rule_term_matches_inside_a_word(rules: dict[str, Any]) -> None:
    terms = rules["removed_items"]["terms"]
    assert find_terms("Joined the Paper Crane Clubs fair", terms) == []
    assert find_terms("Joined the Paper Crane Club.", terms)


# Data checks against the synthetic master.yaml


@pytest.fixture(scope="module")
def master() -> dict[str, Any]:
    return load_master(DATA)


def _master_texts(master: dict[str, Any]) -> list[tuple[str, str]]:
    texts = [
        (f"{e['id']}/{b['id']}", b["text"])
        for e in master["roles"] + master["projects"]
        for b in e["bullets"]
    ]
    return texts + [
        (f"headline {h['text']}", h["text"]) for h in master["profile"]["headlines"]
    ]


@pytest.mark.parametrize(
    "key",
    [
        "banned_words",
        "banned_phrases",
        "pronouns",
        "education_forbidden",
        "removed_items",
        "doubtful_items",
        "inflation_phrases",
        "never_as_company",
    ],
)
def test_master_bullets_pass_term_rules(
    rules: dict[str, Any], master: dict[str, Any], key: str
) -> None:
    hits = [
        (where, m.text)
        for where, text in _master_texts(master)
        for m in find_terms(text, rules[key]["terms"])
    ]
    assert hits == []


def test_master_bullets_have_no_years_counts(
    rules: dict[str, Any], master: dict[str, Any]
) -> None:
    hits = [
        (where, m.text)
        for where, text in _master_texts(master)
        for m in find_regex(text, rules["years_patterns"]["regex"])
    ]
    assert hits == []


def test_removed_skills_are_gone(rules: dict[str, Any], master: dict[str, Any]) -> None:
    names = {s["name"] for s in master["skills"]}
    assert names.isdisjoint(rules["removed_items"]["skills"])


def test_public_contact_matches_master(
    rules: dict[str, Any], master: dict[str, Any]
) -> None:
    assert rules["public_contact"]["email"] == master["profile"]["email"]
    assert rules["public_contact"]["phone"] == master["profile"]["phone"]


def test_employer_of_record_covers_every_through_entry(
    rules: dict[str, Any], master: dict[str, Any]
) -> None:
    required = rules["employer_of_record"]["required_any"]
    ids = {e["id"] for e in master["roles"] + master["projects"]}
    assert set(required) <= ids
    for entry in master["roles"]:
        m = re.search(r"\bthrough (\w+)", entry["org"])
        if not m:
            continue
        assert entry["id"] in required, entry["id"]
        assert any(w.split()[0] == m.group(1) for w in required[entry["id"]])


def test_never_as_company_ids_exist(
    rules: dict[str, Any], master: dict[str, Any]
) -> None:
    ids = {e["id"] for e in master["roles"]}
    assert set(rules["never_as_company"]["freelance_only"]) <= ids


def test_not_working_periods_are_ordered(rules: dict[str, Any]) -> None:
    periods = rules["not_working_periods"]["periods"]
    assert all(p["from"] <= p["to"] for p in periods)
    assert [p["from"] for p in periods] == sorted(p["from"] for p in periods)


def test_rules_yaml_loads_with_safe_load() -> None:
    text = (DATA / "rules.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(text)["schema_version"] == 1
