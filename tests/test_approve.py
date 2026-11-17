"""`facts approve`: the review text, the checks and the signed record (W-5).

The tests use synthetic data, a temporary copy of the data root and a key that
the test makes in a temporary folder. No test uses a pseudo-terminal: the
command-level test checks only the refusal, and the steps below the terminal
test take the answer from a function.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from darzi.cli import approve_steps, main
from darzi.config import load_config
from darzi.facts.check import RunFiles
from darzi.facts.repo import load_master
from darzi.facts.selection import is_approved, load_selection
from darzi.runs.approve import (
    one_line,
    run_text,
    safe,
    selection_text,
    unreviewable,
)
from darzi.runs.signing import selection_record, sig_path

from .conftest import DATA
from .signing_root import allowed_line, approve, make_key, setup_signing, signed_copy

RUN = "2026-11-17-acme-product-designer"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A copy of the synthetic data root with a test key and no approval."""
    folder = tmp_path / "root"
    shutil.copytree(DATA, folder)
    setup_signing(folder, tmp_path / "keys")
    return folder


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """A run folder (tailored.json = base.json) in a signed data root."""
    root = signed_copy(tmp_path / "signed")
    run = root / "applications" / RUN
    selection = root / "selections" / "ux.yaml"
    assert main(["export", str(selection), "--run", str(run), "--repo", str(root)]) == 0
    shutil.copy(run / "base.json", run / "tailored.json")
    (run / "cover_letter.md").write_text("Dear team,\nI design forms.\n", "utf-8")
    return run


def _ux(root: Path) -> Path:
    return root / "selections" / "ux.yaml"


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def _no_ask(_prompt: str) -> str:
    pytest.fail("the owner was asked")


def _root_of(run: Path) -> Path:
    return run.parents[1]


def _tailored(run: Path) -> dict[str, Any]:
    return json.loads((run / "tailored.json").read_text(encoding="utf-8"))


def _write_tailored(run: Path, data: dict[str, Any]) -> None:
    (run / "tailored.json").write_text(json.dumps(data), encoding="utf-8")


# --- The terminal test --------------------------------------------------------


