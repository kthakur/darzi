"""Run folders (`runs/runs.py`) and `facts new-run` (work item W-4)."""

from __future__ import annotations

import copy
import datetime as dt
import io
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from darzi import cli
from darzi.cli import main
from darzi.facts.repo import InputError
from darzi.runs import runs
from darzi.runs.runs import (
    APPLICATIONS,
    APPROVAL,
    JOB,
    RUN_YAML,
    STATUSES,
    content_hash,
    load_meta,
    new_run,
    run_name,
    slug,
)

DAY = dt.date(2026, 11, 11)
POST = "Acme needs a product designer.\nDesign the sign-up forms.\n"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """An empty temporary data root: only a master.yaml, so find_repo finds it."""
    folder = tmp_path / "root"
    folder.mkdir()
    (folder / "master.yaml").write_text("{}\n", encoding="utf-8")
    return folder


def _tree(folder: Path) -> dict[str, bytes | None]:
    """Return each file (with its content) and each folder (None) under `folder`."""
    return {
        str(p.relative_to(folder)): p.read_bytes() if p.is_file() else None
        for p in folder.rglob("*")
    }


# content_hash: no change from the source (docs/SPEC.md section 7)

TAILORED: dict[str, Any] = {
    "personalInfo": {"name": "Test Person", "email": "test.person@example.com"},
    "summary": "Designs caf\u00e9 sign-up forms.",
    "workExperience": [{"id": 1, "company": "Acme", "description": ["B", "A"]}],
}
LETTER = "Dear Acme team,\n\nI design forms.\n"


def test_hash_covers_the_letter() -> None:
    assert content_hash(TAILORED, "a") != content_hash(TAILORED, "b")


def test_hash_covers_the_resume() -> None:
    other = copy.deepcopy(TAILORED)
    other["summary"] = "Designs forms."
    assert content_hash(TAILORED, LETTER) != content_hash(other, LETTER)


def test_hash_is_equal_for_equal_content() -> None:
    assert content_hash(TAILORED, None) == content_hash(copy.deepcopy(TAILORED), None)


def test_hash_of_no_letter_equals_an_empty_letter() -> None:
    assert content_hash(TAILORED, None) == content_hash(TAILORED, "")


def test_hash_ignores_key_order() -> None:
    reordered = dict(reversed(list(TAILORED.items())))
    assert content_hash(reordered, LETTER) == content_hash(TAILORED, LETTER)


def test_hash_is_pinned() -> None:
    # Computed once from the source function on the same synthetic input. A
    # change to content_hash() changes these values (hard rule 2).
    assert content_hash(TAILORED, LETTER) == (
        "6570dae8a74e609e4388a7dc1843b9cd75e0fe7fe5c7ae00cbae5d7b94656f83"
    )
    assert content_hash(TAILORED, None) == (
        "2d6eed091bdb88e906bdca710588bc001e25b788a107f1c4e759fba7ceee85a2"
    )


def test_file_names_are_the_source_names() -> None:
    # Work item W-5 reads approval.json with this name. The source has the same
    # names.
    assert (APPLICATIONS, JOB, RUN_YAML, APPROVAL) == (
        "applications",
        "job.md",
        "run.yaml",
        "approval.json",
    )
    assert runs.RUN_SCHEMA_VERSION == 1


# Folder names: the source slug, with no change


def test_run_name_with_spaces_and_punctuation() -> None:
    assert run_name("Acme, Inc.", "Senior UX/UI Designer (Remote)", DAY) == (
        "2026-11-11-acme-inc-senior-ux-ui-designer-remote"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Acme", "acme"),
        ("  Example   Labs  ", "example-labs"),
        ("UX--Lead", "ux-lead"),
        ("Designer 2", "designer-2"),
        ("Caf\u00e9 \u00dcber", "caf-ber"),  # an accented letter becomes a hyphen
        ("na\u00efve", "na-ve"),
        ("../../x", "x"),
        ("a/b\\c", "a-b-c"),
        ("UX_Lead", "ux-lead"),  # an underscore becomes a hyphen, as in the source
        ("__a__b__", "a-b"),
    ],
)
def test_slug(text: str, expected: str) -> None:
    assert slug(text) == expected


