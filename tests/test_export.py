"""Selections and `facts export` (D-20, D-21, PV-2, PV-3)."""

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from darzi.cli import main
from darzi.facts.check_meta import load_open_questions
from darzi.facts.export import build_base, export, fmt_date, fmt_years
from darzi.facts.public import public_copy
from darzi.facts.repo import InputError, load_master, load_rules
from darzi.facts.selection import check_selection, load_selection, selection_hash
from darzi.schemas import SELECTION, load_schema

from .conftest import DATA

RULES = load_rules(DATA)
MASTER = load_master(DATA)
SCHEMA = load_schema(SELECTION)
UX = DATA / "selections" / "ux.yaml"
CORPORATE = DATA / "selections" / "corporate-tech.yaml"


@pytest.fixture
def selection() -> dict[str, Any]:
    """The synthetic ux selection."""
    return load_selection(UX)


@pytest.fixture
def corporate() -> dict[str, Any]:
    """The synthetic corporate-tech selection."""
    return load_selection(CORPORATE)


def errors(
    selection: dict[str, Any],
    questions: dict[str, list[str]] | None = None,
    master: dict[str, Any] | None = None,
    rules: dict[str, Any] | None = None,
) -> list[str]:
    return [
        f.rule
        for f in check_selection(
            selection, SCHEMA, master or MASTER, rules or RULES, questions or {}
        )
    ]


# D-20 / D-21: the synthetic selections and broken selections


@pytest.mark.parametrize("path", [UX, CORPORATE])
def test_synthetic_selection_is_valid(path: Path) -> None:
    assert errors(load_selection(path), load_open_questions(DATA)) == []


Change = Callable[[dict[str, Any]], object]

BAD_FORMAT: list[Change] = [
    lambda s: s.pop("max_pages"),
    lambda s: s.update(tier=4),
    lambda s: s["work"][0].update(note="text is not allowed"),
]


@pytest.mark.parametrize("change", BAD_FORMAT)
def test_d20_format(selection: dict[str, Any], change: Change) -> None:
    change(selection)
    findings = check_selection(selection, SCHEMA, MASTER, RULES, {})
    assert [f.rule for f in findings] == ["D-20"]
    assert findings[0].message.endswith("Fix it to match the selection schema.")


def test_d21_unknown_entry(selection: dict[str, Any]) -> None:
    selection["work"].append({"id": "initech", "bullets": []})
    assert errors(selection) == ["D-21"]


def test_d21_bullet_of_other_entry(selection: dict[str, Any]) -> None:
    selection["work"][0]["bullets"].append("globex-portal")
    assert errors(selection) == ["D-21"]


def test_d21_hidden_bullet(selection: dict[str, Any]) -> None:
    master = copy.deepcopy(MASTER)
    acme = next(r for r in master["roles"] if r["id"] == "acme")
    hidden = next(b for b in acme["bullets"] if b["id"] == "acme-accessibility")
    hidden["exclude_from"] = ["all"]
    selection["work"][0]["bullets"].append("acme-accessibility")
    assert errors(selection) == []
    assert errors(selection, master=master) == ["D-21"]


def test_d21_doubtful_bullet(selection: dict[str, Any]) -> None:
    rules = copy.deepcopy(RULES)
    rules["doubtful_items"]["bullet_ids"] = ["acme-checkout"]
    assert errors(selection, rules=rules) == ["D-21"]


def test_d21_excluded_for_this_type(corporate: dict[str, Any]) -> None:
    excluded = [
        r["id"]
        for r in MASTER["roles"]
        if "corporate-tech" in (r.get("exclude_from") or [])
    ]
    assert excluded, "master.yaml has an entry excluded from corporate-tech"
    corporate["work"].append({"id": excluded[0], "bullets": []})
    assert errors(corporate) == ["D-21"]
    corporate["type"] = "ux"
    assert errors(corporate) == []  # allowed for ux


def test_d21_bullet_excluded_for_this_type(corporate: dict[str, Any]) -> None:
    corporate["work"][1]["bullets"].append("globex-support")
    assert errors(corporate) == ["D-21"]


def test_d21_removed_skill(selection: dict[str, Any]) -> None:
    selection["skills"].append("Flash")
    findings = check_selection(selection, SCHEMA, MASTER, RULES, {})
    assert [f.message for f in findings] == ["The owner removed this skill."]


def test_d21_unknown_skill(selection: dict[str, Any]) -> None:
    selection["skills"].append("Not a skill")
    assert errors(selection) == ["D-21"]


def test_d21_headline_must_be_master_text(selection: dict[str, Any]) -> None:
    selection["headline"] = "Visionary Design Leader"
    assert errors(selection) == ["D-21"]


def test_d21_summary_basis_must_be_selected(corporate: dict[str, Any]) -> None:
    corporate["summary_basis"].append("acme-design-system")
    assert errors(corporate) == ["D-21"]


