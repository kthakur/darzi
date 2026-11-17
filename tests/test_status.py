"""`facts status` and `runs/status.py` (work item W-4)."""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

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


def _tree(folder: Path) -> dict[str, bytes | None]:
    """Return each file (with its content) and each folder (None) under `folder`."""
    return {
        str(p.relative_to(folder)): p.read_bytes() if p.is_file() else None
        for p in folder.rglob("*")
    }


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


def test_status_writes_nothing(data_root: Path) -> None:
    before = _tree(data_root)
    assert _status(data_root) == 0
    assert _status(data_root, "--json") == 0
    assert _tree(data_root) == before
    (data_root / APPLICATIONS / INITECH / RUN_YAML).unlink()
    before = _tree(data_root)
    assert _status(data_root) == 1
    assert _tree(data_root) == before


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

FAKE_ROW = "2026-12-31  Evil Corp  Fake Role  ux  submitted"


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
    with pytest.raises(InputError) as exc:
        list_runs(data_root)
    assert str(exc.value) == f"{folder} cannot be read: Permission denied."
    assert _status(data_root) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    message = f"{folder} cannot be read: Permission denied."
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
