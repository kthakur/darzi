"""`facts public`, the privacy scan and `facts query` (PV-1 to PV-3)."""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from darzi.cli import main
from darzi.facts.public import privacy_findings, public_copy, query, strings
from darzi.facts.repo import load_master, load_rules

from .conftest import DATA

RULES = load_rules(DATA)
MASTER = load_master(DATA)


@pytest.fixture(scope="module")
def public() -> dict[str, Any]:
    return public_copy(MASTER, RULES)


def _bullet(data: dict[str, Any], bullet_id: str) -> dict[str, Any]:
    return next(
        b
        for e in data["roles"] + data["projects"]
        for b in e["bullets"]
        if b["id"] == bullet_id
    )


# The privacy test on the public copy of the synthetic data root


def test_public_copy_has_no_private_data(public: dict[str, Any]) -> None:
    findings = privacy_findings(public, RULES)
    assert findings == [], "\n".join(f"{f.location}: {f.message}" for f in findings)


def test_scan_does_find_private_data_in_master() -> None:
    """Control: the same scan finds the private parts of a master.yaml."""
    master = copy.deepcopy(MASTER)
    master["profile"]["notes"] = "Private phone 555-0199."
    master["roles"][0]["notes"] = "Mail test.private@example.org."
    assert {f.location for f in privacy_findings(master, RULES)} == {
        "profile.notes",
        "roles[acme].notes",
    }
    assert privacy_findings(public_copy(master, RULES), RULES) == []


# PV-1: what the public copy leaves out


def test_pv1_dropped_keys() -> None:
    master = copy.deepcopy(MASTER)
    master["profile"]["computing_background"] = "Wrote small games as a teenager."
    data = public_copy(master, RULES)
    assert "source_files" not in data
    for key in (
        "notes",
        "residence",
        "experience_claims",
        "experience_claims_policy",
        "availability_notes",
        "contact_sources",
        "contact_verified",
    ):
        assert key not in data["profile"]
    assert "computing_background" in data["profile"]
    paths = [path for path, _ in strings(data)]
    assert not [p for p in paths if p.endswith(".notes")]


def test_pv1_public_contact_only() -> None:
    master = copy.deepcopy(MASTER)
    master["profile"]["email"] = "test.private@example.org"
    master["profile"]["phone"] = "555-0199"
    data = public_copy(master, RULES)
    assert data["profile"]["email"] == "test.person@example.com"
    assert data["profile"]["phone"] == "555-0100"


def test_pv1_exclude_from_all_dropped() -> None:
    master = copy.deepcopy(MASTER)
    _bullet(master, "acme-accessibility")["exclude_from"] = ["all"]
    master["projects"][0]["exclude_from"] = ["all"]
    master["education"][1]["exclude_from"] = ["all"]
    data = public_copy(master, RULES)
    bullets = {b["id"] for e in data["roles"] + data["projects"] for b in e["bullets"]}
    assert "acme-accessibility" not in bullets
    assert "acme-checkout" in bullets
    assert data["projects"] == []
    assert [e["id"] for e in data["education"]] == ["example-u-ba"]


def test_pv1_removed_and_doubtful_items_dropped() -> None:
    master = copy.deepcopy(MASTER)
    master["skills"].append(
        {"name": "Flash", "tags": ["web"], "sources": ["confirmed"]}
    )
    master["skills"].append(
        {"name": "Paper Crane Club", "tags": ["club"], "sources": ["confirmed"]}
    )
    for bullet_id, text in (
        ("acme-club", "Ran the Paper Crane Club."),
        ("acme-award", "Won the Night Shift Award."),
    ):
        master["roles"][0]["bullets"].append(
            {
                "id": bullet_id,
                "text": text,
                "tags": ["ux"],
                "evidence": "",
                "sources": ["confirmed"],
            }
        )
    rules = copy.deepcopy(RULES)
    rules["doubtful_items"]["bullet_ids"] = ["acme-research"]
    data = public_copy(master, rules)
    assert {s["name"] for s in data["skills"]}.isdisjoint({"Flash", "Paper Crane Club"})
    kept = {b["id"] for b in data["roles"][0]["bullets"]}
    assert kept == {"acme-checkout", "acme-design-system", "acme-accessibility"}