def test_d21_open_question(selection: dict[str, Any]) -> None:
    findings = check_selection(selection, SCHEMA, MASTER, RULES, {"acme": ["7"]})
    assert [f.rule for f in findings] == ["D-21"]
    assert "Wait for the owner." in findings[0].message


def test_d21_open_question_on_a_bullet(selection: dict[str, Any]) -> None:
    selection["work"][0]["bullets"].append("acme-accessibility")
    assert errors(selection, load_open_questions(DATA)) == ["D-21"]


def test_d21_entry_and_bullet_listed_twice(selection: dict[str, Any]) -> None:
    selection["work"][0]["bullets"].append("acme-checkout")
    assert errors(selection) == ["D-21"]
    selection["work"][0]["bullets"].pop()
    selection["work"].append({"id": "acme", "bullets": []})
    assert errors(selection) == ["D-21"]


def test_d21_education(selection: dict[str, Any]) -> None:
    selection["education"].append("example-tech-ms")
    assert errors(selection) == ["D-21"]


# The selection hash


def test_hash_ignores_approval_field(selection: dict[str, Any]) -> None:
    approved = copy.deepcopy(selection)
    approved["approval"] = {"date": "2026-01-15", "content_hash": "0" * 64}
    assert selection_hash(selection) == selection_hash(approved)


def test_hash_follows_content(selection: dict[str, Any]) -> None:
    before = selection_hash(selection)
    selection["skills"].pop()
    assert selection_hash(selection) != before


def test_hash_is_sha256_of_canonical_json() -> None:
    selection = {"type": "ux", "headline": "Diseñador", "approval": {"date": None}}
    canonical = '{"headline":"Diseñador","type":"ux"}'
    assert selection_hash(selection) == hashlib.sha256(canonical.encode()).hexdigest()
    assert selection_hash(selection) == (
        "b58b98c8b22a6146bc8d6812096037b26cf80ae56c4232947da333193b89b1b1"
    )


# Export: format, public data only, privacy scan


def test_dates() -> None:
    assert fmt_years({"start": "2012-03", "end": "2014-11"}) == "Mar 2012 - Nov 2014"
    assert fmt_years({"start": "2019-08", "end": "2019-08"}) == "Aug 2019"
    assert fmt_years({"start": "2021-01", "end": "present"}) == "Jan 2021 - Present"
    assert fmt_years({"start": 2016, "end": 2017}) == "2016 - 2017"
    assert fmt_years({"start": 2024, "end": ""}) == "2024"
    assert fmt_date(None) == ""


def test_export_uses_display_names_and_exact_text(selection: dict[str, Any]) -> None:
    base, base_map = build_base(public_copy(MASTER, RULES), selection, "sel.yaml")
    acme = base["workExperience"][0]
    assert acme["company"] == "Acme"
    assert acme["title"] == "Senior Product Designer"
    assert acme["years"] == "Mar 2021 - Present"
    master_text = {
        b["id"]: " ".join(b["text"].split())
        for e in MASTER["roles"] + MASTER["projects"]
        for b in e["bullets"]
    }
    entry = base_map["entries"][0]
    assert acme["description"] == [master_text[i] for i in entry["bullets"]]
    assert base_map["type"] == "ux" and base_map["max_pages"] == 2
    assert base_map["selection"] == "sel.yaml"
    assert base_map["summary_basis"] == ["acme-checkout", "acme-design-system"]
    assert [m["displayName"] for m in base["sectionMeta"]][3] == "Projects"
    assert base["workExperience"][3]["condensed"] is True


def test_export_personal_info_and_summary(selection: dict[str, Any]) -> None:
    base, _ = build_base(public_copy(MASTER, RULES), selection, "sel.yaml")
    assert base["personalInfo"] == {
        "name": "Test Person",
        "title": "Product Designer",
        "email": "test.person@example.com",
        "phone": "555-0100",
        "location": "Springfield, ST",
        "website": "https://portfolio.example.com",
    }
    assert base["summary"] == "Product Designer."
    skills = ["Figma", "User research", "React", "Swift"]
    assert base["additional"] == {"technicalSkills": skills}


def test_export_projects_and_education(selection: dict[str, Any]) -> None:
    base, base_map = build_base(public_copy(MASTER, RULES), selection, "sel.yaml")
    assert base["personalProjects"] == [
        {
            "id": 1,
            "name": "Plant Care App",  # an independent project shows its title
            "role": "Independent",
            "years": "2024",
            "description": ["Built a plant care app in Swift with reminders."],
        }
    ]
    assert base["education"] == [
        {
            "id": 1,
            "institution": "Example University",
            "degree": "B.A. Graphic Design",
            "years": "2006 - 2009",
        },
        {
            "id": 2,
            "institution": "Example State University",
            "degree": "Human-Computer Interaction",
            "years": "2020-09 - 2021",
        },
    ]
    assert [m["section"] for m in base_map["entries"]] == (
        ["workExperience"] * 4 + ["personalProjects"] + ["education"] * 2
    )


