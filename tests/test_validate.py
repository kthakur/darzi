"""`facts validate` on master.yaml: D-1 to D-6 and D-8."""

from typing import Any

import pytest

from darzi.facts.repo import load_master, load_selections
from darzi.facts.validate import Finding, validate
from darzi.schemas import MASTER, load_schema

from .conftest import DATA


def rules(findings: list[Finding]) -> list[str]:
    return [f.rule for f in findings]


def test_minimal_master_passes(master: dict[str, Any], schema: dict[str, Any]) -> None:
    assert validate(master, schema) == []


def test_synthetic_master_yaml_passes() -> None:
    findings = validate(load_master(DATA), load_schema(MASTER), load_selections(DATA))
    assert findings == [], "\n".join(
        f"{f.rule} {f.location}: {f.message}" for f in findings
    )


# D-1: structure


def test_d1_missing_required_field(
    master: dict[str, Any], schema: dict[str, Any]
) -> None:
    del master["roles"][0]["org"]
    findings = validate(master, schema)
    assert rules(findings) == ["D-1"]
    assert findings[0].location == "roles[acme]"
    assert "'org' is a required property" in findings[0].message


def test_d1_unknown_field(master: dict[str, Any], schema: dict[str, Any]) -> None:
    master["roles"][0]["bullets"][0]["impact"] = "big"
    findings = validate(master, schema)
    assert rules(findings) == ["D-1"]
    assert findings[0].location == "roles[acme].bullets[acme-forms]"


def test_d1_role_field_not_allowed_on_project(
    master: dict[str, Any], schema: dict[str, Any]
) -> None:
    master["projects"][0]["alt_titles"] = ["Lead"]
    assert rules(validate(master, schema)) == ["D-1"]


def test_d1_display_names_are_allowed(
    master: dict[str, Any], schema: dict[str, Any]
) -> None:
    master["roles"][0]["display_org"] = "Acme"
    master["education"][0]["display_institution"] = "Example U."
    assert validate(master, schema) == []


# D-2: unique, well-formed IDs


def test_d2_duplicate_id_across_sections(
    master: dict[str, Any], schema: dict[str, Any]
) -> None:
    master["roles"][0]["bullets"][0]["id"] = "example-ba"
    findings = validate(master, schema)
    assert rules(findings) == ["D-2"]
    assert findings[0].location == "education[example-ba]"
    assert "roles[acme].bullets[example-ba]" in findings[0].message


def test_d2_id_must_be_lowercase_hyphenated(
    master: dict[str, Any], schema: dict[str, Any]
) -> None:
    master["roles"][0]["id"] = "Acme_Corp"
    assert rules(validate(master, schema)) == ["D-2"]


# D-3: known sources


def test_d3_unknown_source_in_bullet(
    master: dict[str, Any], schema: dict[str, Any]
) -> None:
    master["roles"][0]["bullets"][0]["sources"] = ["cv-a", "chat-memory"]
    findings = validate(master, schema)
    assert rules(findings) == ["D-3"]
    assert findings[0].location == "roles[acme].bullets[acme-forms].sources[1]"
    assert "'chat-memory'" in findings[0].message


@pytest.mark.parametrize(
    "path",
    [
        ("skills", 0, "sources"),
        ("profile", "contact_sources"),
        ("profile", "headlines", 0, "sources"),
    ],
)
def test_d3_unknown_source_anywhere(
    master: dict[str, Any], schema: dict[str, Any], path: tuple[Any, ...]
) -> None:
    node: Any = master
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = ["nope"]
    assert rules(validate(master, schema)) == ["D-3"]


# D-4: dates


@pytest.mark.parametrize("value", ["2023-02", "2011", 2004, "present", ""])
def test_d4_valid_dates(
    master: dict[str, Any], schema: dict[str, Any], value: Any
) -> None:
    master["roles"][0]["end"] = value
    assert validate(master, schema) == []