def test_display_names_replace_long_names(public: dict[str, Any]) -> None:
    orgs = {e["id"]: e["org"] for e in public["roles"] + public["projects"]}
    assert orgs["acme"] == "Acme"
    assert orgs["globex"] == "Globex (contract through Initech Staffing)"
    assert orgs["vandelay"] == "Vandelay Industries"  # no display name: org stays
    schools = {e["id"]: e["institution"] for e in public["education"]}
    assert schools["example-state-hci"] == "Example State University"
    assert schools["example-u-ba"] == "Example University"


def test_public_copy_does_not_change_master() -> None:
    before = copy.deepcopy(MASTER)
    public_copy(MASTER, RULES)
    assert before == MASTER


def test_public_copy_has_schema_version(public: dict[str, Any]) -> None:
    assert public["schema_version"] == 1


# PV-3: the scan


@pytest.mark.parametrize(
    "text",
    [
        "Reach me at test.private@example.org",
        "Call 555-0199",
        "Call (212) 555-0101",
        "Write to someone@example.org",
        "Lives at 12 Elm Street",
        "Built it with zorbek.",
    ],
)
def test_pv3_private_patterns(public: dict[str, Any], text: str) -> None:
    data = copy.deepcopy(public)
    _bullet(data, "acme-checkout")["text"] = text
    findings = privacy_findings(data, RULES)
    assert findings
    assert {f.location for f in findings} == {"roles[acme].bullets[acme-checkout].text"}
    assert all(f.text == text for f in findings)


def test_pv3_public_contact_passes(public: dict[str, Any]) -> None:
    data = copy.deepcopy(public)
    _bullet(data, "acme-checkout")["text"] = "Mail test.person@example.com or 555-0100."
    assert privacy_findings(data, RULES) == []


@pytest.mark.parametrize("shown", ["(555) 555-0100", "555.555.0100", "555 555 0100"])
def test_pv3_public_phone_in_other_format_passes(shown: str) -> None:
    """A 10-digit public phone passes in any format the phone pattern finds."""
    rules = copy.deepcopy(RULES)
    rules["public_contact"]["phone"] = "555-555-0100"
    data = public_copy(MASTER, rules)
    assert data["profile"]["phone"] == "555-555-0100"
    _bullet(data, "acme-checkout")["text"] = f"Call {shown} after noon."
    assert privacy_findings(data, rules) == []


def test_pv3_other_10_digit_phone_is_found() -> None:
    rules = copy.deepcopy(RULES)
    rules["public_contact"]["phone"] = "555-555-0100"
    data = public_copy(MASTER, rules)
    _bullet(data, "acme-checkout")["text"] = "Call 555-555-0101 after noon."
    findings = privacy_findings(data, rules)
    assert [(f.rule, f.location) for f in findings] == [
        ("R-1", "roles[acme].bullets[acme-checkout].text")
    ]
    assert "'555-555-0101'" in findings[0].message


def test_pv3_public_phone_with_country_code() -> None:
    """The comparison uses the last ten digits of the public phone."""
    rules = copy.deepcopy(RULES)
    rules["public_contact"]["phone"] = "+1 555-555-0100"
    data = public_copy(MASTER, rules)
    _bullet(data, "acme-checkout")["text"] = "Call 555-555-0100 after noon."
    assert privacy_findings(data, rules) == []
    _bullet(data, "acme-checkout")["text"] = "Call (556) 555-0100 after noon."
    findings = privacy_findings(data, rules)
    assert [(f.rule, f.location) for f in findings] == [
        ("R-1", "roles[acme].bullets[acme-checkout].text")
    ]
    assert "'(556) 555-0100'" in findings[0].message


def test_pv3_private_email_term_gives_one_finding(public: dict[str, Any]) -> None:
    data = copy.deepcopy(public)
    _bullet(data, "acme-checkout")["text"] = "Mail test.private@example.org."
    findings = privacy_findings(data, RULES)
    assert len(findings) == 1
    assert "private contact 'test.private@example.org'" in findings[0].message


def test_pv3_private_phone_term_gives_one_finding() -> None:
    rules = copy.deepcopy(RULES)
    rules["private_contact_patterns"]["terms"].append("555-555-0177")
    data = public_copy(MASTER, rules)
    _bullet(data, "acme-checkout")["text"] = "Call 555-555-0177."
    findings = privacy_findings(data, rules)
    assert len(findings) == 1
    assert "private contact '555-555-0177'" in findings[0].message
    # Control: with no term, the phone comparison finds the same number.
    findings = privacy_findings(data, RULES)
    assert len(findings) == 1
    assert "phone number '555-555-0177'" in findings[0].message