@pytest.mark.parametrize(
    "text", ["!!!", "", "   ", "../..", "\u00e9\u00e8", "\u65e5\u672c"]
)
def test_slug_with_no_letter_or_digit_is_refused(text: str) -> None:
    with pytest.raises(InputError, match="gives an empty name"):
        slug(text)


def test_run_name_refuses_an_empty_role() -> None:
    with pytest.raises(InputError, match="gives an empty name"):
        run_name("Acme", "???", DAY)


def test_a_path_in_the_company_stays_inside_applications(root: Path) -> None:
    run = new_run(root, "../../evil", "../Designer", POST, DAY)
    assert run == root / APPLICATIONS / "2026-11-11-evil-designer"
    assert run.resolve().parent == (root / APPLICATIONS).resolve()
    assert sorted(p.name for p in root.iterdir()) == [APPLICATIONS, "master.yaml"]


# new_run


def test_new_run_writes_job_and_run_yaml(root: Path) -> None:
    run = new_run(root, "Acme", "Product Designer", "  The job post.\n\n\n", DAY)
    assert run == root / APPLICATIONS / "2026-11-11-acme-product-designer"
    assert (run / JOB).read_text(encoding="utf-8") == "  The job post.\n"
    meta = yaml.safe_load((run / RUN_YAML).read_text(encoding="utf-8"))
    assert meta == {
        "schema_version": 1,
        "company": "Acme",
        "role": "Product Designer",
        "created": "2026-11-11",
        "type": None,
        "selection_hash": None,
        "status": "new",
    }
    assert list(meta) == [
        "schema_version",
        "company",
        "role",
        "created",
        "type",
        "selection_hash",
        "status",
    ]
    assert "resume_matcher" not in meta
    assert sorted(p.name for p in run.iterdir()) == [JOB, RUN_YAML]


def test_new_run_keeps_non_ascii_text(root: Path) -> None:
    run = new_run(root, "Caf\u00e9 Acme", "Designer", "Caf\u00e9 \u2013 forms.", DAY)
    assert run.name == "2026-11-11-caf-acme-designer"
    assert (run / JOB).read_bytes() == "Caf\u00e9 \u2013 forms.\n".encode()
    assert load_meta(run)["company"] == "Caf\u00e9 Acme"


