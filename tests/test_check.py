"""`facts check`: the text rules R-1 to R-7, the report and the CLI."""

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from darzi.cli import main
from darzi.facts.check import NESTED_KEYS, RULE_KEYS, load_run, require_keys
from darzi.facts.repo import InputError

from .checking import (
    ACME,
    BASE,
    BASE_MAP,
    GLOBEX,
    MASTER,
    RULES,
    findings,
    found,
    master_with,
    tailored,
    write_run,
)
from .conftest import DATA


@pytest.fixture
def base() -> dict[str, Any]:
    return tailored()


def test_base_passes(base: dict[str, Any]) -> None:
    assert found(base) == []


def test_base_reaches_the_rules(base: dict[str, Any]) -> None:
    """Control: the base has text of each kind that the rules read."""
    assert base["summary"] and base["personalInfo"]["title"]
    assert all(e["description"] for e in base["workExperience"])
    assert base["personalProjects"] and len(base["education"]) == 2
    assert base["additional"]["technicalSkills"]


# R-1 contact


def test_r1_private_number_in_letter(base: dict[str, Any]) -> None:
    assert found(base, "Call me at 555-0199.", only="R-1") == [
        ("R-1", "cover_letter.md:1")
    ]


def test_r1_other_email_in_bullet(base: dict[str, Any]) -> None:
    base["workExperience"][GLOBEX]["description"][0] += " Mail someone@example.org."
    assert found(base) == [("R-1", "workExperience[1].description[0]")]


def test_r1_other_phone(base: dict[str, Any]) -> None:
    base["personalInfo"]["phone"] = "(555) 555-0123"
    assert found(base) == [("R-1", "personalInfo.phone")]


def _ten_digit_rules() -> dict[str, Any]:
    rules = copy.deepcopy(RULES)
    rules["public_contact"]["phone"] = "555-555-0100"
    return rules


@pytest.mark.parametrize("shown", ["(555) 555-0100", "555.555.0100", "555 555 0100"])
def test_r1_public_phone_other_format_passes(base: dict[str, Any], shown: str) -> None:
    rules = _ten_digit_rules()
    base["personalInfo"]["phone"] = shown
    assert found(base, rules=rules) == []
    base["personalInfo"]["phone"] = shown.replace("0100", "0101")  # control
    assert found(base, rules=rules) == [("R-1", "personalInfo.phone")]


def test_r1_missing_email(base: dict[str, Any]) -> None:
    del base["personalInfo"]["email"]
    assert found(base) == [("R-1", "personalInfo.email")]


def test_r1_missing_phone(base: dict[str, Any]) -> None:
    base["personalInfo"]["phone"] = ""
    assert found(base) == [("R-1", "personalInfo.phone")]


# The same comparison as the PV-3 scan (see test_public.py)


def test_r1_public_phone_with_country_code(base: dict[str, Any]) -> None:
    rules = copy.deepcopy(RULES)
    rules["public_contact"]["phone"] = "+1 555-555-0100"
    base["personalInfo"]["phone"] = "555-555-0100"
    assert found(base, rules=rules) == []
    base["personalInfo"]["phone"] = "556-555-0100"  # same last seven digits
    assert found(base, rules=rules) == [("R-1", "personalInfo.phone")]


def test_r1_private_email_term_gives_one_finding(base: dict[str, Any]) -> None:
    result = findings(base, "Mail test.private@example.org.")
    assert [(f.rule, f.location) for f in result if f.rule == "R-1"] == [
        ("R-1", "cover_letter.md:1")
    ]
    assert "Private contact" in next(f for f in result if f.rule == "R-1").message


def test_r1_private_phone_term_gives_one_finding(base: dict[str, Any]) -> None:
    rules = copy.deepcopy(RULES)
    rules["private_contact_patterns"]["terms"].append("555-555-0177")
    result = findings(base, "Call 555-555-0177.", rules=rules)
    r1 = [f for f in result if f.rule == "R-1"]
    assert [f.location for f in r1] == ["cover_letter.md:1"]
    assert "Private contact" in r1[0].message
    # Control: with no term, the phone comparison finds the same number.
    result = findings(base, "Call 555-555-0177.")
    r1 = [f for f in result if f.rule == "R-1"]
    assert [f.location for f in r1] == ["cover_letter.md:1"]
    assert "is not the public phone" in r1[0].message