def test_pv3_email_ignores_letter_case(public: dict[str, Any]) -> None:
    data = copy.deepcopy(public)
    _bullet(data, "acme-checkout")["text"] = "Mail TEST.PERSON@EXAMPLE.COM."
    assert privacy_findings(data, RULES) == []
    _bullet(data, "acme-checkout")["text"] = "Mail TEST.OTHER@EXAMPLE.COM."
    assert len(privacy_findings(data, RULES)) == 1  # control


def test_pv3_term_allowed_only_in_approved_bullet(public: dict[str, Any]) -> None:
    rules = copy.deepcopy(RULES)
    rules["privacy_patterns"]["allowed_bullet_ids"] = ["acme-checkout"]
    data = copy.deepcopy(public)
    _bullet(data, "acme-checkout")["text"] += " With zorbek."
    _bullet(data, "acme-design-system")["text"] += " With zorbek."
    assert [f.location for f in privacy_findings(data, rules)] == [
        "roles[acme].bullets[acme-design-system].text"
    ]


# facts query


def test_query_all_tags_and_text(public: dict[str, Any]) -> None:
    hits = query(public, ["ux", "build"], "figma")
    assert {h["id"] for h in hits} == {"acme-design-system"}
    hits = query(public, ["build"])
    assert {h["id"] for h in hits} == {
        "acme-design-system",
        "freelance-shop-sites",
        "plant-app-build",
    }
    assert all({"build"} <= set(h["tags"]) for h in hits)


def test_query_result_fields(public: dict[str, Any]) -> None:
    (hit,) = query(public, ["retail"])
    assert hit == {
        "id": "acme-checkout",
        "entry": "acme",
        "org": "Acme",
        "tags": ["ux", "retail"],
        "verified": True,
        "text": "Designed a new checkout flow that cut drop-off by 12%.",
    }


def test_query_never_returns_hidden_bullets() -> None:
    master = copy.deepcopy(MASTER)
    _bullet(master, "acme-accessibility")["exclude_from"] = ["all"]
    data = public_copy(master, RULES)
    assert query(data, [], "accessibility") == []
    assert query(public_copy(MASTER, RULES), [], "accessibility")


# CLI


def test_cli_public(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["public", "--repo", str(DATA)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1 and "source_files" not in data


def test_cli_public_refuses_a_leak(
    make_repo: Callable[[Any], Path],
    master: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    master["roles"][0]["bullets"][0]["text"] = "Email someone@example.org for details."
    repo = make_repo(master)
    assert main(["public", "--repo", str(repo)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private data" in captured.err


def test_cli_query_refuses_a_leak(
    make_repo: Callable[[Any], Path],
    master: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    master["roles"][0]["bullets"][0]["text"] = "Email someone@example.org for details."
    repo = make_repo(master)
    assert main(["query", "--repo", str(repo)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private data" in captured.err


def test_cli_query_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["query", "--repo", str(DATA), "--tags", "retail", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 1
    assert report["command"] == "query"
    assert report["bullets"] and all("retail" in b["tags"] for b in report["bullets"])


def test_cli_query_text(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["query", "--repo", str(DATA), "--tags", "ux, build"]) == 0
    out = capsys.readouterr().out
    assert "acme-design-system  [acme] ux,build\n" in out
    assert "plant-app-build  [plant-app] build,ux  (not verified)" in out
    assert out.endswith("2 bullets.\n")


# Exit code 2 for invalid input (docs/SPEC.md section 6)


@pytest.mark.parametrize("command", ["public", "query"])
def test_cli_no_master_exit_2(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([command, "--repo", str(tmp_path)]) == 2
    assert "No master.yaml found" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["public", "query"])
def test_cli_bad_yaml_exit_2(
    command: str,
    make_repo: Callable[[Any], Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_repo("roles: [unclosed\n")
    assert main([command, "--repo", str(repo)]) == 2
    assert "not valid YAML" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["public", "query"])
def test_cli_no_rules_exit_2(
    command: str,
    make_repo: Callable[[Any], Path],
    master: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_repo(master)
    (repo / "rules.yaml").unlink()
    assert main([command, "--repo", str(repo)]) == 2
    assert "rules.yaml does not exist" in capsys.readouterr().err