@pytest.mark.parametrize("value", ["2022-14", "Feb 2023", "2023-2", "today", 42])
def test_d4_invalid_dates(
    master: dict[str, Any], schema: dict[str, Any], value: Any
) -> None:
    master["roles"][0]["end"] = value
    findings = validate(master, schema)
    assert rules(findings) == ["D-4"]
    assert findings[0].location == "roles[acme].end"


def test_d4_dates_in_positions_and_residence(
    master: dict[str, Any], schema: dict[str, Any]
) -> None:
    master["roles"][0]["positions"][0]["start"] = "Jan 2020"
    master["profile"]["residence"][0]["from"] = "spring"
    assert rules(validate(master, schema)) == ["D-4", "D-4"]


# D-5: entry types


def test_d5_invalid_type(master: dict[str, Any], schema: dict[str, Any]) -> None:
    master["projects"][0]["type"] = "freelance"
    findings = validate(master, schema)
    assert rules(findings) == ["D-5"]
    assert "own-business" in findings[0].message


# D-6: bullet tags


def test_d6_tag_not_in_list(master: dict[str, Any], schema: dict[str, Any]) -> None:
    master["roles"][0]["bullets"][0]["tags"] = ["ux", "design"]
    findings = validate(master, schema)
    assert rules(findings) == ["D-6"]
    assert findings[0].location == "roles[acme].bullets[acme-forms].tags[1]"
    assert "master.schema.json" in findings[0].message


def test_d6_skill_tags_are_free(master: dict[str, Any], schema: dict[str, Any]) -> None:
    master["skills"][0]["tags"] = ["anything-goes"]
    assert validate(master, schema) == []


# D-8: display names for entries that a selection uses

LONG_ORG = "Acme Corporation (client, through a very long staffing agency name)"
SELECTION = {"work": [{"id": "acme", "bullets": ["acme-forms"]}], "education": []}


def test_d8_warns_for_selected_long_org(
    master: dict[str, Any], schema: dict[str, Any]
) -> None:
    master["roles"][0]["org"] = LONG_ORG
    findings = validate(master, schema, {"selections/ux.yaml": SELECTION})
    assert [(f.rule, f.severity) for f in findings] == [("D-8", "warning")]
    assert findings[0].location == "roles[acme]"
    assert "selections/ux.yaml" in findings[0].message
    assert "the owner" in findings[0].message


def test_d8_display_name_clears_warning(
    master: dict[str, Any], schema: dict[str, Any]
) -> None:
    master["roles"][0]["org"] = LONG_ORG
    master["roles"][0]["display_org"] = "Acme"
    assert validate(master, schema, {"selections/ux.yaml": SELECTION}) == []


def test_d8_unselected_entry_is_not_checked(
    master: dict[str, Any], schema: dict[str, Any]
) -> None:
    master["roles"][0]["org"] = LONG_ORG
    assert validate(master, schema, {"selections/ux.yaml": {"work": []}}) == []
    assert validate(master, schema) == []


def test_d8_education_institution(
    master: dict[str, Any], schema: dict[str, Any]
) -> None:
    master["education"][0]["institution"] = "E" * 51
    selection = {"education": ["example-ba"]}
    findings = validate(master, schema, {"selections/x.yaml": selection})
    assert [f.rule for f in findings] == ["D-8"]
    assert "display_institution" in findings[0].message


def test_d8_synthetic_master_has_short_names_for_long_names() -> None:
    """Each synthetic entry with a name over 50 characters has a display name."""
    master = load_master(DATA)
    every_entry = {
        "work": [{"id": e["id"]} for e in master["roles"] + master["projects"]],
        "education": [e["id"] for e in master["education"]],
    }
    findings = validate(master, load_schema(MASTER), {"all": every_entry})
    assert findings == []
    long_names = [
        e for e in master["education"] if len(e["institution"]) > 50
    ]  # the synthetic data has one, so the test is not empty
    assert long_names
