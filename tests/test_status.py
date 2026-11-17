"""`facts status` and `runs/status.py` (work item W-4)."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import yaml

from darzi import cli
from darzi.cli import main
from darzi.facts.repo import InputError
from darzi.runs.runs import APPLICATIONS, JOB, RUN_YAML, STATUSES, load_meta, run_name
from darzi.runs.status import RunRow, list_runs

from .conftest import DATA

GLOBEX = "2026-09-14-globex-ux-designer"
INITECH = "2026-10-02-initech-design-technologist"
EXAMPLE_LABS = "2026-11-03-example-labs-product-designer"

EXPECTED_ROWS = [
    {
        "run": GLOBEX,
        "date": "2026-09-14",
        "company": "Globex",
        "role": "UX Designer",
        "type": "ux",
        "status": "submitted",
    },
    {
        "run": INITECH,
        "date": "2026-10-02",
        "company": "Initech",
        "role": "Design Technologist",
        "type": "corporate-tech",
        "status": "finalized",
    },
    {
        "run": EXAMPLE_LABS,
        "date": "2026-11-03",
        "company": "Example Labs",
        "role": "Product Designer",
        "type": None,
        "status": "new",
    },
]

EXPECTED_TEXT = (
    "DATE        COMPANY       ROLE                 TYPE            STATUS\n"
    "2026-09-14  Globex        UX Designer          ux              submitted\n"
    "2026-10-02  Initech       Design Technologist  corporate-tech  finalized\n"
    "2026-11-03  Example Labs  Product Designer     -               new\n"
    "3 runs.\n"
)


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    """A temporary copy of the synthetic data root that a test can change."""
    root = tmp_path / "root"
    shutil.copytree(DATA, root)
    return root


def _status(root: Path, *extra: str) -> int:
    return main(["status", "--repo", str(root), *extra])


# The synthetic runs in tests/data


def test_status_lists_the_synthetic_runs(capsys: pytest.CaptureFixture[str]) -> None:
    assert _status(DATA) == 0
    assert capsys.readouterr().out == EXPECTED_TEXT


def test_status_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert _status(DATA, "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 1
    assert report["command"] == "status"
    assert report["target"] == str(DATA)
    assert report["ok"] is True
    assert (report["errors"], report["warnings"], report["findings"]) == (0, 0, [])
    assert report["runs"] == EXPECTED_ROWS


def test_list_runs_returns_rows_in_folder_name_order() -> None:
    rows, findings = list_runs(DATA)
    assert findings == []
    assert [r.run for r in rows] == [GLOBEX, INITECH, EXAMPLE_LABS]
    assert rows[2] == RunRow(
        EXAMPLE_LABS, "2026-11-03", "Example Labs", "Product Designer", None, "new"
    )


def test_each_synthetic_run_is_consistent() -> None:
    folders = sorted(p for p in (DATA / APPLICATIONS).iterdir() if p.is_dir())
    assert [p.name for p in folders] == [GLOBEX, INITECH, EXAMPLE_LABS]
    for folder in folders:
        assert sorted(p.name for p in folder.iterdir()) == [JOB, RUN_YAML]
        meta = load_meta(folder)
        created = dt.date.fromisoformat(meta["created"])
        assert folder.name == run_name(meta["company"], meta["role"], created)
        assert meta["status"] in STATUSES
        assert meta["schema_version"] == 1
        assert "resume_matcher" not in meta
        assert (folder / JOB).read_text(encoding="utf-8").strip()


def test_the_synthetic_applications_folder_holds_only_the_three_runs() -> None:
    # A stray write into the real tests/data (for example by status) fails here.
    assert sorted(os.listdir(DATA)) == [
        "answers.yaml",
        "applications",
        "facts.toml",
        "master.yaml",
        "questions.md",
        "rules.yaml",
        "selections",
    ]
    folder = DATA / APPLICATIONS
    assert sorted(os.listdir(folder)) == [GLOBEX, INITECH, EXAMPLE_LABS]
    for name in (GLOBEX, INITECH, EXAMPLE_LABS):
        assert sorted(os.listdir(folder / name)) == [JOB, RUN_YAML]
        assert (folder / name / JOB).is_file() and (folder / name / RUN_YAML).is_file()


# The data root of test_status_writes_nothing. The test writes it from this fixed
# content, so a file or a folder that an earlier test left in tests/data cannot
# hide a write.
FIXED_FOLDERS = [
    "applications",
    "applications/2026-10-20-acme-tester",
    "applications/2026-10-21-acme-writer",
]
FIXED_FILES = {
    "master.yaml": b"{}\n",
    "applications/2026-10-20-acme-tester/job.md": b"Acme needs a tester.\n",
    "applications/2026-10-20-acme-tester/run.yaml": (
        b"schema_version: 1\ncompany: Acme\nrole: Tester\ncreated: '2026-10-20'\n"
        b"type: null\nselection_hash: null\nstatus: new\n"
    ),
    # No run.yaml: status gives an error for this run.
    "applications/2026-10-21-acme-writer/job.md": b"Acme needs a writer.\n",
}


def _snapshot(folder: Path) -> dict[str, bytes | None]:
    """Return each file (with its content) and each folder (None) under `folder`."""
    return {
        p.relative_to(folder).as_posix(): p.read_bytes() if p.is_file() else None
        for p in folder.rglob("*")
    }


def test_status_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "fixed"
    for name in FIXED_FOLDERS:
        (root / name).mkdir(parents=True)
    for name, content in FIXED_FILES.items():
        (root / name).write_bytes(content)
    expected: dict[str, bytes | None] = {name: None for name in FIXED_FOLDERS}
    expected.update(FIXED_FILES)
    assert _snapshot(root) == expected
    assert _status(root) == 1
    assert _snapshot(root) == expected
    assert _status(root, "--json") == 1
    assert _snapshot(root) == expected
    (root / "applications/2026-10-21-acme-writer/job.md").unlink()
    (root / "applications/2026-10-21-acme-writer").rmdir()
    del expected["applications/2026-10-21-acme-writer/job.md"]
    del expected["applications/2026-10-21-acme-writer"]
    assert _status(root) == 0
    assert _status(root, "--json") == 0
    assert _snapshot(root) == expected


# No runs


def test_status_with_no_applications_folder(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.rmtree(data_root / APPLICATIONS)
    assert _status(data_root) == 0
    assert capsys.readouterr().out == "0 runs.\n"
    assert _status(data_root, "--json") == 0
    assert json.loads(capsys.readouterr().out)["runs"] == []


def test_status_with_an_empty_applications_folder(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.rmtree(data_root / APPLICATIONS)
    (data_root / APPLICATIONS).mkdir()
    (data_root / APPLICATIONS / "notes.txt").write_text("x", encoding="utf-8")
    assert _status(data_root) == 0
    assert capsys.readouterr().out == "0 runs.\n"


# Errors and warnings


def test_a_run_with_no_run_yaml_is_an_error(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (data_root / APPLICATIONS / INITECH / RUN_YAML).unlink()
    assert _status(data_root) == 1
    out = capsys.readouterr().out
    assert f"error status {INITECH}: " in out and "does not exist" in out
    assert "Globex" in out and "Example Labs" in out and "Initech" not in out
    assert out.endswith("2 runs.\n")


def test_a_run_with_no_run_yaml_json(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (data_root / APPLICATIONS / INITECH / RUN_YAML).unlink()
    assert _status(data_root, "--json") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False and report["errors"] == 1
    assert report["runs"] == [EXPECTED_ROWS[0], EXPECTED_ROWS[2]]
    [finding] = report["findings"]
    assert (finding["rule"], finding["severity"], finding["location"]) == (
        "status",
        "error",
        INITECH,
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("company: [unclosed\n", "not valid YAML"),
        ("- a\n- b\n", "not a mapping"),
    ],
)
def test_a_bad_run_yaml_is_an_error(
    data_root: Path, text: str, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    (data_root / APPLICATIONS / GLOBEX / RUN_YAML).write_text(text, encoding="utf-8")
    assert _status(data_root) == 1
    out = capsys.readouterr().out
    assert f"error status {GLOBEX}: " in out and message in out
    assert "Initech" in out and out.endswith("2 runs.\n")


BAD_DATE = "created: 2026-09-31"  # not quoted, and September has 30 days


def _write_bad_date(root: Path) -> None:
    path = root / APPLICATIONS / INITECH / RUN_YAML
    text = path.read_text(encoding="utf-8")
    assert "created: '2026-10-02'" in text
    path.write_text(text.replace("created: '2026-10-02'", BAD_DATE), encoding="utf-8")


def test_a_date_that_does_not_exist_is_an_error(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_bad_date(data_root)
    assert _status(data_root) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert lines[1].startswith("2026-09-14  Globex")
    assert lines[2].startswith("2026-11-03  Example Labs")
    assert lines[3].startswith(f"error status {INITECH}: ")
    assert "a date that does not exist" in lines[3]
    assert lines[4:] == ["2 runs."]


def test_a_date_that_does_not_exist_json(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_bad_date(data_root)
    assert _status(data_root, "--json") == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    report = json.loads(captured.out)
    assert report["ok"] is False
    assert (report["errors"], report["warnings"]) == (1, 0)
    assert report["runs"] == [EXPECTED_ROWS[0], EXPECTED_ROWS[2]]
    [finding] = report["findings"]
    assert (finding["severity"], finding["location"]) == ("error", INITECH)
    assert "a date that does not exist" in finding["message"]


def test_a_bad_yaml_finding_shows_no_line_of_the_file(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = data_root / APPLICATIONS / GLOBEX / RUN_YAML
    path.write_text("company: [unclosed hidden-text\n", encoding="utf-8")
    assert _status(data_root) == 1
    out = capsys.readouterr().out
    assert "hidden-text" not in out
    assert len(out.splitlines()) == 5  # header, 2 rows, 1 finding, the count
    assert "is not valid YAML (line 2, column 1). Fix the YAML." in out


# A line end in a folder name cannot start a false line (S1 of the review).

FAKE_ROW = "2026-12-31  Fake Co  Fake Role  ux  submitted"


def test_a_line_end_in_a_folder_name_of_an_error_stays_on_one_line(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (data_root / APPLICATIONS / f"2026-01-01-x\n{FAKE_ROW}\tmore").mkdir()
    assert _status(data_root) == 1
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 6  # header, 3 rows, 1 finding, the count
    assert not any(line.startswith(FAKE_ROW) for line in lines)
    assert "\t" not in out
    assert lines[4].startswith(f"error status 2026-01-01-x?{FAKE_ROW}?more: ")


def test_a_line_end_in_a_folder_name_of_a_warning_stays_on_one_line(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = data_root / APPLICATIONS / f"2026-01-01-y\n{FAKE_ROW}\tmore"
    run.mkdir()
    (run / RUN_YAML).write_text("status: applied\n", encoding="utf-8")
    assert _status(data_root) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 7  # header, 4 rows, 1 finding, the count
    assert not any(line.startswith(FAKE_ROW) for line in lines)
    assert "\t" not in out
    assert lines[5].startswith(f"warning status 2026-01-01-y?{FAKE_ROW}?more: ")


def test_status_json_keeps_the_true_folder_name(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    name = f"2026-01-01-x\n{FAKE_ROW}\tmore"
    (data_root / APPLICATIONS / name).mkdir()
    assert _status(data_root, "--json") == 1
    report = json.loads(capsys.readouterr().out)
    assert [f["location"] for f in report["findings"]] == [name]


def test_an_applications_folder_that_cannot_be_read(
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "iterdir", fail)
    folder = data_root / APPLICATIONS
    message = (
        f"{folder} cannot be read: Permission denied. "
        "Check the permissions of the folder."
    )
    with pytest.raises(InputError) as exc:
        list_runs(data_root)
    assert str(exc.value) == message
    assert _status(data_root) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"facts status: {message}\n"


def test_an_unknown_status_is_a_warning(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = data_root / APPLICATIONS / EXAMPLE_LABS / RUN_YAML
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("status: new", "status: applied"), encoding="utf-8")
    assert _status(data_root) == 0
    out = capsys.readouterr().out
    assert f"warning status {EXAMPLE_LABS}: Status 'applied' is not one of" in out
    assert "applied" in out.splitlines()[3]
    assert out.endswith("3 runs.\n")


def test_a_missing_status_is_a_warning(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = data_root / APPLICATIONS / EXAMPLE_LABS / RUN_YAML
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("status: new\n", ""), encoding="utf-8")
    assert _status(data_root, "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["warnings"] == 1 and report["runs"][2]["status"] is None


@pytest.mark.parametrize("status", STATUSES)
def test_each_known_status_gives_no_warning(data_root: Path, status: str) -> None:
    path = data_root / APPLICATIONS / EXAMPLE_LABS / RUN_YAML
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("status: new", f"status: {status}"), encoding="utf-8")
    _, findings = list_runs(data_root)
    assert findings == []


def test_missing_keys_show_as_a_hyphen(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = data_root / APPLICATIONS / INITECH / RUN_YAML
    path.write_text("status: new\n", encoding="utf-8")
    assert _status(data_root) == 0
    line = capsys.readouterr().out.splitlines()[2]
    assert line.split() == ["-", "-", "-", "-", "new"]


def test_a_yaml_date_is_shown_as_a_date(data_root: Path) -> None:
    path = data_root / APPLICATIONS / INITECH / RUN_YAML
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("'2026-10-02'", "2026-10-02"), encoding="utf-8")
    rows, _ = list_runs(data_root)
    assert rows[1].date == "2026-10-02"


def test_a_terminal_escape_code_is_shown_as_a_question_mark(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = data_root / APPLICATIONS / GLOBEX / RUN_YAML
    text = path.read_text(encoding="utf-8")
    hidden = '"Glo\\e[31mbex\\u202e\\nX"'  # YAML escapes: ESC, a bidi override, LF
    path.write_text(text.replace("Globex", hidden), encoding="utf-8")
    assert _status(data_root) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\u202e" not in out
    assert "Glo?[31mbex??X" in out.splitlines()[1]
    assert len(out.splitlines()) == 5


def test_a_tab_in_a_table_cell_is_shown_as_a_question_mark(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = data_root / APPLICATIONS / GLOBEX / RUN_YAML
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("Globex", '"Glo\\tbex"'), encoding="utf-8")
    assert list_runs(data_root)[0][0].company == "Glo\tbex"
    assert _status(data_root) == 0
    out = capsys.readouterr().out
    assert "\t" not in out
    assert out.splitlines()[1].startswith("2026-09-14  Glo?bex ")


def test_a_terminal_escape_code_in_a_finding_is_shown_as_a_question_mark(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (data_root / APPLICATIONS / "2026-01-01-x\x1b[2J").mkdir()
    assert _status(data_root) == 1
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "error status 2026-01-01-x?[2J: " in out


def test_status_no_data_root_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _status(tmp_path) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "facts status: No master.yaml found" in captured.err


def test_status_lists_a_run_from_new_run(
    data_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_today", lambda: dt.date(2026, 11, 20))
    job = tmp_path / "post.txt"
    job.write_text("Acme needs a designer.", encoding="utf-8")
    args = ["--company", "Acme", "--role", "Designer", "--job", str(job)]
    assert main(["new-run", *args, "--repo", str(data_root)]) == 0
    capsys.readouterr()
    assert _status(data_root, "--json") == 0
    rows = json.loads(capsys.readouterr().out)["runs"]
    assert rows[-1] == {
        "run": "2026-11-20-acme-designer",
        "date": "2026-11-20",
        "company": "Acme",
        "role": "Designer",
        "type": None,
        "status": "new",
    }
    assert rows[:-1] == EXPECTED_ROWS


def test_one_run_is_counted_as_one_run(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in (GLOBEX, INITECH):
        shutil.rmtree(data_root / APPLICATIONS / name)
    assert _status(data_root) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "1 run."


# One run folder that status cannot read gives one error finding. The other runs
# still list, and no traceback comes out (N1 of the second review).

NEW = "2026-10-20-acme-tester"  # sorts between INITECH and EXAMPLE_LABS
GOOD = "created: '2026-10-20'\ncompany: Acme\nrole: Tester\ntype: ux\nstatus: new\n"
GOOD_ROW: dict[str, str | None] = {
    "date": "2026-10-20",
    "company": "Acme",
    "role": "Tester",
    "type": "ux",
    "status": "new",
}
NOT_ROOT = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="a permission test needs a POSIX user that is not root",
)


def _add_run(root: Path, content: str) -> Path:
    run = root / APPLICATIONS / NEW
    run.mkdir()
    (run / RUN_YAML).write_text(content, encoding="utf-8")
    return run


def _expect_one_error(
    root: Path, capsys: pytest.CaptureFixture[str], message: str
) -> None:
    """Assert: the three good runs list, and NEW gives one error, text and JSON."""
    assert _status(root) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert lines[:4] == EXPECTED_TEXT.splitlines()[:4]
    assert len(lines) == 6
    assert lines[4].startswith(f"error status {NEW}: ")
    assert message in lines[4]
    assert lines[5] == "3 runs."
    assert _status(root, "--json") == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    report = json.loads(captured.out)
    assert report["ok"] is False
    assert (report["errors"], report["warnings"]) == (1, 0)
    assert report["runs"] == EXPECTED_ROWS
    [finding] = report["findings"]
    assert (finding["rule"], finding["severity"]) == ("status", "error")
    assert finding["location"] == NEW
    assert message in finding["message"] and "\n" not in finding["message"]


def _expect_row(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    row: dict[str, str | None],
    warnings: int = 0,
) -> None:
    """Assert: NEW gives the row `row` among the three good runs, text and JSON."""
    assert _status(root) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 6 + warnings and lines[-1] == "4 runs."
    keys = ("date", "company", "role", "type", "status")
    assert re.split(r" {2,}", lines[3].rstrip()) == [row[k] or "-" for k in keys]
    assert _status(root, "--json") == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    report = json.loads(captured.out)
    assert (report["errors"], report["warnings"]) == (0, warnings)
    assert report["runs"] == [*EXPECTED_ROWS[:2], {"run": NEW, **row}, EXPECTED_ROWS[2]]


@contextlib.contextmanager
def _mode(path: Path, mode: int) -> Generator[None]:
    """Set the mode of `path`, and set it back so that pytest can remove it."""
    old = path.stat().st_mode
    path.chmod(mode)
    try:
        yield
    finally:
        path.chmod(old)


# (a) A run folder that the user cannot open.

DENIED = "cannot be read: Permission denied. Check the permissions"


@NOT_ROOT
def test_a_run_folder_with_no_permission_is_an_error(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _add_run(data_root, GOOD)
    with _mode(run, 0):
        # stat() raises PermissionError on each Python version. On Python 3.14,
        # Path.is_file() returns False here instead.
        with pytest.raises(PermissionError):
            (run / RUN_YAML).stat()
        _expect_one_error(data_root, capsys, DENIED)


@NOT_ROOT
def test_a_run_yaml_with_no_permission_is_an_error(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _add_run(data_root, GOOD)
    with _mode(run / RUN_YAML, 0):
        _expect_one_error(data_root, capsys, DENIED)


@NOT_ROOT
def test_a_link_into_a_folder_with_no_permission_is_an_error(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Path.is_dir() raises PermissionError here. Before this round, that stopped
    # the whole listing with exit code 2.
    closed = data_root / "closed"
    (closed / "inner").mkdir(parents=True)
    link = data_root / APPLICATIONS / NEW
    link.symlink_to(closed / "inner")
    with _mode(closed, 0):
        with pytest.raises(PermissionError):
            link.stat()
        _expect_one_error(data_root, capsys, DENIED)


# (b) A value that the PyYAML constructors cannot read raises other classes than
# YAMLError and ValueError.

DEEP_LIST = "x: " + "[" * 1000 + "]" * 1000 + "\n"
DEEP_MAP = "x: " + "{a: " * 1000 + "1" + "}" * 1000 + "\n"
CANNOT_READ = "has a value that YAML cannot read. Fix the value, or put it in quotes."


@pytest.mark.parametrize(
    ("text", "error", "message"),
    [
        ("company: !!bool maybe\n", KeyError, CANNOT_READ),
        ('company: !!int ""\n', IndexError, CANNOT_READ),
        ('role: !!float ""\n', IndexError, CANNOT_READ),
        ("created: !!timestamp abc\n", AttributeError, CANNOT_READ),
        (DEEP_LIST, RecursionError, "nested too deep. Fix the YAML."),
        (DEEP_MAP, RecursionError, "nested too deep. Fix the YAML."),
        # A key that status does not show.
        (GOOD + "notes: !!bool maybe\n", KeyError, CANNOT_READ),
    ],
    ids=["bool", "int", "float", "timestamp", "deep-list", "deep-map", "other-key"],
)
def test_a_value_that_yaml_cannot_read_is_an_error(
    data_root: Path,
    capsys: pytest.CaptureFixture[str],
    text: str,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error):
        yaml.safe_load(text)
    _add_run(data_root, text)
    _expect_one_error(data_root, capsys, message)


# (c) An integer that Python cannot make into text (more than 4300 digits).

BIG_INTEGERS = {
    "hex": "0x" + "f" * 4000,
    "binary": "0b" + "1" * 15000,
    "octal": "0" + "7" * 5000,
    "sexagesimal": "1" + ":30" * 3000,
}


def _not_text(root: Path, key: str) -> str:
    path = root / APPLICATIONS / NEW / RUN_YAML
    return f"The value of {key} in {path} is not text. Write it as text, in quotes."


@pytest.mark.parametrize("key", ["created", "company", "role", "type", "status"])
@pytest.mark.parametrize("form", list(BIG_INTEGERS))
def test_an_integer_too_large_for_text_is_an_error(
    data_root: Path, capsys: pytest.CaptureFixture[str], key: str, form: str
) -> None:
    text = f"{key}: {BIG_INTEGERS[form]}\n"
    value = yaml.safe_load(text)[key]
    assert isinstance(value, int)
    with pytest.raises(ValueError):
        str(value)
    _add_run(data_root, text)
    _expect_one_error(data_root, capsys, _not_text(data_root, key))


def test_a_long_decimal_integer_is_an_error(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # PyYAML itself raises a ValueError for this one.
    _add_run(data_root, "company: " + "9" * 5000 + "\n")
    _expect_one_error(data_root, capsys, "has a value that is not valid")


# A value that is not text, a number, a date or None.


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("company: [Acme, Labs]\n", "company"),
        ("company: {name: Acme}\n", "company"),
        ("status: [new]\n", "status"),
        ("type: !!binary QWNtZQ==\n", "type"),
        ("role: !!set {a, b}\n", "role"),
        ("role: !!omap [a: 1]\n", "role"),
    ],
    ids=["list", "mapping", "status-list", "binary", "set", "omap"],
)
def test_a_value_that_is_not_text_is_an_error(
    data_root: Path, capsys: pytest.CaptureFixture[str], text: str, key: str
) -> None:
    _add_run(data_root, text)
    _expect_one_error(data_root, capsys, _not_text(data_root, key))


def _alias_yaml(key: str, depth: int = 8, width: int = 11) -> str:
    """Return YAML in which `key` holds width**depth items, built from aliases.

    The file is short. Before this round, the text of this value took about 10
    seconds to make.
    """
    items = ", ".join(["x"] * width)
    lines = [f"a0: &a0 [{items}]"]
    for i in range(1, depth):
        refs = ", ".join([f"*a{i - 1}"] * width)
        lines.append(f"a{i}: &a{i} [{refs}]")
    lines.append(f"{key}: *a{depth - 1}")
    return "\n".join(lines) + "\n"


def _seconds(root: Path, code: int, *extra: str) -> float:
    start = time.monotonic()
    assert _status(root, *extra) == code
    return time.monotonic() - start


# Issue 13 (owner decision): load_meta() refuses a run.yaml that uses a YAML
# alias, so a value built from aliases is an error before the YAML load.
ALIAS = "uses a YAML alias (*name). Write each value in full"


def test_a_value_built_from_aliases_is_an_error_and_fast(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _alias_yaml("company")
    assert len(text) < 1000
    _add_run(data_root, text)
    assert _seconds(data_root, 1) < 2
    assert _seconds(data_root, 1, "--json") < 2
    capsys.readouterr()
    _expect_one_error(data_root, capsys, ALIAS)


def test_a_value_built_from_aliases_in_another_key_is_an_error_and_fast(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add_run(data_root, _alias_yaml("notes") + GOOD)
    assert _seconds(data_root, 1) < 2
    assert _seconds(data_root, 1, "--json") < 2
    capsys.readouterr()
    _expect_one_error(data_root, capsys, ALIAS)


def test_one_alias_is_an_error(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add_run(data_root, GOOD.replace("role: Tester", "role: &r Tester\nnotes: *r"))
    _expect_one_error(data_root, capsys, ALIAS)


def test_an_anchor_with_no_alias_gives_a_row(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add_run(data_root, GOOD.replace("role: Tester", "role: &r Tester"))
    _expect_row(data_root, capsys, GOOD_ROW)


# Issue 13 (owner decision): load_meta() refuses a run.yaml larger than 64 KiB.

TOO_LARGE = "is larger than 64 KiB."


def test_a_run_yaml_over_the_size_limit_is_an_error(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = GOOD + "notes: " + "x" * 70_000 + "\n"
    _add_run(data_root, text)
    _expect_one_error(data_root, capsys, TOO_LARGE)


def test_a_run_yaml_at_the_size_limit_gives_a_row(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = GOOD + "notes: "
    text = head + "x" * (64 * 1024 - len(head) - 1) + "\n"
    assert len(text.encode("utf-8")) == 64 * 1024
    run = _add_run(data_root, text)
    _expect_row(data_root, capsys, GOOD_ROW)
    (run / RUN_YAML).write_text(text + "\n", encoding="utf-8")
    _expect_one_error(data_root, capsys, TOO_LARGE)


# Values and keys that give a correct row.


@pytest.mark.parametrize(
    ("text", "row"),
    [
        (
            "created: '2026-10-20'\ncompany: .nan\nrole: .inf\ntype: -.inf\n"
            "status: new\n",
            {**GOOD_ROW, "company": "nan", "role": "inf", "type": "-inf"},
        ),
        (GOOD.replace("type: ux", "type: yes"), {**GOOD_ROW, "type": "True"}),
        (GOOD.replace("type: ux", "type: 3.5"), {**GOOD_ROW, "type": "3.5"}),
        (
            GOOD.replace("'2026-10-20'", "2026-10-20T10:30:00+05:30"),
            {**GOOD_ROW, "date": "2026-10-20T10:30:00+05:30"},
        ),
        ("1: one\n~: none\ntrue: yes\n2026-01-01: day\n3.5: x\n" + GOOD, GOOD_ROW),
        # Below the size limit of 64 KiB (issue 13).
        (GOOD.replace("Acme", "A" * 60_000), {**GOOD_ROW, "company": "A" * 60_000}),
    ],
    ids=["nan-inf", "bool", "float", "datetime-with-zone", "keys-not-text", "long"],
)
def test_a_value_that_gives_a_row(
    data_root: Path,
    capsys: pytest.CaptureFixture[str],
    text: str,
    row: dict[str, str | None],
) -> None:
    _add_run(data_root, text)
    _expect_row(data_root, capsys, row)


@pytest.mark.parametrize(
    # The YAML text "\t" (in double quotes) is one tab character.
    "value",
    ["''", "'   '", '"\\t"'],
    ids=["empty", "blank", "tab"],
)
def test_an_empty_or_blank_value_shows_a_mark(
    data_root: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    # Issue 13: an empty or blank cell shows "" in the table. JSON keeps the value.
    _add_run(data_root, GOOD.replace("company: Acme", f"company: {value}"))
    assert _status(data_root) == 0
    lines = capsys.readouterr().out.splitlines()
    assert re.split(r" {2,}", lines[3].rstrip()) == [
        "2026-10-20",
        '""',
        "Tester",
        "ux",
        "new",
    ]
    assert _status(data_root, "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["runs"][2]["company"] == yaml.safe_load(f"v: {value}")["v"]


def test_a_top_level_list_reads_as_an_empty_mapping(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The "or {}" of the source readers: an empty file, and also a top-level [],
    # reads as {}. Status then shows "-" values and a warning for the status.
    _add_run(data_root, "[]\n")
    empty: dict[str, str | None] = dict.fromkeys(GOOD_ROW)
    _expect_row(data_root, capsys, empty, warnings=1)


# A character that the encoding of stdout does not have.


def _ascii_status(
    root: Path, monkeypatch: pytest.MonkeyPatch, *extra: str
) -> tuple[int, str]:
    """Run status with an ASCII stdout. Return the exit code and the output."""
    buffer = io.BytesIO()
    out = io.TextIOWrapper(buffer, encoding="ascii")
    with monkeypatch.context() as m:
        m.setattr(sys, "stdout", out)
        code = _status(root, *extra)
        out.flush()
    return code, buffer.getvalue().decode("ascii")


def test_text_that_stdout_cannot_encode_shows_as_a_question_mark(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_run(data_root, GOOD.replace("Acme", "Caf\u00e9 \u4e2d"))
    (data_root / APPLICATIONS / "2026-10-21-caf\u00e9").mkdir()  # no run.yaml
    code, out = _ascii_status(data_root, monkeypatch)
    assert code == 1
    lines = out.splitlines()
    cells = ["2026-10-20", "Caf? ?", "Tester", "ux", "new"]
    assert re.split(r" {2,}", lines[3].rstrip()) == cells
    assert lines[5].startswith("error status 2026-10-21-caf?: ")
    assert lines[6] == "4 runs."
    code, out = _ascii_status(data_root, monkeypatch, "--json")
    assert code == 1
    report = json.loads(out)
    assert report["runs"][2]["company"] == "Caf\u00e9 \u4e2d"
    assert report["findings"][0]["location"] == "2026-10-21-caf\u00e9"