def test_new_run_writes_job_md_first(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written: list[str] = []
    original = Path.write_text

    def record(self: Path, data: str, *args: Any, **kwargs: Any) -> int:
        written.append(self.name)
        return original(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", record)
    new_run(root, "Acme", "Designer", POST, DAY)
    assert written == [JOB, RUN_YAML]


def test_job_md_survives_a_failure_after_it(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("the run.yaml step failed")

    monkeypatch.setattr(runs.yaml, "safe_dump", fail)
    with pytest.raises(RuntimeError):
        new_run(root, "Acme", "Designer", POST, DAY)
    run = root / APPLICATIONS / "2026-11-11-acme-designer"
    assert (run / JOB).read_text(encoding="utf-8") == POST
    assert not (run / RUN_YAML).exists()


def test_a_second_run_with_the_same_name_is_refused(root: Path) -> None:
    run = new_run(root, "Acme", "Designer", POST, DAY)
    before = _tree(root)
    with pytest.raises(InputError, match="already exists"):
        new_run(root, "ACME", "designer!", "Another post.", DAY)
    assert _tree(root) == before
    assert (run / JOB).read_text(encoding="utf-8") == POST


def test_an_existing_empty_folder_is_refused(root: Path) -> None:
    (root / APPLICATIONS / "2026-11-11-acme-designer").mkdir(parents=True)
    with pytest.raises(InputError, match="already exists"):
        new_run(root, "Acme", "Designer", POST, DAY)
    assert not (root / APPLICATIONS / "2026-11-11-acme-designer" / JOB).exists()


def test_a_run_on_another_day_is_a_new_folder(root: Path) -> None:
    first = new_run(root, "Acme", "Designer", POST, DAY)
    second = new_run(root, "Acme", "Designer", POST, DAY + dt.timedelta(days=1))
    assert first != second and second.name.startswith("2026-11-12-")


@pytest.mark.parametrize("text", ["", "   ", "\n\t\n"])
def test_an_empty_job_post_is_refused(root: Path, text: str) -> None:
    with pytest.raises(InputError, match="job post is empty"):
        new_run(root, "Acme", "Designer", text, DAY)
    assert not (root / APPLICATIONS).exists()


def test_an_empty_name_makes_no_folder(root: Path) -> None:
    with pytest.raises(InputError, match="gives an empty name"):
        new_run(root, "!!!", "Designer", POST, DAY)
    assert not (root / APPLICATIONS).exists()


# load_meta: the one reader of run.yaml


def test_load_meta_reads_run_yaml(root: Path) -> None:
    run = new_run(root, "Acme", "Designer", POST, DAY)
    meta = load_meta(run)
    assert meta["status"] == "new" and meta["status"] in STATUSES


def test_load_meta_of_an_empty_file_is_empty(tmp_path: Path) -> None:
    (tmp_path / RUN_YAML).write_text("", encoding="utf-8")
    assert load_meta(tmp_path) == {}


def test_load_meta_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="does not exist"):
        load_meta(tmp_path)


def test_load_meta_refuses_bad_yaml(tmp_path: Path) -> None:
    (tmp_path / RUN_YAML).write_text("company: [unclosed\n", encoding="utf-8")
    with pytest.raises(InputError, match="not valid YAML"):
        load_meta(tmp_path)


def test_load_meta_gives_a_short_message_for_bad_yaml(tmp_path: Path) -> None:
    # The message is one line: the place of the error, not a line of the file.
    (tmp_path / RUN_YAML).write_text(
        "status: new\ncompany: [unclosed hidden-text\n", encoding="utf-8"
    )
    with pytest.raises(InputError) as exc:
        load_meta(tmp_path)
    message = str(exc.value)
    assert "\n" not in message and "hidden-text" not in message
    assert message == (
        f"{tmp_path / RUN_YAML} is not valid YAML (line 3, column 1). Fix the YAML."
    )


@pytest.mark.parametrize(
    "created", ["2026-09-31", "2026-02-30", "2026-13-01", "0000-01-01"]
)
def test_load_meta_refuses_a_date_that_does_not_exist(
    tmp_path: Path, created: str
) -> None:
    # PyYAML raises a plain ValueError here, not a YAML error.
    with pytest.raises(ValueError):
        yaml.safe_load(f"created: {created}\n")
    (tmp_path / RUN_YAML).write_text(
        f"company: Acme\ncreated: {created}\n", encoding="utf-8"
    )
    with pytest.raises(InputError, match="a date that does not exist") as exc:
        load_meta(tmp_path)
    assert str(exc.value).startswith(f"{tmp_path / RUN_YAML} has a value")
    assert "\n" not in str(exc.value)


def test_load_meta_reads_a_quoted_date_that_does_not_exist(tmp_path: Path) -> None:
    (tmp_path / RUN_YAML).write_text("created: '2026-09-31'\n", encoding="utf-8")
    assert load_meta(tmp_path) == {"created": "2026-09-31"}


def test_load_meta_refuses_a_file_that_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / RUN_YAML).write_text("status: new\n", encoding="utf-8")

    def fail(*args: Any, **kwargs: Any) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(InputError) as exc:
        load_meta(tmp_path)
    assert str(exc.value) == (
        f"{tmp_path / RUN_YAML} cannot be read: Permission denied. "
        "Check the permissions of the file and its folder."
    )


def test_load_meta_refuses_a_file_it_cannot_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The file test (stat()) too raises an OSError, for example for a run folder
    # that the user cannot open.
    def fail(*args: Any, **kwargs: Any) -> bool:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "stat", fail)
    with pytest.raises(InputError, match="cannot be read: Permission denied"):
        load_meta(tmp_path)


