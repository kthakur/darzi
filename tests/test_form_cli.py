"""`facts fill-plan RUN` and `facts submitted RUN` (W-9).

Synthetic data only. A run is approved with a key that the test makes in a
temporary folder, and finalized with the fake renderer of the W-8 tests. Each
refusal test checks the exit code, that stdout is empty, that no traceback is
printed, and that the run folder did not change.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from darzi.cli import main
from darzi.render.word import letter_docx, resume_docx
from darzi.runs.lock import lock_path

from .pdf_fakes import pdf_bytes
from .resume_pdf import pdf_lines
from .signing_root import approve

# The W-8 helpers: the same approved and finalized run, the same tree compare.
from .test_finalize import (  # pyright: ignore[reportPrivateUsage]
    LETTER,
    RUN,
    _finalize,  # pyright: ignore[reportPrivateUsage]
    _make_run,  # pyright: ignore[reportPrivateUsage]
    _tree,  # pyright: ignore[reportPrivateUsage]
)

APPLY = "https://careers.example.com/apply/1"
PDF = "out/Test_Person_Resume.pdf"
WORD = "out/Test_Person_Resume.docx"
LETTER_FILE = "out/Test_Person_Cover_Letter.docx"

Capture = pytest.CaptureFixture[str]
Json = dict[str, Any]


def _base(folder: Path, letter: bool) -> Path:
    run = _make_run(folder, letter=letter)
    with (run / "run.yaml").open("a", encoding="utf-8") as f:
        f.write(f"apply_url: {APPLY}\n")
    assert approve(run.parents[1], run) == 0
    _finalize(run)
    return run.parents[1]


@pytest.fixture(scope="module")
def bases(tmp_path_factory: pytest.TempPathFactory) -> dict[bool, Path]:
    """Two finalized data roots: with and without a cover letter."""
    return {
        True: _base(tmp_path_factory.mktemp("letter"), True),
        False: _base(tmp_path_factory.mktemp("no-letter"), False),
    }


def _copy(bases: dict[bool, Path], tmp_path: Path, letter: bool = True) -> Path:
    root = tmp_path / "root"
    shutil.copytree(bases[letter], root, symlinks=True)
    return root / "applications" / RUN


@pytest.fixture
def run_dir(bases: dict[bool, Path], tmp_path: Path) -> Path:
    """A copy of the finalized run with a cover letter."""
    return _copy(bases, tmp_path)


def _plan(run: Path, capsys: Capture, *extra: str) -> tuple[int, str, str]:
    code = main(["fill-plan", str(run), "--repo", str(run.parents[1]), *extra])
    out, err = capsys.readouterr()
    return code, out, err


def _refused(
    run: Path, capsys: Capture, match: str, code: int = 1, repo: Path | None = None
) -> None:
    before = _tree(run)
    got = main(["fill-plan", str(run), "--repo", str(repo or run.parents[1])])
    out, err = capsys.readouterr()
    assert got == code, err
    assert out == ""
    assert "Traceback" not in err
    assert match in err, err
    assert len(err.strip().splitlines()) == 1
    assert _tree(run) == before


def _manifest(run: Path) -> Json:
    return json.loads((run / "manifest.json").read_text(encoding="utf-8"))


def _set_manifest(run: Path, change: Callable[[Json], None]) -> None:
    data = _manifest(run)
    change(data)
    (run / "manifest.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def _entry(manifest: Json, kind: str) -> Json:
    return next(f for f in manifest["files"] if f["kind"] == kind)


def _meta(run: Path) -> Json:
    return yaml.safe_load((run / "run.yaml").read_text(encoding="utf-8"))


def _set_meta(run: Path, **values: Any) -> None:
    meta = _meta(run)
    for key, value in values.items():
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
    (run / "run.yaml").write_text(yaml.safe_dump(meta, sort_keys=False), "utf-8")


def _replace_file(run: Path, path: str, data: bytes) -> None:
    """Replace an output file AND its hash in the manifest."""
    (run / path).write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()

    def change(m: Json) -> None:
        next(f for f in m["files"] if f["path"] == path)["sha256"] = digest

    _set_manifest(run, change)


def _tailored(run: Path) -> Json:
    return json.loads((run / "tailored.json").read_text(encoding="utf-8"))


# --- Success ---------------------------------------------------------------------


def test_the_plan_lists_the_files_and_the_answers(
    run_dir: Path, capsys: Capture
) -> None:
    before = _tree(run_dir)
    code, out, err = _plan(run_dir, capsys, "--json")
    assert code == 0, err
    plan = json.loads(out)
    assert plan["schema_version"] == 1
    assert plan["command"] == "fill-plan"
    assert plan["run"] == RUN
    manifest = _manifest(run_dir)
    assert plan["files"] == [
        {"path": f["path"], "kind": f["kind"], "sha256": f["sha256"]}
        for f in manifest["files"]
    ]
    assert {f["path"] for f in plan["files"]} == {PDF, WORD, LETTER_FILE}
    assert plan["personal_info"] == _tailored(run_dir)["personalInfo"]
    assert plan["personal_info"]["name"] == "Test Person"
    assert plan["cover_letter"] == LETTER
    ids = [a["id"] for a in plan["answers"]]
    assert "work_authorized_us" in ids
    assert "willing_to_relocate" not in ids
    assert {
        "id": "willing_to_relocate",
        "question": "Are you willing to relocate?",
    } in (plan["empty_answers"])
    assert any("Submit" in rule for rule in plan["rules"])
    unsigned = plan["not_signed"]
    assert unsigned["apply_url"] == APPLY
    assert unsigned["company"] == "Acme"
    assert unsigned["role"] == "Product Designer"
    assert "signature" in unsigned["note"]
    assert set(plan) == {
        "schema_version",
        "command",
        "run",
        "files",
        "personal_info",
        "cover_letter",
        "answers",
        "empty_answers",
        "rules",
        "not_signed",
    }
    assert _tree(run_dir) == before


def test_the_plan_without_a_cover_letter(
    bases: dict[bool, Path], tmp_path: Path, capsys: Capture
) -> None:
    run = _copy(bases, tmp_path, letter=False)
    code, out, err = _plan(run, capsys, "--json")
    assert code == 0, err
    plan = json.loads(out)
    assert plan["cover_letter"] is None
    assert {f["path"] for f in plan["files"]} == {PDF, WORD}


def test_the_text_plan(run_dir: Path, capsys: Capture) -> None:
    code, out, _ = _plan(run_dir, capsys)
    assert code == 0
    assert "NOT covered by the signature" in out
    assert f"apply_url: {APPLY}" in out
    assert PDF in out
    assert "Test Person" in out


def test_each_printed_line_is_safe(run_dir: Path, capsys: Capture) -> None:
    # A zero-width space and a bidirectional mark in a run.yaml value.
    _set_meta(run_dir, role="Designer​‮X")
    code, out, _ = _plan(run_dir, capsys)
    assert code == 0
    assert "​" not in out
    assert "‮" not in out
    assert "role: Designer??X" in out


def test_an_empty_block_list_permits_each_host(run_dir: Path, capsys: Capture) -> None:
    toml = run_dir.parents[1] / "facts.toml"
    text = toml.read_text(encoding="utf-8")
    old = 'blocked_hosts = ["jobs.example.com", "board.example.com"]'
    toml.write_text(text.replace(old, "blocked_hosts = []"), encoding="utf-8")
    _set_meta(run_dir, apply_url="https://jobs.example.com/apply")
    assert _plan(run_dir, capsys)[0] == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://notjobs.example.com/apply",
        "https://jobs.example.com.other.example/apply",
        "http://careers.example.com:8080/apply?id=1",
    ],
)
def test_a_host_that_is_not_blocked_passes(
    run_dir: Path, capsys: Capture, url: str
) -> None:
    _set_meta(run_dir, apply_url=url)
    code, out, err = _plan(run_dir, capsys, "--json")
    assert code == 0, err
    assert json.loads(out)["not_signed"]["apply_url"] == url


# --- a. The run folder, the links, the lock and the status -----------------------


def test_a_run_of_another_data_root(
    run_dir: Path, capsys: Capture, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    shutil.copytree(run_dir.parents[1], other, symlinks=True)
    _refused(run_dir, capsys, "is not a run folder of the data root", 2, other)


def test_a_run_folder_that_is_a_link(run_dir: Path, capsys: Capture) -> None:
    link = run_dir.parent / "2026-11-17-acme-link"
    link.symlink_to(run_dir)
    _refused(link, capsys, "is not a run folder of the data root", 2)


@pytest.mark.parametrize("name", ["run.yaml", "manifest.json", "out"])
def test_a_link_in_the_run_folder(
    run_dir: Path, capsys: Capture, tmp_path: Path, name: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.move(run_dir / name, outside / name)
    (run_dir / name).symlink_to(outside / name)
    _refused(run_dir, capsys, "is a symbolic link", 2)


def test_a_held_lock(run_dir: Path, capsys: Capture) -> None:
    fd = os.open(lock_path(run_dir), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _refused(run_dir, capsys, "another finalize, fill-plan or submitted")
    finally:
        os.close(fd)


def test_a_submitted_run(run_dir: Path, capsys: Capture) -> None:
    _set_meta(run_dir, status="submitted")
    _refused(run_dir, capsys, "already submitted")


def test_a_run_that_is_not_finalized(run_dir: Path, capsys: Capture) -> None:
    _set_meta(run_dir, status="new")
    _refused(run_dir, capsys, "not finalized")


# --- b. The approval of the content in memory ------------------------------------


def test_a_changed_tailored_json_after_finalize(run_dir: Path, capsys: Capture) -> None:
    data = _tailored(run_dir)
    data["personalInfo"]["title"] = "Senior Product Designer"
    (run_dir / "tailored.json").write_text(json.dumps(data), encoding="utf-8")
    _refused(run_dir, capsys, "content changed after the approval")


def test_a_changed_cover_letter_after_finalize(run_dir: Path, capsys: Capture) -> None:
    (run_dir / "cover_letter.md").write_text(LETTER + "P.S.\n", encoding="utf-8")
    _refused(run_dir, capsys, "content changed after the approval")


def test_a_record_with_no_signature(run_dir: Path, capsys: Capture) -> None:
    (run_dir / "approval.json.sig").unlink()
    _refused(run_dir, capsys, "is not approved")


# --- c. The manifest ------------------------------------------------------------


def test_no_manifest(run_dir: Path, capsys: Capture) -> None:
    (run_dir / "manifest.json").unlink()
    _refused(run_dir, capsys, "manifest.json does not exist")


def test_a_hand_over_manifest_with_a_valid_signature(
    run_dir: Path, capsys: Capture
) -> None:
    # Spec section 7, the fifth negative test: the approval is valid, but the
    # manifest says signed: false. fill-plan refuses it.
    _set_manifest(run_dir, lambda m: m.update(signed=False))
    _refused(run_dir, capsys, "hand-over manifest")


@pytest.mark.parametrize("value", ["absent", "true", 1, None, [True]])
def test_signed_that_is_not_exactly_true(
    run_dir: Path, capsys: Capture, value: Any
) -> None:
    if value == "absent":
        _set_manifest(run_dir, lambda m: m.pop("signed"))
    else:
        _set_manifest(run_dir, lambda m: m.update(signed=value))
    _refused(run_dir, capsys, "does not say signed: true")


@pytest.mark.parametrize("version", [2, "1", True, None])
def test_a_wrong_schema_version(run_dir: Path, capsys: Capture, version: Any) -> None:
    _set_manifest(run_dir, lambda m: m.update(schema_version=version))
    _refused(run_dir, capsys, "is not schema version 1")


@pytest.mark.parametrize(
    ("text", "code"), [("[]", 1), ('{"signed": true, "signed": true}', 1), ("{", 2)]
)
def test_a_manifest_that_is_not_a_json_object(
    run_dir: Path, capsys: Capture, text: str, code: int
) -> None:
    # Text that is not JSON is invalid input: load_run() (step b) reads the
    # manifest too and stops with exit code 2.
    (run_dir / "manifest.json").write_text(text, encoding="utf-8")
    _refused(run_dir, capsys, "manifest.json", code)


def test_an_extra_manifest_key(run_dir: Path, capsys: Capture) -> None:
    _set_manifest(run_dir, lambda m: m.update(note="x"))
    _refused(run_dir, capsys, "does not have the keys that finalize writes")


def test_a_wrong_run(run_dir: Path, capsys: Capture) -> None:
    _set_manifest(run_dir, lambda m: m.update(run="2026-11-17-acme-other"))
    _refused(run_dir, capsys, "is for a different run")


def test_a_wrong_approval_hash(run_dir: Path, capsys: Capture) -> None:
    _set_manifest(run_dir, lambda m: m.update(approval_hash="0" * 64))
    _refused(run_dir, capsys, "is for a different approval")


def test_a_manifest_of_an_older_approval(run_dir: Path, capsys: Capture) -> None:
    letter = LETTER.replace("Dear team,", "Dear design team,")
    (run_dir / "cover_letter.md").write_text(letter, encoding="utf-8")
    assert approve(run_dir.parents[1], run_dir) == 0
    capsys.readouterr()
    _refused(run_dir, capsys, "is for a different approval")


def test_a_wrong_fingerprint(run_dir: Path, capsys: Capture) -> None:
    _set_manifest(run_dir, lambda m: m.update(signer_fingerprint="SHA256:abc"))
    _refused(run_dir, capsys, "different signer key")


# --- d. The files in out/ ---------------------------------------------------------


def test_a_changed_output_file(run_dir: Path, capsys: Capture) -> None:
    with (run_dir / PDF).open("ab") as f:
        f.write(b"\n% added\n")
    _refused(run_dir, capsys, "changed after finalize")


def test_a_missing_output_file(run_dir: Path, capsys: Capture) -> None:
    (run_dir / WORD).unlink()
    _refused(run_dir, capsys, "is missing, a link, or cannot be read")


@pytest.mark.parametrize("name", ["notes.txt", ".hidden", "folder"])
def test_an_extra_entry_in_out(run_dir: Path, capsys: Capture, name: str) -> None:
    if name == "folder":
        (run_dir / "out" / name).mkdir()
    else:
        (run_dir / "out" / name).write_text("x", encoding="utf-8")
    _refused(run_dir, capsys, "holds other entries")


def test_an_output_file_that_is_a_link(
    run_dir: Path, capsys: Capture, tmp_path: Path
) -> None:
    copy = tmp_path / "copy.pdf"
    shutil.copy(run_dir / PDF, copy)
    (run_dir / PDF).unlink()
    (run_dir / PDF).symlink_to(copy)
    _refused(run_dir, capsys, "is missing, a link, or cannot be read")


@pytest.mark.parametrize("kind", ["absolute", "dots", "other_name", "subfolder"])
def test_a_manifest_path_that_is_not_plain(
    run_dir: Path, capsys: Capture, kind: str
) -> None:
    paths = {
        "absolute": str(run_dir / PDF),
        "dots": "out/../out/Test_Person_Resume.pdf",
        "other_name": "out/Other_Resume.pdf",
        "subfolder": "out/x/Test_Person_Resume.pdf",
    }
    _set_manifest(run_dir, lambda m: _entry(m, "resume_pdf").update(path=paths[kind]))
    _refused(run_dir, capsys, "the manifest path of resume_pdf")


def test_a_missing_kind(run_dir: Path, capsys: Capture) -> None:
    _set_manifest(
        run_dir,
        lambda m: m.update(files=[f for f in m["files"] if f["kind"] != "letter_word"]),
    )
    _refused(run_dir, capsys, "does not name exactly the files")


def test_a_kind_two_times(run_dir: Path, capsys: Capture) -> None:
    _set_manifest(run_dir, lambda m: m["files"].append(dict(m["files"][0])))
    _refused(run_dir, capsys, "a file kind two times")


def test_a_bad_file_entry(run_dir: Path, capsys: Capture) -> None:
    _set_manifest(run_dir, lambda m: m["files"][0].pop("pages"))
    _refused(run_dir, capsys, "wrong keys")


# --- e. The Word files built again; the round-trip check again --------------------


def test_a_replaced_word_resume_with_its_hash(
    run_dir: Path, capsys: Capture, tmp_path: Path
) -> None:
    # The same text with another layout: the round-trip check passes, the
    # second build does not give the same bytes.
    other = tmp_path / "other.docx"
    resume_docx(_tailored(run_dir), other, {"pageSize": "A4", "compactMode": False})
    _replace_file(run_dir, WORD, other.read_bytes())
    _refused(run_dir, capsys, "is not the Word file that the approved content gives")


def test_a_replaced_word_letter_with_its_hash(
    run_dir: Path, capsys: Capture, tmp_path: Path
) -> None:
    other = tmp_path / "other.docx"
    letter_docx(LETTER + "\nP.S. Added text.\n", other)
    _replace_file(run_dir, LETTER_FILE, other.read_bytes())
    _refused(run_dir, capsys, "is not the Word file that the approved content gives")


def test_a_replaced_pdf_without_a_bullet(run_dir: Path, capsys: Capture) -> None:
    data = _tailored(run_dir)
    removed = False
    for section in ("workExperience", "personalProjects"):
        for entry in data.get(section) or []:
            if entry.get("description") and not removed:
                entry["description"] = entry["description"][1:]
                removed = True
    assert removed
    _replace_file(run_dir, PDF, pdf_bytes(pdf_lines(data)))
    _refused(run_dir, capsys, "the round-trip check found")


def test_a_replaced_pdf_with_another_phone(run_dir: Path, capsys: Capture) -> None:
    data = _tailored(run_dir)
    data["personalInfo"]["phone"] = "555-0199"
    _replace_file(run_dir, PDF, pdf_bytes(pdf_lines(data)))
    _refused(run_dir, capsys, "the round-trip check found")


# --- f. answers.yaml --------------------------------------------------------------


def test_an_answer_with_an_error(run_dir: Path, capsys: Capture) -> None:
    path = run_dir.parents[1] / "answers.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("Springfield, ST", "Near Acme"), encoding="utf-8")
    _refused(run_dir, capsys, "answers.yaml has 1 errors")


def test_no_answers_file(run_dir: Path, capsys: Capture) -> None:
    (run_dir.parents[1] / "answers.yaml").unlink()
    _refused(run_dir, capsys, "answers.yaml does not exist", 2)


# --- g. The apply link and the blocked hosts ----------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.example.com/apply",
        "https://www.jobs.example.com/apply",
        "https://JOBS.Example.COM/apply",
        "https://jobs.example.com./apply",
        "https://jobs.example.com:443/apply",
        "https://ｊｏｂｓ.example.com/apply",
        "https://jobs。example.com/apply",
        "https://a.board.example.com/apply",
    ],
)
def test_a_blocked_host(run_dir: Path, capsys: Capture, url: str) -> None:
    _set_meta(run_dir, apply_url=url)
    _refused(run_dir, capsys, "blocked_hosts blocks")


@pytest.mark.parametrize(
    "url",
    [
        "https://user@example.com/apply",
        "https://user:pw@example.com/apply",
        "https://jobs.example.com@example.com/apply",
        "ftp://careers.example.com/apply",
        "javascript:alert(1)",
        "careers.example.com/apply",
        "https://careers.example.com\\@jobs.example.com/",
        "https://careers.example.com/a b",
        "https://careers.example.com/\t",
        "https://careers.example.com​/apply",
        "https://192.0.2.1/apply",
        "https://[2001:db8::1]/apply",
        "https://careers..example.com/apply",
        "https://careers.example.com:99999/apply",
        "https:///apply",
    ],
)
def test_a_link_that_is_not_plain(run_dir: Path, capsys: Capture, url: str) -> None:
    _set_meta(run_dir, apply_url=url)
    _refused(run_dir, capsys, "apply_url must be a plain http or https link")


@pytest.mark.parametrize("value", [None, ""])
def test_no_apply_url(run_dir: Path, capsys: Capture, value: str | None) -> None:
    if value is None:
        _set_meta(run_dir, apply_url=None)
    else:
        _set_meta(run_dir, apply_url=value)
    _refused(run_dir, capsys, "has no apply_url. Write apply_url:")


def test_no_form_table(run_dir: Path, capsys: Capture) -> None:
    toml = run_dir.parents[1] / "facts.toml"
    text = toml.read_text(encoding="utf-8")
    old = '[form]\nblocked_hosts = ["jobs.example.com", "board.example.com"]\n'
    assert old in text
    toml.write_text(text.replace(old, ""), encoding="utf-8")
    _refused(run_dir, capsys, "facts.toml has no [form] table", 2)


def test_a_block_entry_that_is_not_a_host_name(run_dir: Path, capsys: Capture) -> None:
    toml = run_dir.parents[1] / "facts.toml"
    text = toml.read_text(encoding="utf-8")
    old = '"board.example.com"]'
    toml.write_text(text.replace(old, '"https://board.example.com/"]'), "utf-8")
    _refused(run_dir, capsys, "is not a plain host name", 2)


# --- facts submitted --------------------------------------------------------------


def _submitted(run: Path, capsys: Capture, repo: Path | None = None) -> int:
    code = main(["submitted", str(run), "--repo", str(repo or run.parents[1])])
    out, err = capsys.readouterr()
    assert "Traceback" not in err
    if code != 0:
        assert out == ""
    return code


def test_submitted_writes_the_status_and_the_date(
    run_dir: Path, capsys: Capture
) -> None:
    before = _meta(run_dir)
    tree = _tree(run_dir)
    assert _submitted(run_dir, capsys) == 0
    after = _meta(run_dir)
    assert after["status"] == "submitted"
    assert after["submitted"] == dt.date.today().isoformat()
    assert {k: v for k, v in after.items() if k not in ("status", "submitted")} == {
        k: v for k, v in before.items() if k != "status"
    }
    changed = {k for k in _tree(run_dir) if _tree(run_dir)[k] != tree.get(k)}
    assert changed == {"run.yaml"}
    # A second call is refused and changes nothing.
    tree = _tree(run_dir)
    assert _submitted(run_dir, capsys) == 1
    assert _tree(run_dir) == tree
    # fill-plan refuses the submitted run.
    _refused(run_dir, capsys, "already submitted")


def test_submitted_accepts_a_hand_over_manifest(run_dir: Path, capsys: Capture) -> None:
    _set_manifest(run_dir, lambda m: m.update(signed=False))
    assert _submitted(run_dir, capsys) == 0
    assert _meta(run_dir)["status"] == "submitted"


@pytest.mark.parametrize("case", ["new", "no_manifest", "no_signed", "lock"])
def test_submitted_refuses(run_dir: Path, capsys: Capture, case: str) -> None:
    if case == "new":
        _set_meta(run_dir, status="new")
    elif case == "no_manifest":
        (run_dir / "manifest.json").unlink()
    elif case == "no_signed":
        _set_manifest(run_dir, lambda m: m.pop("signed"))
    tree = _tree(run_dir)
    fd = os.open(lock_path(run_dir), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        if case == "lock":
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _submitted(run_dir, capsys) == 1
    finally:
        os.close(fd)
    assert _tree(run_dir) == tree


def test_submitted_refuses_a_run_of_another_root(
    run_dir: Path, capsys: Capture, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    shutil.copytree(run_dir.parents[1], other, symlinks=True)
    tree = _tree(run_dir)
    assert _submitted(run_dir, capsys, other) == 2
    assert _tree(run_dir) == tree