def test_approve_refuses_when_stdin_is_not_a_terminal(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A pipe that says "y" is not a person at a terminal.
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    before = _files(root)
    assert main(["approve", str(_ux(root)), "--repo", str(root)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("facts approve: stdin is not a terminal.")
    assert _files(root) == before


def test_the_terminal_test_comes_before_each_file_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    missing = tmp_path / "missing"
    assert main(["approve", str(missing / "x.yaml"), "--repo", str(missing)]) == 1
    assert "stdin is not a terminal" in capsys.readouterr().err


# --- Selection ----------------------------------------------------------------


def test_approve_a_selection(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    yaml_bytes = _ux(root).read_bytes()
    assert approve(root, _ux(root)) == 0
    out = capsys.readouterr().out
    assert "Selection: ux (tier 1), max 2 pages" in out
    assert f"Approved {_ux(root)} on 2026-11-17 (sha256 " in out
    assert ", key SHA256:" in out
    # The selection YAML is never written.
    assert _ux(root).read_bytes() == yaml_bytes
    path = _ux(root)
    settings = load_config(root).approval
    assert is_approved(root, settings, path, load_selection(path)).approved


def test_the_answer_no_writes_nothing(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _files(root)
    assert approve(root, _ux(root), answer="n") == 1
    assert "Not approved. No file was written." in capsys.readouterr().out
    assert _files(root) == before


@pytest.mark.parametrize("answer", ["y", "Y", " yes ", "YES"])
def test_only_y_or_yes_approves(root: Path, answer: str) -> None:
    assert approve(root, _ux(root), answer=answer) == 0


@pytest.mark.parametrize("answer", ["", "n", "yep", "y y", "ok"])
def test_each_other_answer_stops(root: Path, answer: str) -> None:
    assert approve(root, _ux(root), answer=answer) == 1
    assert not selection_record(_ux(root)).exists()


def test_an_approved_selection_is_not_signed_again(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert approve(root, _ux(root)) == 0
    record = selection_record(_ux(root)).read_bytes()
    capsys.readouterr()
    assert approve_steps(_ux(root), root, _no_ask, dt.date(2026, 11, 18)) == 0
    assert "has a valid approval already" in capsys.readouterr().out
    assert selection_record(_ux(root)).read_bytes() == record


def test_approve_again_after_a_change_replaces_the_signature(root: Path) -> None:
    assert approve(root, _ux(root)) == 0
    sig = sig_path(selection_record(_ux(root)))
    old = sig.read_bytes()
    text = _ux(root).read_text(encoding="utf-8")
    _ux(root).write_text(text.replace("max_pages: 2", "max_pages: 1"), "utf-8")
    yaml_bytes = _ux(root).read_bytes()
    assert approve(root, _ux(root)) == 0
    assert sig.read_bytes() != old
    assert _ux(root).read_bytes() == yaml_bytes
    path = _ux(root)
    settings = load_config(root).approval
    assert is_approved(root, settings, path, load_selection(path)).approved


def test_an_old_signature_file_is_removed_before_the_sign_call(root: Path) -> None:
    # ssh-keygen keeps an old .sig when it cannot ask to replace it.
    sig = sig_path(selection_record(_ux(root)))
    sig.write_text("an old signature\n", encoding="utf-8")
    selection_record(_ux(root)).write_text("{}\n", encoding="utf-8")
    assert approve(root, _ux(root)) == 0
    assert sig.read_text(encoding="utf-8").startswith("-----BEGIN SSH SIGNATURE")
    path = _ux(root)
    settings = load_config(root).approval
    assert is_approved(root, settings, path, load_selection(path)).approved


def test_a_selection_with_errors_is_refused(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _ux(root).read_text(encoding="utf-8")
    _ux(root).write_text(text.replace("Swift]", "Flash]"), encoding="utf-8")
    assert approve_steps(_ux(root), root, _no_ask, dt.date(2026, 11, 17)) == 1
    captured = capsys.readouterr()
    assert "The owner removed this skill." in captured.out
    assert "the selection has errors" in captured.err
    assert not selection_record(_ux(root)).exists()


def test_absent_settings_stop_before_the_question(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (root / "allowed_signers").unlink()
    assert approve_steps(_ux(root), root, _no_ask, dt.date(2026, 11, 17)) == 1
    assert "cannot sign" in capsys.readouterr().err
    assert not selection_record(_ux(root)).exists()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('signer = "owner"', 'signer = "-owner"'),
        ('namespace = "darzi-approval"', 'namespace = "-darzi-approval"'),
    ],
    ids=["signer", "namespace"],
)
def test_a_value_that_starts_with_a_dash_stops_before_the_question(
    root: Path, capsys: pytest.CaptureFixture[str], old: str, new: str
) -> None:
    toml = root / "facts.toml"
    text = toml.read_text(encoding="utf-8")
    assert old in text
    toml.write_text(text.replace(old, new), encoding="utf-8")
    assert approve_steps(_ux(root), root, _no_ask, dt.date(2026, 11, 17)) == 1
    err = capsys.readouterr().err
    assert "cannot sign" in err and "not permitted" in err
    assert not selection_record(_ux(root)).exists()
    assert not sig_path(selection_record(_ux(root))).exists()


def test_an_absent_key_file_stops_before_the_question(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "keys" / "approval_key").unlink()
    assert approve_steps(_ux(root), root, _no_ask, dt.date(2026, 11, 17)) == 1
    assert "key file" in capsys.readouterr().err


def test_a_wrong_type_in_facts_toml_gives_exit_2(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = root / "facts.toml"
    text = toml.read_text(encoding="utf-8")
    toml.write_text(text.replace('signer = "owner"', "signer = 1"), "utf-8")
    assert approve_steps(_ux(root), root, _no_ask, dt.date(2026, 11, 17)) == 2
    assert "[approval] signer must be a text" in capsys.readouterr().err


def test_a_failed_sign_call_leaves_no_record(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "keys" / "approval_key").write_text("not a key\n", "utf-8")
    assert approve(root, _ux(root)) == 1
    assert "did not sign" in capsys.readouterr().err
    assert not selection_record(_ux(root)).exists()
    assert not sig_path(selection_record(_ux(root))).exists()


def test_a_signature_that_does_not_verify_leaves_no_record(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # allowed_signers names a different key: the sign call works, the verify
    # call after it fails, and the new pair is removed.
    other = make_key(tmp_path / "other")
    (root / "allowed_signers").write_text(allowed_line(other), "utf-8")
    assert approve(root, _ux(root)) == 1
    assert "does not verify" in capsys.readouterr().err
    assert not selection_record(_ux(root)).exists()
    assert not sig_path(selection_record(_ux(root))).exists()


# --- Run ----------------------------------------------------------------------


def test_approve_a_run(run_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tailored = (run_dir / "tailored.json").read_bytes()
    assert approve(_root_of(run_dir), run_dir) == 0
    out = capsys.readouterr().out
    assert "CONTACT" in out and "COVER LETTER" in out
    assert "facts check: 0 errors," in out
    assert (run_dir / "approval.json").is_file()
    assert (run_dir / "approval.json.sig").is_file()
    assert (run_dir / "check.json").is_file()
    assert (run_dir / "tailored.json").read_bytes() == tailored


def test_a_run_with_check_errors_is_refused(
    run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = _tailored(run_dir)
    data["additional"]["technicalSkills"].append("Flash")
    _write_tailored(run_dir, data)
    assert approve_steps(run_dir, _root_of(run_dir), _no_ask, dt.date.today()) == 1
    assert "facts check found errors" in capsys.readouterr().err
    assert not (run_dir / "approval.json").exists()


def test_a_run_with_an_unshown_key_is_refused(
    run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = _tailored(run_dir)
    data["workExperience"][0]["hiddenNote"] = "Led the team"
    _write_tailored(run_dir, data)
    assert approve_steps(run_dir, _root_of(run_dir), _no_ask, dt.date.today()) == 1
    assert "workExperience[0].hiddenNote" in capsys.readouterr().err
    assert not (run_dir / "approval.json").exists()


def test_the_answer_no_writes_no_run_approval(run_dir: Path) -> None:
    assert approve(_root_of(run_dir), run_dir, answer="n") == 1
    assert not (run_dir / "approval.json").exists()
    assert not (run_dir / "approval.json.sig").exists()


# --- unreviewable() -----------------------------------------------------------

Change = Callable[[dict[str, Any]], object]

REFUSED: list[tuple[str, Change, str]] = [
    ("top-key", lambda t: t.update(notes="x"), "top-level key 'notes'"),
    ("info-key", lambda t: t["personalInfo"].update(fax="x"), "personalInfo.fax"),
    (
        "entry-key",
        lambda t: t["education"][0].update(gpa="4"),
        "education[0].gpa",
    ),
    (
        "additional-key",
        lambda t: t["additional"].update(hobbies=["x"]),
        "additional.hobbies",
    ),
    (
        "custom",
        lambda t: t.update(customSections={"a": {"items": ["x"]}}),
        "customSections (not empty)",
    ),
    ("meta-key", lambda t: t["sectionMeta"][1].update(note="x"), "sectionMeta[1].note"),
    (
        "meta-type",
        lambda t: t["sectionMeta"][1].update(sectionType="html"),
        "sectionMeta[1].sectionType 'html'",
    ),
    (
        "hidden",
        lambda t: t["sectionMeta"][1].update(isVisible=False),
        "sectionMeta[1] is hidden (isVisible is not true)",
    ),
    (
        "style",
        lambda t: t["workExperience"][0].update(descriptionStyles=["hidden"]),
        "workExperience[0].descriptionStyles 'hidden'",
    ),
    (
        "edu-style",
        lambda t: t["education"][0].update(descriptionStyles=["tiny"]),
        "education[0].descriptionStyles 'tiny'",
    ),
    ("summary-type", lambda t: t.update(summary=["x"]), "summary is not text"),
    (
        "info-type",
        lambda t: t["personalInfo"].update(email={"a": 1}),
        "personalInfo.email is not text",
    ),
    (
        "entry-type",
        lambda t: t["workExperience"][0].update(company=["Acme"]),
        "workExperience[0].company is not text",
    ),
    (
        "description-type",
        lambda t: t["workExperience"][0].update(description=[{"a": 1}]),
        "workExperience[0].description is not a list of text",
    ),
    (
        "skills-type",
        lambda t: t["additional"].update(technicalSkills="Figma"),
        "additional.technicalSkills is not a list of text",
    ),
]


@pytest.mark.parametrize(
    ("change", "problem"),
    [(c, p) for _, c, p in REFUSED],
    ids=[i for i, _, _ in REFUSED],
)
def test_unreviewable_finds_each_part(
    run_dir: Path, change: Change, problem: str
) -> None:
    data = _tailored(run_dir)
    assert unreviewable(data) == []
    change(data)
    assert problem in unreviewable(data)


# --- The review text ----------------------------------------------------------


def _run_files(run: Path, letter: str | None = None) -> RunFiles:
    data = _tailored(run)
    return RunFiles(data, letter, data, {"entries": []})


def test_run_text_shows_each_field(run_dir: Path) -> None:
    data = _tailored(run_dir)
    data["personalInfo"]["linkedin"] = "linkedin.example.com/in/test-person"
    data["additional"]["languages"] = ["Esperanto"]
    data["personalProjects"] = [
        {
            "id": 1,
            "name": "Notes App",
            "role": "Builder",
            "years": "2024",
            "website": "https://notes.example.com",
            "github": "https://code.example.com/notes",
            "description": "One line.",
        }
    ]
    text = run_text(RunFiles(data, "Dear team.\n", data, {"entries": []}))
    for part in (
        "  linkedin: linkedin.example.com/in/test-person",
        "LANGUAGES\nEsperanto",
        "SECTIONS: Personal Info, Summary,",
        "- Notes App | Builder | 2024 | https://notes.example.com | "
        "https://code.example.com/notes",
        "    * One line.",
        "Springfield, ST",
        "COVER LETTER\nDear team.",
    ):
        assert part in text, part


def test_run_text_shows_a_line_break_as_a_mark(run_dir: Path) -> None:
    data = _tailored(run_dir)
    data["workExperience"][0]["location"] = "Springfield\nFALSE LINE"
    text = run_text(RunFiles(data, None, data, {"entries": []}))
    assert "Springfield \u23ce FALSE LINE" in text
    assert one_line("a \n  b") == "a \u23ce b"


def test_run_text_hides_escape_codes_and_invisible_characters(run_dir: Path) -> None:
    letter = "Dear team.\x1b[2K\x1b[1A\rHidden\u202e\u200b line\n"
    text = run_text(_run_files(run_dir, letter))
    assert "\x1b" not in text and "\r" not in text
    assert "\u202e" not in text and "\u200b" not in text
    assert "?[2K" in text


def test_selection_text_shows_each_fact(root: Path) -> None:
    text = selection_text(load_selection(_ux(root)), load_master(root))
    lines = text.splitlines()
    assert lines[0] == "Selection: ux (tier 1), max 2 pages"
    assert lines[1] == "Headline: Product Designer"
    assert "PROJECTS" in lines and "WORK" in lines and "EDUCATION" in lines
    assert "  [not verified]" in text
    assert " \u2013 " in text
    assert "(condensed)" in text
    assert lines[-2:] == ["SKILLS", "Figma, User research, React, Swift"]


@pytest.mark.parametrize(
    "hidden",
    [
        "\ufe0f",  # variation selector
        "\U000e0100",  # variation selector supplement
        "\u3164",  # Hangul filler
        "\u2800",  # braille blank
        "\u200b",  # zero-width space
        "\u202e",  # right-to-left override
        "\U000e0041",  # tag character
        "\U0010ffff",  # not assigned
        "\x1b",  # escape
        "\x85",  # C1 control
    ],
)
def test_safe_replaces_hidden_characters(hidden: str) -> None:
    assert safe(f"a{hidden}b") == "a?b"


def test_safe_keeps_normal_text() -> None:
    # Visible non-ASCII: e acute, en dash, curly quotes, i diaeresis.
    text = "Caf\u00e9 \u2013 \u201cquoted\u201d 40% $5 na\u00efve\tok\nnext"
    assert safe(text) == text


def test_safe_keeps_combining_accent() -> None:
    # "e" followed by U+0301 (combining acute accent) is visible text.
    text = "Cafe\u0301 menu"
    assert safe(text) == text