@pytest.mark.parametrize("text", ["- a\n- b\n", "just text\n", "5\n"])
def test_load_meta_refuses_a_file_that_is_not_a_mapping(
    tmp_path: Path, text: str
) -> None:
    (tmp_path / RUN_YAML).write_text(text, encoding="utf-8")
    with pytest.raises(InputError, match="not a mapping"):
        load_meta(tmp_path)


def test_load_meta_refuses_text_that_is_not_utf8(tmp_path: Path) -> None:
    (tmp_path / RUN_YAML).write_bytes(b"company: Caf\xe9\n")
    with pytest.raises(InputError, match="not UTF-8"):
        load_meta(tmp_path)


def test_load_meta_refuses_a_folder_named_run_yaml(tmp_path: Path) -> None:
    (tmp_path / RUN_YAML).mkdir()
    with pytest.raises(InputError, match="does not exist"):
        load_meta(tmp_path)


# facts new-run


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> dt.date:
    """Fix the date of a new run, so that a test does not fail at midnight."""
    monkeypatch.setattr(cli, "_today", lambda: DAY)
    return DAY


def _stdin(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(data)))


def _new_run(root: Path, *extra: str, company: str = "Acme") -> int:
    args = ["new-run", "--company", company, "--role", "Designer"]
    return main([*args, *extra, "--repo", str(root)])


def test_cli_new_run_with_a_job_file(
    root: Path, tmp_path: Path, today: dt.date, capsys: pytest.CaptureFixture[str]
) -> None:
    job = tmp_path / "post.txt"
    job.write_text("Build prototypes.", encoding="utf-8")
    assert _new_run(root, "--job", str(job)) == 0
    run = Path(capsys.readouterr().out.strip())
    assert run == root / APPLICATIONS / "2026-11-11-acme-designer"
    assert (run / JOB).read_text(encoding="utf-8") == "Build prototypes.\n"
    assert load_meta(run)["created"] == today.isoformat()