def test_r1_email_ignores_letter_case(base: dict[str, Any]) -> None:
    base["personalInfo"]["email"] = "TEST.PERSON@EXAMPLE.COM"
    assert found(base) == []


# R-2 years


def test_r2_role_dates_pass(base: dict[str, Any]) -> None:
    base["workExperience"][ACME]["years"] = "Mar 2021 to Present"
    assert found(base) == []
    assert found(base, "I have worked at Acme since 2021.", only="R-2") == []


@pytest.mark.parametrize(
    "text", ["Ten years of craft.", "15 years in design.", "Over 3+ years of work."]
)
def test_r2_years_in_letter(base: dict[str, Any], text: str) -> None:
    assert found(base, text, only="R-2") == [("R-2", "cover_letter.md:1")]


def test_r2_years_in_summary(base: dict[str, Any]) -> None:
    base["summary"] = "Product Designer with twenty years of experience."
    assert found(base) == [("R-2", "summary")]


# R-3 banned words and phrases


def test_r3_near_words_pass(base: dict[str, Any]) -> None:
    base["summary"] = "Product Designer. Leveraging is not a banned word."
    assert found(base, only="R-3") == []


def test_r3_banned_phrase(base: dict[str, Any]) -> None:
    base["summary"] += " Passionate about forms."
    assert found(base) == [("R-3", "summary")]


def test_r3_banned_word_in_letter(base: dict[str, Any]) -> None:
    assert found(base, "I leveraged the design system.") == [
        ("R-3", "cover_letter.md:1")
    ]


# R-4 pronouns


def test_r4_cover_letter_may_use_i(base: dict[str, Any]) -> None:
    assert found(base, "I built the design system in Figma.") == []


def test_r4_pronoun_in_bullet(base: dict[str, Any]) -> None:
    base["workExperience"][ACME]["description"][0] = (
        "I designed a new checkout flow that cut drop-off by 12%."
    )
    assert found(base, only="R-4") == [("R-4", "workExperience[0].description[0]")]


def test_r4_pronoun_in_summary(base: dict[str, Any]) -> None:
    base["summary"] = "My work turns ideas into tools."
    assert found(base) == [("R-4", "summary")]


# R-5 education and credentials


def test_r5_masters_degree(base: dict[str, Any]) -> None:
    base["education"][0]["degree"] = "Master's degree, Graphic Design"
    assert ("R-5", "education[0].degree") in found(base)


def test_r5_coursework_label_missing(base: dict[str, Any]) -> None:
    base["education"][1]["degree"] = "Human-Computer Interaction"
    assert found(base) == [
        ("R-5", "education[1].institution"),
        ("R-8", "education[1].degree"),
    ]


def test_r5_coursework_label_missing_under_the_long_name(
    base: dict[str, Any],
) -> None:
    long_name = "Example State University School of Continuing Professional Studies"
    base["education"][1]["institution"] = long_name
    assert found(base) == []  # with the label, the long name passes
    base["education"][1]["degree"] = "Human-Computer Interaction"
    assert found(base) == [
        ("R-5", "education[1].institution"),
        ("R-8", "education[1].degree"),
    ]


def test_r5_reads_coursework_ids(base: dict[str, Any]) -> None:
    """With no coursework id, the same edit gives only R-8."""
    rules = copy.deepcopy(RULES)
    rules["education_forbidden"]["coursework_ids"] = []
    base["education"][1]["degree"] = "Human-Computer Interaction"
    assert found(base, rules=rules) == [("R-8", "education[1].degree")]


@pytest.mark.parametrize("skill", ["UX certified", "UX certificate"])
def test_r5_credential_wording_fails(base: dict[str, Any], skill: str) -> None:
    base["additional"]["technicalSkills"].append(skill)
    assert [r for r, _ in found(base)] == ["R-5", "R-19"]  # not a fact skill either


@pytest.mark.parametrize("skill", ["Certificate in Research", "UX training"])
def test_r5_credential_wording_passes(base: dict[str, Any], skill: str) -> None:
    base["additional"]["technicalSkills"].append(skill)
    assert [r for r, _ in found(base)] == ["R-19"]


# R-6 removed and doubtful items


