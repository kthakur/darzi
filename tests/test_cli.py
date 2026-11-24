"""The `facts` command: help, version, the command list and `facts validate`."""

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from darzi import __version__, cli
from darzi.cli import main

from .conftest import DATA
from .docs_text import built_commands
from .signing_root import signed_copy

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


def test_the_built_commands_are_the_commands_of_the_spec() -> None:
    assert built_commands() == SPEC_COMMANDS


def test_a_parsed_command_with_no_handler_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # argparse refuses an unknown command, so give the parser one more command.
    real = cli.build_parser

    def with_extra() -> argparse.ArgumentParser:
        parser = real()
        [sub] = [
            a
            for a in parser._actions  # pyright: ignore[reportPrivateUsage]
            if isinstance(a, argparse._SubParsersAction)  # pyright: ignore[reportPrivateUsage]
        ]
        sub.add_parser("extra")  # pyright: ignore[reportUnknownMemberType]
        return parser

    monkeypatch.setattr(cli, "build_parser", with_extra)
    assert main(["extra"]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "facts extra: unknown command. Run facts --help.\n"


@pytest.mark.parametrize("command", ["submit", "rm", "next", "suggest"])
def test_unknown_command_is_refused(command: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main([command])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "args",
    [["--help"], ["--version"], ["status", "--repo", str(DATA)]],
    ids=["help", "version", "status"],
)
def test_a_new_process_prints_nothing_on_stderr(args: list[str]) -> None:
    # A new process imports each module again, so a warning at import shows.
    code = "import sys; from darzi.cli import main; sys.exit(main(sys.argv[1:]))"
    done = subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout
    assert done.stderr == ""


def test_the_import_adds_only_the_socks_filter() -> None:
    # A new process: the filters after `import darzi.cli`. The filter of
    # trafilatura for InsecureRequestWarning stays, and the SOCKS filter is
    # there. The SOCKS filter does not match a different urllib3 warning.
    code = (
        "import warnings\n"
        "import darzi.cli\n"
        "from darzi.sourcing.fetch import SOCKS_WARNING_MESSAGE\n"
        "from urllib3.exceptions import DependencyWarning, InsecureRequestWarning\n"
        "kept = [f for f in warnings.filters\n"
        "        if f[0] == 'ignore' and f[2] is InsecureRequestWarning]\n"
        "socks = [f for f in warnings.filters if f[0] == 'ignore' and f[1]\n"
        "         and f[1].pattern == SOCKS_WARNING_MESSAGE and f[2] is Warning]\n"
        "assert len(kept) == 1, warnings.filters\n"
        "assert len(socks) == 1, warnings.filters\n"
        "with warnings.catch_warnings(record=True) as seen:\n"
        "    warnings.simplefilter('always')\n"
        "    warnings.filters[:0] = socks\n"
        "    warnings.warn('SOCKS support in urllib3 requires the installation of '\n"
        "        'optional dependencies: specifically, PySocks.  For more '\n"
        "        'information, see https://example.com', DependencyWarning)\n"
        "    warnings.warn('A different warning.', DependencyWarning)\n"
        "assert [str(w.message) for w in seen] == ['A different warning.'], seen\n"
        "print('ok')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "ok\n"
    assert done.stderr == ""


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
    """A temporary copy of the synthetic data root that a test can change.

    A test key in the temporary folder signs both selections (W-5).
    """
    return signed_copy(tmp_path)


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
