"""The `facts` command: help, version, planned commands and `facts validate`."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from darzi import __version__
from darzi.cli import PLANNED, main

from .conftest import DATA

# Each command of docs/SPEC.md section 6, by the work item that builds it.
SPEC_COMMANDS = {
    "validate",
    "public",
    "query",
    "check",
    "export",
    "new-run",
    "fit",
    "render",
    "approve",
    "finalize",
    "answers",
    "fill-plan",
    "submitted",
    "status",
    "doctor",
    "workflow",
}


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: facts" in capsys.readouterr().out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_each_spec_command_is_built_or_planned() -> None:
    built = {"validate", "public", "query", "export"}
    assert built | set(PLANNED) == SPEC_COMMANDS
    assert built.isdisjoint(PLANNED)


@pytest.mark.parametrize("command", sorted(PLANNED))
def test_planned_command_says_not_built(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([command]) == 2
    err = capsys.readouterr().err
    assert "not built yet" in err
    assert f"work item {PLANNED[command]}." in err


@pytest.mark.parametrize("command", ["submit", "rm", "next", "suggest"])
def test_unknown_command_is_refused(command: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main([command])
    assert exc.value.code == 2


# facts validate


def test_validate_synthetic_data_root(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", "--repo", str(DATA)]) == 0
    assert "0 errors, 0 warnings" in capsys.readouterr().out


def test_validate_finds_the_data_root_from_a_subfolder(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(DATA / "selections")
    assert main(["validate"]) == 0
    assert "0 errors, 0 warnings" in capsys.readouterr().out


def test_validate_ok_exit_0(
    make_repo: Callable[[Any], Path],
    master: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_repo(master)
    assert main(["validate", "--repo", str(repo)]) == 0
    assert "0 errors, 0 warnings" in capsys.readouterr().out


def test_validate_errors_exit_1(
    make_repo: Callable[[Any], Path],
    master: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    master["roles"][0]["type"] = "freelance"
    repo = make_repo(master)
    assert main(["validate", "--repo", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "error D-5 roles[acme].type:" in out
    assert "1 errors" in out


def test_validate_rules_errors_exit_1(
    make_repo: Callable[[Any], Path],
    master: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_repo(master)
    rules = (repo / "rules.yaml").read_text(encoding="utf-8")
    (repo / "rules.yaml").write_text(
        rules.replace("severity: warning", "severity: fatal", 1), encoding="utf-8"
    )
    assert main(["validate", "--repo", str(repo)]) == 1
    assert "error D-11 rules.yaml: unverified_bullets.severity:" in (
        capsys.readouterr().out
    )


def test_validate_json(
    make_repo: Callable[[Any], Path],
    master: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    master["roles"][0]["sources"] = ["nope"]
    repo = make_repo(master)
    assert main(["validate", "--repo", str(repo), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 1
    assert report["command"] == "validate"
    assert report["ok"] is False
    assert report["findings"] == [
        {
            "rule": "D-3",
            "severity": "error",
            "location": "roles[acme].sources[0]",
            "message": "Source 'nope' is not a key of source_files. "
            "Add it to source_files or fix the id.",
            "text": "",
        }
    ]


def test_validate_bad_yaml_exit_2(
    make_repo: Callable[[Any], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo("roles: [unclosed\n")
    assert main(["validate", "--repo", str(repo)]) == 2
    assert "not valid YAML" in capsys.readouterr().err


def test_validate_bad_selection_yaml_exit_2(
    make_repo: Callable[[Any], Path],
    master: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_repo(master)
    (repo / "selections").mkdir()
    (repo / "selections" / "ux.yaml").write_text("work: [unclosed\n")
    assert main(["validate", "--repo", str(repo)]) == 2
    assert "not valid YAML" in capsys.readouterr().err


def test_validate_no_rules_exit_2(
    make_repo: Callable[[Any], Path],
    master: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_repo(master)
    (repo / "rules.yaml").unlink()
    assert main(["validate", "--repo", str(repo)]) == 2
    assert "rules.yaml does not exist" in capsys.readouterr().err


def test_validate_no_repo_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["validate", "--repo", str(tmp_path)]) == 2
    assert "No master.yaml found" in capsys.readouterr().err


def test_validate_never_writes(
    make_repo: Callable[[Any], Path], master: dict[str, Any]
) -> None:
    """D-7: validate does not change any file."""
    repo = make_repo(master)
    before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    main(["validate", "--repo", str(repo)])
    after = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    assert before == after


def test_validate_warning_only_exit_0(
    make_repo: Callable[[Any], Path],
    master: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    master["roles"][0]["org"] = "X" * 60
    repo = make_repo(master)
    (repo / "selections").mkdir()
    (repo / "selections" / "ux.yaml").write_text("work:\n  - {id: acme}\n")
    assert main(["validate", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "warning D-8 roles[acme]:" in out
    assert "0 errors, 1 warnings" in out


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    """A temporary copy of the synthetic data root that a test can change."""
    root = tmp_path / "root"
    shutil.copytree(DATA, root)
    return root


def _export(root: Path, run: Path) -> int:
    selection = root / "selections" / "corporate-tech.yaml"
    return main(["export", str(selection), "--run", str(run), "--repo", str(root)])


def test_export_writes_utf8_text_and_one_last_newline(
    data_root: Path, tmp_path: Path
) -> None:
    master = data_root / "master.yaml"
    text = master.read_text(encoding="utf-8")
    old = "Designed a new checkout flow"
    assert old in text
    master.write_text(
        text.replace(old, "Designed a new caf\u00e9 checkout flow"), encoding="utf-8"
    )
    run = tmp_path / "run"
    assert _export(data_root, run) == 0
    raw = (run / "base.json").read_text(encoding="utf-8")
    assert "caf\u00e9 checkout" in raw  # the character itself, not a \u escape
    assert "\\u00e9" not in raw
    for name in ("base.json", "base.map.json"):
        written = (run / name).read_text(encoding="utf-8")
        assert written.endswith("}\n") and not written.endswith("\n\n"), name
        assert json.loads(written)


def test_export_reads_the_open_questions(
    data_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    questions = data_root / "questions.md"
    text = questions.read_text(encoding="utf-8")
    old = "Affects: acme-accessibility"
    assert old in text
    questions.write_text(text.replace(old, "Affects: globex-portal"), encoding="utf-8")
    run = tmp_path / "run"
    assert _export(data_root, run) == 1
    assert not (run / "base.json").exists()
    assert not run.exists()
    out = capsys.readouterr().out
    assert "error D-21 selection: work[1] globex:" in out
    assert "1 errors, 0 warnings" in out
    # Control: the same question under "Resolved" no longer blocks the export.
    assert "## Resolved\n" in text
    resolved = text.replace(
        "## Resolved\n",
        "## Resolved\n\n- **3.** Synthetic.\n  Affects: globex-portal\n",
    )
    questions.write_text(resolved, encoding="utf-8")
    assert _export(data_root, run) == 0
    assert (run / "base.json").is_file()


def test_report_hides_invisible_characters(
    make_repo: Callable[[Any], Path],
    master: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A finding cannot put a terminal code or a direction mark on the screen."""
    master["roles"][0]["id"] = "acme\u202e\x1b[2J"
    repo = make_repo(master)
    assert main(["validate", "--repo", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "\u202e" not in out and "\x1b" not in out
    assert "error D-2 roles[acme??[2J].id:" in out