@pytest.mark.parametrize("term", ["Paper Crane Club", "Night Shift Award"])
def test_r6_removed_term_as_skill_once(base: dict[str, Any], term: str) -> None:
    skills = base["additional"]["technicalSkills"]
    skills.append(term)
    where = f"additional.technicalSkills[{len(skills) - 1}]"
    assert found(base) == [("R-6", where), ("R-19", where)]


def test_r6_removed_term_in_bullet(base: dict[str, Any]) -> None:
    base["workExperience"][ACME]["description"][1] += " Ran the Paper Crane Club."
    assert found(base, only="R-6") == [("R-6", "workExperience[0].description[1]")]


def test_r6_word_boundaries(base: dict[str, Any]) -> None:
    letter = "I visited the Paper Crane Clubs fair."
    assert found(base, letter, only="R-6") == []
    assert found(base, "I visited the Paper Crane Club.", only="R-6") == [
        ("R-6", "cover_letter.md:1")
    ]


# R-7 inflation


def test_r7_summary_claim(base: dict[str, Any]) -> None:
    base["summary"] = "Led the design team at Acme."
    assert found(base) == [("R-7", "summary")]


LEAD = "Led the checkout team of four."


def _with_lead_bullet(entries: dict[str, Any]) -> None:
    entries["acme"]["bullets"].append(
        {
            "id": "acme-lead",
            "text": LEAD,
            "tags": ["ux"],
            "evidence": "",
            "verified": True,
            "sources": ["confirmed"],
        }
    )


def test_r7_source_bullet_allows_it(base: dict[str, Any]) -> None:
    master = master_with(_with_lead_bullet)
    base["workExperience"][ACME]["description"][0] = LEAD
    assert found(base, only="R-7", master=master) == []
    assert found(base, only="R-7") == [("R-7", "workExperience[0].description[0]")]


def test_r7_same_phrase_other_entry_fails(base: dict[str, Any]) -> None:
    master = master_with(_with_lead_bullet)
    base["workExperience"][GLOBEX]["description"][0] = LEAD
    assert found(base, only="R-7", master=master) == [
        ("R-7", "workExperience[1].description[0]")
    ]


def test_r7_client_bullet_under_agency(base: dict[str, Any]) -> None:
    """A client entry placed through Globex shows under the Globex entry."""
    client_bullet = "Led the support team for the client portal."
    master = copy.deepcopy(MASTER)
    globex = next(r for r in master["roles"] if r["id"] == "globex")
    client = copy.deepcopy(globex)
    client.update(id="hooli", org="Hooli (client, through Globex)")
    client.pop("display_org")
    client["bullets"] = [{**globex["bullets"][0], "id": "hooli-team"}]
    client["bullets"][0]["text"] = client_bullet
    master["roles"].append(client)
    base["workExperience"][GLOBEX]["description"][0] = client_bullet
    assert found(base, only="R-7", master=master) == []
    assert found(base, only="R-7") == [("R-7", "workExperience[1].description[0]")]


# The report names the rule, the location and the exact text


def test_finding_has_rule_location_and_text(base: dict[str, Any]) -> None:
    base["summary"] = "Spearheaded design."
    (finding,) = [f for f in findings(base) if f.severity == "error"]
    assert (finding.rule, finding.location, finding.text) == (
        "R-3",
        "summary",
        "Spearheaded design.",
    )
    assert "'Spearheaded'" in finding.message


def test_findings_are_sorted_by_rule_number(base: dict[str, Any]) -> None:
    base["additional"]["technicalSkills"].append("Sketch")  # R-19
    base["summary"] = "Spearheaded design."  # R-3
    base["personalInfo"]["email"] = "tp@example.com"  # R-1
    result = [f.rule for f in findings(base) if f.severity == "error"]
    assert result == ["R-1", "R-3", "R-19"]


# A rules.yaml or base.map.json that lacks a key: InputError, not a KeyError


def _rule_key_paths() -> list[tuple[str, str]]:
    paths = [(name, key) for name, keys in RULE_KEYS.items() for key in keys]
    paths += [(name, key) for name in RULE_KEYS for key in ("id", "severity")]
    return paths


@pytest.mark.parametrize(("name", "key"), _rule_key_paths())
def test_missing_rule_key_is_input_error(name: str, key: str) -> None:
    rules = copy.deepcopy(RULES)
    del rules[name][key]
    with pytest.raises(InputError, match=f"rules.yaml: {name} has no key {key}\\."):
        findings(tailored(), "I built it.", rules=rules)