def test_export_refuses_a_bullet_that_is_not_public(selection: dict[str, Any]) -> None:
    master = copy.deepcopy(MASTER)
    acme = next(r for r in master["roles"] if r["id"] == "acme")
    acme["bullets"][0]["text"] = "Ran the Paper Crane Club checkout."
    with pytest.raises(InputError, match="not in the public copy"):
        build_base(public_copy(master, RULES), selection, "sel.yaml")


def test_export_refuses_private_data(selection: dict[str, Any]) -> None:
    public = public_copy(MASTER, RULES)
    acme = next(e for e in public["roles"] if e["id"] == "acme")
    picked = next(b for b in acme["bullets"] if b["id"] == "acme-checkout")
    picked["text"] = "Mail someone@example.org."
    with pytest.raises(InputError, match="private data"):
        export(public, selection, "sel.yaml", RULES)


# CLI


def _write(tmp_path: Path, selection: dict[str, Any]) -> Path:
    path = tmp_path / "ux.yaml"
    path.write_text(yaml.safe_dump(selection, sort_keys=False), encoding="utf-8")
    return path


def _export(selection: Path, run: Path, repo: Path) -> int:
    return main(["export", str(selection), "--run", str(run), "--repo", str(repo)])


@pytest.mark.parametrize("path", [UX, CORPORATE])
def test_cli_export_writes_base_and_map(
    tmp_path: Path, path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = tmp_path / "applications" / "run"
    assert _export(path, run, DATA) == 0
    base = json.loads((run / "base.json").read_text(encoding="utf-8"))
    base_map = json.loads((run / "base.map.json").read_text(encoding="utf-8"))
    selection = load_selection(path)
    assert len(base["workExperience"]) == len(selection["work"])
    assert base_map["schema_version"] == 1
    assert base_map["selection"] == f"selections/{path.name}"
    assert (base, base_map) == export(
        public_copy(MASTER, RULES), selection, f"selections/{path.name}", RULES
    )
    assert "facts export: wrote" in capsys.readouterr().out


def test_cli_export_refuses_selection_errors(
    tmp_path: Path, selection: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    selection["skills"].append("Not a skill")
    run = tmp_path / "run"
    assert _export(_write(tmp_path, selection), run, DATA) == 1
    assert not run.exists()
    assert "1 errors, 0 warnings" in capsys.readouterr().out


def test_cli_export_refuses_selection_errors_json(
    tmp_path: Path, selection: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    selection["headline"] = "Visionary Design Leader"
    path = _write(tmp_path, selection)
    run = tmp_path / "run"
    args = ["export", str(path), "--run", str(run), "--repo", str(DATA), "--json"]
    assert main(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["command"] == "export" and report["errors"] == 1
    assert not run.exists()


def test_cli_export_refuses_private_data(
    tmp_path: Path,
    make_repo: Callable[[Any], Path],
    master: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    master["roles"][0]["bullets"][0]["text"] = "Mail someone@example.org."
    repo = make_repo(master)
    selection = {
        "schema_version": 1,
        "type": "ux",
        "tier": 1,
        "headline": "Designer",
        "summary_basis": [],
        "section_order": [["personalInfo", "Personal Info"]],
        "work": [{"id": "acme", "bullets": ["acme-forms"]}],
        "projects": [],
        "education": [],
        "skills": ["Figma"],
        "max_pages": 1,
        "approval": {"date": None, "content_hash": None},
    }
    path = repo / "selections" / "ux.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(selection), encoding="utf-8")
    run = repo / "applications" / "run"
    assert _export(path, run, repo) == 1
    assert not run.exists()
    assert "private data" in capsys.readouterr().err


# Exit code 2 for invalid input (docs/SPEC.md section 6)


def test_cli_export_missing_selection_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = tmp_path / "run"
    assert _export(tmp_path / "none.yaml", run, DATA) == 2
    assert "does not exist" in capsys.readouterr().err
    assert not run.exists()


def test_cli_export_bad_selection_yaml_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "ux.yaml"
    path.write_text("work: [unclosed\n", encoding="utf-8")
    assert _export(path, tmp_path / "run", DATA) == 2
    assert "not valid YAML" in capsys.readouterr().err


def test_cli_export_selection_not_a_mapping_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "ux.yaml"
    path.write_text("- one\n- two\n", encoding="utf-8")
    assert _export(path, tmp_path / "run", DATA) == 2
    assert "expected a mapping" in capsys.readouterr().err


def test_cli_export_no_master_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _export(UX, tmp_path / "run", tmp_path) == 2
    assert "No master.yaml found" in capsys.readouterr().err
    assert not (tmp_path / "run").exists()