def test_cli_new_run_uses_today(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = tmp_path / "post.txt"
    job.write_text("Build prototypes.", encoding="utf-8")
    before = dt.date.today()
    assert _new_run(root, "--job", str(job)) == 0
    after = dt.date.today()
    run = Path(capsys.readouterr().out.strip())
    # Accept either date, so that the test does not fail at midnight.
    assert run.name in {
        run_name("Acme", "Designer", before),
        run_name("Acme", "Designer", after),
    }


def test_cli_new_run_from_stdin(
    root: Path,
    today: dt.date,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stdin(monkeypatch, "Caf\u00e9 job.\r\nLine two.\r\n".encode())
    assert _new_run(root, "--job", "-") == 0
    run = Path(capsys.readouterr().out.strip())
    # The CR LF inside stays. new_run removes the white space at the end.
    assert (run / JOB).read_bytes() == "Caf\u00e9 job.\r\nLine two.\n".encode()


def test_cli_new_run_keeps_the_line_ends_from_stdin(
    root: Path,
    today: dt.date,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # As in the source on macOS and Linux: CR LF and a lone CR stay as they are.
    # Only the white space at the end changes (new_run writes one last "\n").
    _stdin(monkeypatch, b"One.\r\nTwo.\rThree.\nFour.\r\n")
    assert _new_run(root, "--job", "-") == 0
    run = Path(capsys.readouterr().out.strip())
    assert (run / JOB).read_bytes() == b"One.\r\nTwo.\rThree.\nFour.\n"


def test_cli_stdin_is_utf8_independent_of_the_locale(
    root: Path,
    today: dt.date,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The text wrapper says Latin-1. The bytes are UTF-8. The command reads the
    # bytes, so job.md holds the correct text.
    data = "Caf\u00e9 \u2013 forms.\n".encode()
    wrapper = io.TextIOWrapper(io.BytesIO(data), encoding="latin-1")
    monkeypatch.setattr("sys.stdin", wrapper)
    assert _new_run(root, "--job", "-") == 0
    run = Path(capsys.readouterr().out.strip())
    assert (run / JOB).read_bytes() == data


BOM_POST = b"\xef\xbb\xbfAcme needs a designer.\nLine two.\n"


def test_cli_keeps_a_byte_order_mark_from_stdin(
    root: Path,
    today: dt.date,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # As the source: the UTF-8 byte order mark stays in job.md.
    _stdin(monkeypatch, BOM_POST)
    assert _new_run(root, "--job", "-") == 0
    run = Path(capsys.readouterr().out.strip())
    assert (run / JOB).read_bytes() == BOM_POST


def test_cli_keeps_a_byte_order_mark_in_a_job_file(
    root: Path, tmp_path: Path, today: dt.date, capsys: pytest.CaptureFixture[str]
) -> None:
    # As the source: the UTF-8 byte order mark stays in job.md.
    job = tmp_path / "post.txt"
    job.write_bytes(BOM_POST)
    assert _new_run(root, "--job", str(job)) == 0
    run = Path(capsys.readouterr().out.strip())
    assert (run / JOB).read_bytes() == BOM_POST


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            b"Acme needs a designer.\r\nLine two.\r\n",
            b"Acme needs a designer.\nLine two.\n",
        ),
        (
            b"\xef\xbb\xbfOne.\r\nTwo.\rThree.\nFour.\r\n",
            b"\xef\xbb\xbfOne.\nTwo.\nThree.\nFour.\n",
        ),
    ],
)
def test_cli_line_ends_of_a_job_file_become_lf(
    root: Path,
    tmp_path: Path,
    today: dt.date,
    capsys: pytest.CaptureFixture[str],
    data: bytes,
    expected: bytes,
) -> None:
    # As the source: the source reads a job file in text mode, so CR LF and a
    # lone CR become LF. (From stdin, the line ends stay: see above.)
    job = tmp_path / "post.txt"
    job.write_bytes(data)
    assert _new_run(root, "--job", str(job)) == 0
    run = Path(capsys.readouterr().out.strip())
    assert (run / JOB).read_bytes() == expected


def test_cli_new_run_json(
    root: Path, tmp_path: Path, today: dt.date, capsys: pytest.CaptureFixture[str]
) -> None:
    job = tmp_path / "post.txt"
    job.write_text(POST, encoding="utf-8")
    assert _new_run(root, "--job", str(job), "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {
        "schema_version": 1,
        "command": "new-run",
        "run": str(root / APPLICATIONS / "2026-11-11-acme-designer"),
    }


def _refused(root: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> str:
    """Run new-run, expect exit code 2 and no run folder, return stderr."""
    assert _new_run(root, *extra) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert not (root / APPLICATIONS).exists()
    return captured.err


def test_cli_missing_job_file_exit_2(
    root: Path, tmp_path: Path, today: dt.date, capsys: pytest.CaptureFixture[str]
) -> None:
    err = _refused(root, capsys, "--job", str(tmp_path / "nope.txt"))
    assert err.startswith("facts new-run: ")
    assert "nope.txt does not exist" in err and "--job -" in err


def test_cli_job_file_that_cannot_be_read_exit_2(
    root: Path, tmp_path: Path, today: dt.date, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "a-folder"
    folder.mkdir()
    err = _refused(root, capsys, "--job", str(folder))
    assert "cannot be read" in err


def test_cli_job_file_that_is_not_utf8_exit_2(
    root: Path, tmp_path: Path, today: dt.date, capsys: pytest.CaptureFixture[str]
) -> None:
    job = tmp_path / "post.txt"
    job.write_bytes(b"Caf\xe9 job post.\n")
    err = _refused(root, capsys, "--job", str(job))
    assert "post.txt is not UTF-8 text" in err
    assert "plain text" in err and "--job -" in err


def test_cli_stdin_that_is_not_utf8_exit_2(
    root: Path,
    today: dt.date,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stdin(monkeypatch, b"Caf\xe9 job post.\n")
    err = _refused(root, capsys, "--job", "-")
    assert "stdin is not UTF-8 text" in err and "plain text" in err


def test_cli_empty_job_post_exit_2(
    root: Path, tmp_path: Path, today: dt.date, capsys: pytest.CaptureFixture[str]
) -> None:
    job = tmp_path / "post.txt"
    job.write_text("  \n\n", encoding="utf-8")
    assert "job post is empty" in _refused(root, capsys, "--job", str(job))


def test_cli_empty_stdin_exit_2(
    root: Path,
    today: dt.date,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stdin(monkeypatch, b"")
    assert "job post is empty" in _refused(root, capsys, "--job", "-")


def test_cli_name_with_an_empty_slug_exit_2(
    root: Path, tmp_path: Path, today: dt.date, capsys: pytest.CaptureFixture[str]
) -> None:
    job = tmp_path / "post.txt"
    job.write_text(POST, encoding="utf-8")
    assert _new_run(root, "--job", str(job), company="???") == 2
    err = capsys.readouterr().err
    assert "gives an empty name" in err
    assert not (root / APPLICATIONS).exists()


def test_cli_no_data_root_exit_2(
    tmp_path: Path, today: dt.date, capsys: pytest.CaptureFixture[str]
) -> None:
    job = tmp_path / "post.txt"
    job.write_text(POST, encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _new_run(empty, "--job", str(job)) == 2
    assert "No master.yaml found" in capsys.readouterr().err
    assert list(empty.iterdir()) == []


def test_cli_second_run_on_the_same_day_exit_2(
    root: Path, tmp_path: Path, today: dt.date, capsys: pytest.CaptureFixture[str]
) -> None:
    job = tmp_path / "post.txt"
    job.write_text(POST, encoding="utf-8")
    assert _new_run(root, "--job", str(job)) == 0
    capsys.readouterr()
    before = _tree(root)
    other = tmp_path / "other.txt"
    other.write_text("Another post.", encoding="utf-8")
    assert _new_run(root, "--job", str(other)) == 2
    assert "already exists" in capsys.readouterr().err
    assert _tree(root) == before


def test_cli_write_failure_exit_1(
    root: Path,
    tmp_path: Path,
    today: dt.date,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", fail)
    job = tmp_path / "post.txt"
    job.write_text(POST, encoding="utf-8")
    assert _new_run(root, "--job", str(job)) == 1
    err = capsys.readouterr().err
    assert "cannot write the run folder" in err and "Traceback" not in err


class _FailingStdin:
    """A stdin whose read fails with an OSError (for example a disk error)."""

    class _Buffer:
        def read(self) -> bytes:
            raise OSError(5, "Input/output error")

    buffer = _Buffer()


def test_cli_stdin_read_failure_has_its_own_message(
    root: Path,
    today: dt.date,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", _FailingStdin())
    assert _new_run(root, "--job", "-") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "facts new-run: cannot read the job post from stdin: Input/output error. "
        "Save the job post to a file and pass --job FILE.\n"
    )
    assert not (root / APPLICATIONS).exists()


# facts new-run --url: see tests/test_fetch.py


def test_cli_job_and_url_together_exit_2(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = tmp_path / "post.txt"
    job.write_text(POST, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _new_run(root, "--job", str(job), "--url", "https://jobs.example.com/x")
    assert exc.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err
    assert not (root / APPLICATIONS).exists()


def test_cli_no_job_and_no_url_exit_2(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        _new_run(root)
    assert exc.value.code == 2
    assert "one of the arguments --job --url is required" in capsys.readouterr().err
    assert not (root / APPLICATIONS).exists()