@pytest.mark.parametrize("name", list(RULE_KEYS))
def test_missing_rule_entry_is_input_error(name: str) -> None:
    rules = copy.deepcopy(RULES)
    del rules[name]
    with pytest.raises(InputError, match=f"rules.yaml has no entry {name}\\."):
        findings(tailored(), rules=rules)


@pytest.mark.parametrize(
    ("name", "value", "key"),
    [(n, v, k) for n, v, keys, _ in NESTED_KEYS for k in keys],
)
def test_missing_nested_rule_key_is_input_error(
    name: str, value: str, key: str
) -> None:
    rules = copy.deepcopy(RULES)
    item = rules[name][value]
    del (item[0] if isinstance(item, list) else item)[key]
    with pytest.raises(InputError, match=f"has no key {key}\\."):
        findings(tailored(), rules=rules)


def test_rules_that_are_not_a_mapping() -> None:
    with pytest.raises(InputError, match="rules.yaml has no rule entries"):
        require_keys(None, BASE_MAP)  # an empty rules.yaml


def _other_rule_keys() -> list[tuple[str, str]]:
    """Keys of the synthetic rules.yaml that check reads with a default or not."""
    listed = set(_rule_key_paths())
    return [
        (name, key)
        for name, entry in RULES.items()
        if isinstance(entry, dict)
        for key in entry
        if (name, key) not in listed
    ]


@pytest.mark.parametrize(("name", "key"), _other_rule_keys())
def test_other_rule_keys_are_not_required(name: str, key: str) -> None:
    """Each other key can be absent: check runs to the end with no KeyError."""
    rules = copy.deepcopy(RULES)
    del rules[name][key]
    letter = "I want to join the Payments team.\nTest Person\n555-0100\n"
    result = findings(tailored(), letter, "Payments team.", rules=rules)
    assert isinstance(result, list)


@pytest.mark.parametrize("key", ["section", "rm_id"])
def test_missing_map_key_is_input_error(key: str) -> None:
    the_map = copy.deepcopy(BASE_MAP)
    del the_map["entries"][1][key]
    with pytest.raises(
        InputError, match=f"base.map.json: entries\\[1\\] has no key {key}\\."
    ):
        findings(tailored(), the_map=the_map)


def test_map_entry_that_is_not_an_object() -> None:
    the_map = copy.deepcopy(BASE_MAP)
    the_map["entries"][0] = "acme"
    with pytest.raises(InputError, match="entries\\[0\\] has no key section"):
        findings(tailored(), the_map=the_map)


# CLI: run folder, check.json, exit codes


def _check(run: Path, *extra: str, repo: Path = DATA) -> int:
    return main(["check", str(run), "--repo", str(repo), *extra])


def test_cli_pass_writes_check_json(tmp_path: Path, base: dict[str, Any]) -> None:
    run = write_run(tmp_path / "run", base, "I built the design system in Figma.\n")
    before = (run / "tailored.json").read_bytes()
    assert _check(run) == 0
    report = json.loads((run / "check.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == 1 and report["ok"] is True
    assert report["command"] == "check" and report["errors"] == 0
    assert report["warnings"] == 3  # R-16: three bullets are not verified
    assert (run / "tailored.json").read_bytes() == before


def test_cli_errors_exit_1(
    tmp_path: Path, base: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    base["summary"] = "Spearheaded design for ten years."
    run = write_run(tmp_path / "run", base)
    assert _check(run) == 1
    out = capsys.readouterr().out
    assert "error R-2 summary:" in out and "error R-3 summary:" in out
    assert "text: Spearheaded design for ten years." in out
    assert out.endswith(f"{run}: 2 errors, 3 warnings.\n")
    report = json.loads((run / "check.json").read_text(encoding="utf-8"))
    assert report["errors"] == 2 and report["ok"] is False


def test_cli_json(
    tmp_path: Path, base: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    base["additional"]["technicalSkills"].append("Sketch")
    run = write_run(tmp_path / "run", base)
    assert _check(run, "--json") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 1 and report["command"] == "check"
    errors = [f for f in report["findings"] if f["severity"] == "error"]
    assert errors == [
        {
            "rule": "R-19",
            "severity": "error",
            "location": "additional.technicalSkills[4]",
            "message": "Skill is not an exact master.yaml skill name. Use the "
            "master name or remove it.",
            "text": "Sketch",
        }
    ]
    assert json.loads((run / "check.json").read_text(encoding="utf-8")) == report


def test_cli_resume_data_wrapper(tmp_path: Path, base: dict[str, Any]) -> None:
    run = write_run(tmp_path / "run", {"resume_data": base})
    assert _check(run) == 0


def test_cli_reads_job_and_letter(tmp_path: Path, base: dict[str, Any]) -> None:
    letter = "I want to join the Payments team.\n"
    run = write_run(tmp_path / "run", base, letter)
    assert _check(run) == 1  # R-22: no job.md
    shutil.rmtree(run)
    run = write_run(tmp_path / "run", base, letter, job="Payments team, Springfield.")
    assert _check(run) == 0


def test_cli_warning_alone_exit_0(
    tmp_path: Path, base: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = {"files": [{"path": "out/resume.pdf", "pages": 3}]}
    run = write_run(tmp_path / "run", base, manifest=manifest)
    assert _check(run) == 0
    assert "warning R-18 manifest.json: out/resume.pdf:" in capsys.readouterr().out


@pytest.mark.parametrize("content", ["{not json", '{"title": "no personalInfo"}'])
def test_cli_invalid_input_exit_2(
    tmp_path: Path, content: str, capsys: pytest.CaptureFixture[str]
) -> None:
    run = write_run(tmp_path / "run", content)
    assert _check(run) == 2
    assert capsys.readouterr().err.startswith("facts check: ")
    assert not (run / "check.json").exists()


def test_cli_missing_tailored_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = write_run(tmp_path / "run", {})
    (run / "tailored.json").unlink()
    assert _check(run) == 2
    err = capsys.readouterr().err
    assert "tailored.json does not exist. Copy base.json to tailored.json first." in err
    assert not (run / "check.json").exists()


def test_cli_no_folder_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _check(tmp_path / "missing") == 2
    assert "is not a folder. Pass a run folder." in capsys.readouterr().err


def test_cli_map_with_no_entries_exit_2(
    tmp_path: Path, base: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    run = write_run(tmp_path / "run", base, the_map={"type": "ux"})
    assert _check(run) == 2
    assert "has no entries list" in capsys.readouterr().err


def test_load_run_no_folder(tmp_path: Path) -> None:
    with pytest.raises(InputError):
        load_run(tmp_path / "missing")


def test_load_run_reads_each_file(tmp_path: Path, base: dict[str, Any]) -> None:
    manifest = {"files": []}
    run = write_run(tmp_path / "run", base, "Letter.", job="Job.", manifest=manifest)
    files = load_run(run)
    assert files.tailored == base and files.base == BASE
    assert files.the_map == BASE_MAP and files.manifest == manifest
    assert (files.letter, files.job) == ("Letter.", "Job.")


# CLI: a rules.yaml or base.map.json that lacks a key (exit 2, no traceback)


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    shutil.copytree(DATA, root)
    return root


def test_cli_rules_with_no_coursework_ids_exit_2(
    tmp_path: Path,
    data_root: Path,
    base: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    rules = data_root / "rules.yaml"
    text = rules.read_text(encoding="utf-8")
    line = "  coursework_ids: [example-state-hci]\n"
    assert line in text
    rules.write_text(text.replace(line, ""), encoding="utf-8")
    run = write_run(tmp_path / "run", base)
    assert _check(run, repo=data_root) == 2
    captured = capsys.readouterr()
    assert captured.err == (
        "facts check: rules.yaml: education_forbidden has no key coursework_ids. "
        "Add it to rules.yaml.\n"
    )
    assert "Traceback" not in captured.out + captured.err
    assert not (run / "check.json").exists()
    assert main(["validate", "--repo", str(data_root)]) == 0  # the schema permits it


def test_cli_map_entry_with_a_missing_key_exit_2(
    tmp_path: Path, base: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    the_map = copy.deepcopy(BASE_MAP)
    del the_map["entries"][2]["rm_id"]
    run = write_run(tmp_path / "run", base, the_map=the_map)
    assert _check(run) == 2
    captured = capsys.readouterr()
    assert captured.err == (
        "facts check: base.map.json: entries[2] has no key rm_id. "
        "Run facts export again.\n"
    )
    assert "Traceback" not in captured.out + captured.err
    assert not (run / "check.json").exists()
