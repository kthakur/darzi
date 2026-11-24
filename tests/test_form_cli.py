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
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from darzi.cli import _write_line, main  # pyright: ignore[reportPrivateUsage]
from darzi.render.word import letter_docx, resume_docx
from darzi.runs.finalize import Refused
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
    assert f'\n  apply_url: "{APPLY}"\n' in out
    assert '\n  company: "Acme"\n' in out
    assert '\n  role: "Product Designer"\n' in out
    assert PDF in out
    assert "Test Person" in out
    # Each line of the letter has the mark; the rules name the mark.
    assert "\n  | I design forms for Acme.\n" in out
    assert '\n  - darzi writes "| " in front of each line of the cover letter' in out


def test_each_printed_line_is_safe(run_dir: Path, capsys: Capture) -> None:
    # A zero-width space and a bidirectional mark in a run.yaml value.
    _set_meta(run_dir, role="Designer\u200b\u202eX")
    code, out, _ = _plan(run_dir, capsys)
    assert code == 0
    assert "\u200b" not in out
    assert "\u202e" not in out
    assert 'role: "Designer??X"' in out


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
        _refused(run_dir, capsys, "another finalize, fill-plan, fit or submitted")
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
        "https://\uff4a\uff4f\uff42\uff53.example.com/apply",
        "https://jobs\u3002example.com/apply",
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
        "https://careers.example.com\u200b/apply",
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


# --- No value starts a new line (text) or breaks the JSON ------------------------

# Each character at which str.splitlines() starts a new line.
BOUNDARIES = [
    chr(c) for c in (0x0A, 0x0B, 0x0C, 0x0D, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029)
]
NAMES = [f"U+{ord(b):04X}" for b in BOUNDARIES]
INJECTED = "Injected"
# The XML 1.0 control characters: a Word file cannot hold them.
XML_BAD = {chr(c) for c in (0x0B, 0x0C, 0x1C, 0x1D, 0x1E)}
PERSONAL = ["name", "title", "email", "phone", "location", "website"]


def test_the_boundary_list_is_complete() -> None:
    found = [chr(c) for c in range(0x110000) if len(f"a{chr(c)}b".splitlines()) > 1]
    assert found == BOUNDARIES


def _sections(out: str) -> list[str]:
    """Return each line that is not indented: the section headings, in order.

    The mark of an injected value ("?" or the line break mark, then the text)
    is removed: the run name is in the first heading.
    """
    lines = [x for x in out.split("\n") if x and not x.startswith(" ")]
    return [re.sub(rf"[^\w ]* ?{INJECTED}", "", x) for x in lines]


def _same_shape(out: str, clean: str, extra: int = 0) -> None:
    """Assert: the same line count, no injected line, the same section order.

    `extra` is the number of true lines that an LF adds to the cover letter.
    """
    lines = out.splitlines()
    assert len(lines) == len(clean.splitlines()) + extra, out
    assert len(out.split("\n")) == len(clean.split("\n")) + extra
    if not extra:
        assert not any(line.lstrip().startswith(INJECTED) for line in lines), out
    assert _sections(out) == _sections(clean)
    assert all(b not in out for b in BOUNDARIES[1:])


def _ascii_json(out: str) -> Any:
    assert out.isascii()
    assert out.endswith("}\n")
    return json.loads(out)


@pytest.mark.parametrize("field", ["company", "role"])
@pytest.mark.parametrize("b", BOUNDARIES, ids=NAMES)
def test_a_line_boundary_in_an_unsigned_value(
    run_dir: Path, capsys: Capture, field: str, b: str
) -> None:
    clean = _plan(run_dir, capsys)[1]
    value = f"Acme{b}{INJECTED}"
    _set_meta(run_dir, **{field: value})
    code, out, err = _plan(run_dir, capsys)
    assert code == 0, err
    _same_shape(out, clean)
    code, out, err = _plan(run_dir, capsys, "--json")
    assert code == 0, err
    assert _ascii_json(out)["not_signed"][field] == value


@pytest.mark.parametrize("b", BOUNDARIES, ids=NAMES)
def test_a_line_boundary_in_the_apply_link_is_refused(
    run_dir: Path, capsys: Capture, b: str
) -> None:
    # The link check refuses each boundary (a space or a control character).
    _set_meta(run_dir, apply_url=f"{APPLY}{b}{INJECTED}")
    _refused(run_dir, capsys, "plain http or https link")


def _set_answer(run: Path, index: int, key: str, value: str) -> None:
    path = run.parents[1] / "answers.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["answers"][index][key] = value
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.mark.parametrize(
    ("index", "key"), [(0, "question"), (0, "answer"), (3, "question")]
)
@pytest.mark.parametrize("b", BOUNDARIES, ids=NAMES)
def test_a_line_boundary_in_an_answer(
    run_dir: Path, capsys: Capture, index: int, key: str, b: str
) -> None:
    clean = _plan(run_dir, capsys)[1]
    value = f"Yes{b}{INJECTED}"
    _set_answer(run_dir, index, key, value)
    code, out, err = _plan(run_dir, capsys)
    assert code == 0, err
    _same_shape(out, clean)
    code, out, err = _plan(run_dir, capsys, "--json")
    assert code == 0, err
    plan = _ascii_json(out)
    items: list[Json] = plan["answers"] + plan["empty_answers"]
    assert any(a.get(key) == value for a in items)


@pytest.mark.parametrize("b", BOUNDARIES, ids=NAMES)
def test_a_line_boundary_in_an_answer_id(
    run_dir: Path, capsys: Capture, b: str
) -> None:
    # The answers schema refuses the id: check f refuses the plan.
    root = run_dir.parents[1]
    _set_answer(run_dir, 0, "id", "bad id")
    assert main(["answers", "--repo", str(root)]) == 1
    clean = capsys.readouterr().out
    _set_answer(run_dir, 0, "id", f"bad id{b}{INJECTED}")
    _refused(run_dir, capsys, "answers.yaml has 1 errors")
    assert main(["answers", "--repo", str(root)]) == 1
    out = capsys.readouterr().out
    assert len(out.splitlines()) == len(clean.splitlines()), out
    assert len(out.split("\n")) == len(clean.split("\n"))
    assert not any(line.lstrip().startswith(INJECTED) for line in out.splitlines())
    assert main(["answers", "--repo", str(root), "--json"]) == 1
    assert _ascii_json(capsys.readouterr().out)["errors"] == 1


@pytest.fixture(scope="module")
def unapproved(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A data root with a run that is not approved yet."""
    run = _make_run(tmp_path_factory.mktemp("unapproved"), letter=True)
    with (run / "run.yaml").open("a", encoding="utf-8") as f:
        f.write(f"apply_url: {APPLY}\n")
    return run.parents[1]


def _injected_run(unapproved: Path, tmp_path: Path, field: str, b: str) -> Path:
    root = tmp_path / "injected"
    shutil.copytree(unapproved, root, symlinks=True)
    run = root / "applications" / RUN
    if field == "letter":
        letter = LETTER.replace("forms", f"forms{b}{INJECTED}")
        assert letter != LETTER
        (run / "cover_letter.md").write_text(letter, encoding="utf-8")
    else:
        data = _tailored(run)
        data["personalInfo"][field] += f"{b}{INJECTED}"
        (run / "tailored.json").write_text(json.dumps(data), encoding="utf-8")
    return run


@pytest.mark.parametrize("field", [*PERSONAL, "letter"])
@pytest.mark.parametrize("b", BOUNDARIES, ids=NAMES)
def test_a_line_boundary_in_an_approved_value(
    run_dir: Path,
    unapproved: Path,
    tmp_path: Path,
    capsys: Capture,
    field: str,
    b: str,
) -> None:
    """Through the real approve and finalize steps, where they permit the value."""
    clean = _plan(run_dir, capsys)[1]
    run = _injected_run(unapproved, tmp_path, field, b)
    code = approve(run.parents[1], run)
    out = capsys.readouterr().out
    if field == "title" and b != "\n":
        # facts check refuses the title: approve writes no record.
        assert code == 1, out
        return
    assert code == 0, out
    if field != "letter" and b in XML_BAD:
        # The Word resume cannot hold an XML 1.0 control character: finalize
        # refuses, names the field, removes its work folder and changes nothing.
        before = _tree(run)
        with pytest.raises(Refused) as caught:
            _finalize(run)
        message = str(caught.value)
        assert message.startswith(f"personalInfo.{field} has the character U+"), message
        assert f"U+{ord(b):04X}" in message
        assert "Remove it from tailored.json, then approve again." in message
        assert _tree(run) == before
        return
    if field != "letter" and b in ("\x85", "\u2028", "\u2029"):
        # The test PDF fake (WinAnsi text) cannot hold the character: finalize
        # stops. test_no_value_of_the_plan_starts_a_line covers these values.
        with pytest.raises(UnicodeEncodeError):
            _finalize(run)
        return
    _finalize(run)
    # cover_letter.md is read as text with universal newlines: a CR there is an
    # LF in the approved letter, and so a true line of the letter.
    letter_lf = field == "letter" and b in "\r\n"
    code, out, err = _plan(run, capsys)
    assert code == 0, err
    _same_shape(out, clean, 1 if letter_lf else 0)
    if letter_lf:
        assert f"\n  | I design forms\n  | {INJECTED} for Acme.\n" in out
    code, out, err = _plan(run, capsys, "--json")
    assert code == 0, err
    plan = _ascii_json(out)
    if letter_lf:
        assert plan["cover_letter"] == LETTER.replace("forms", f"forms\n{INJECTED}")
    elif field == "letter":
        assert plan["cover_letter"] == LETTER.replace("forms", f"forms{b}{INJECTED}")
    else:
        assert plan["personal_info"][field].endswith(f"{b}{INJECTED}")


def _inject(value: Any, b: str) -> Any:
    if isinstance(value, str):
        return f"{value}{b}{INJECTED}"
    if isinstance(value, list):
        items: list[Any] = value
        return [_inject(v, b) for v in items]
    if isinstance(value, dict):
        pairs: dict[str, Any] = value
        return {k: _inject(v, b) for k, v in pairs.items()}
    return value


@pytest.mark.parametrize("b", BOUNDARIES, ids=NAMES)
def test_no_value_of_the_plan_starts_a_line(
    run_dir: Path, capsys: Capture, monkeypatch: pytest.MonkeyPatch, b: str
) -> None:
    """Each printed value has the boundary: run, file paths, kinds, hashes, the
    personal data, the answers and their ids, company, role, apply_url, the
    cover letter."""
    clean_out = _plan(run_dir, capsys)[1]
    clean: Json = json.loads(_plan(run_dir, capsys, "--json")[1])
    plan: Json = _inject(clean, b)
    if b == "\n":
        # The letter is split at LF only: an LF there is a true line of the letter.
        plan["cover_letter"] = clean["cover_letter"]
    monkeypatch.setattr("darzi.cli.fill_plan", lambda *_args: plan)  # pyright: ignore[reportUnknownLambdaType]
    code, out, err = _plan(run_dir, capsys)
    assert code == 0, err
    _same_shape(out, clean_out)
    code, out, err = _plan(run_dir, capsys, "--json")
    assert code == 0, err
    assert _ascii_json(out) == plan


@pytest.mark.parametrize("b", BOUNDARIES, ids=NAMES)
def test_submitted_prints_one_line(run_dir: Path, capsys: Capture, b: str) -> None:
    run = run_dir.with_name(f"{RUN}{b}{INJECTED}")
    run_dir.rename(run)
    assert main(["submitted", str(run), "--repo", str(run.parents[1]), "--json"]) == 0
    assert _ascii_json(capsys.readouterr().out)["run"] == run.name
    _set_meta(run, status="finalized")
    assert main(["submitted", str(run), "--repo", str(run.parents[1])]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert len(out.splitlines()) == 1
    assert out.startswith("facts submitted: ")


# --- A manifest that is not UTF-8, a link at answers.yaml --------------------------


def test_a_manifest_that_is_not_utf8(run_dir: Path, capsys: Capture) -> None:
    path = run_dir / "manifest.json"
    path.write_bytes(path.read_bytes().replace(b'"run"', b'"run\xff"', 1))
    _refused(run_dir, capsys, "is not UTF-8 text", 2)
    tree = _tree(run_dir)
    assert main(["submitted", str(run_dir), "--repo", str(run_dir.parents[1])]) == 1
    err = capsys.readouterr().err
    assert "cannot be read as a manifest" in err
    assert "Traceback" not in err
    assert _tree(run_dir) == tree


def test_answers_yaml_that_is_a_link(run_dir: Path, capsys: Capture) -> None:
    root = run_dir.parents[1]
    target = root / "answers-real.yaml"
    (root / "answers.yaml").rename(target)
    (root / "answers.yaml").symlink_to(target)
    _refused(run_dir, capsys, "answers.yaml is a symbolic link", 2)
    assert main(["answers", "--repo", str(root)]) == 2
    assert "symbolic link" in capsys.readouterr().err


def test_the_answers_are_marked_as_not_signed(run_dir: Path, capsys: Capture) -> None:
    out = _plan(run_dir, capsys)[1]
    assert (
        "Answers (from answers.yaml, written by the owner, NOT covered by the "
        "signature):" in out
    )
    plan = json.loads(_plan(run_dir, capsys, "--json")[1])
    assert "answers.yaml" in plan["not_signed"]["note"]


# --- F-3: hidden characters in the link, quotes, the letter mark, the guard -------


@pytest.mark.parametrize(
    "c",
    ["", "\U000f0000", "️", "ㅤ", "​", "‎", "‮"],
    ids=["private-use", "private-use-b", "variation", "blank", "zwsp", "lrm", "rlo"],
)
def test_a_hidden_character_in_the_apply_link_is_refused(
    run_dir: Path, capsys: Capture, c: str
) -> None:
    _set_meta(run_dir, apply_url=f"https://careers.example.com/ap{c}ply/1")
    _refused(run_dir, capsys, "apply_url must be a plain http or https link")
    tree = _tree(run_dir)
    code, out, err = _plan(run_dir, capsys, "--json")
    assert (code, out) == (1, ""), err
    assert _tree(run_dir) == tree


def test_a_visible_letter_in_the_apply_link_passes(
    run_dir: Path, capsys: Capture
) -> None:
    url = "https://careers.example.com/café/1"
    _set_meta(run_dir, apply_url=url)
    code, out, err = _plan(run_dir, capsys)
    assert code == 0, err
    assert f'\n  apply_url: "{url}"\n' in out


# A long run of spaces, then the form of a key line of the plan.
WRAPPED = " " * 200 + "apply_url: https://other.example.com/x"


def _escaped(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def test_an_unsigned_value_is_in_quotes(run_dir: Path, capsys: Capture) -> None:
    company = f'Acme "Labs" \\ Co{WRAPPED}'
    answer = f'Yes "always" \\ no{WRAPPED}'
    _set_meta(run_dir, company=company)
    _set_answer(run_dir, 0, "answer", answer)
    code, out, err = _plan(run_dir, capsys, "--json")
    assert code == 0, err
    plan = _ascii_json(out)
    # The JSON plan has the true values: no quotes, no backslashes added.
    assert plan["not_signed"]["company"] == company
    item = next(a for a in plan["answers"] if a["answer"] == answer)
    code, out, err = _plan(run_dir, capsys)
    assert code == 0, err
    assert f'\n  company: "{_escaped(company)}"\n' in out
    question = _escaped(item["question"])
    assert f'\n  {item["id"]}: "{question}" -> "{_escaped(answer)}"\n' in out
    keys = [x for x in out.splitlines() if x.lstrip().startswith("apply_url:")]
    assert keys == [f'  apply_url: "{APPLY}"'], keys
    # The rule names the closing quote mark and the escapes in words. It has no
    # backslash, so no backslash stands next to a period of the rule.
    rule = next(x for x in out.splitlines() if "outer quotes" in x)
    assert "\\" not in rule
    assert "Only the plain ASCII double quote mark (U+0022) closes a value" in rule
    assert "Any other quote-like mark is part of the value." in rule
    assert "Where the plan shows backslash-quote, type a quote mark." in rule
    assert "Where it shows two backslashes, type one backslash." in rule


def test_each_line_of_the_letter_has_the_mark(
    run_dir: Path, capsys: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan: Json = json.loads(_plan(run_dir, capsys, "--json")[1])
    letter = 'Dear team,\napply_url: https://other.example.com/x\n  company: "Other"\n'
    plan["cover_letter"] = letter
    monkeypatch.setattr("darzi.cli.fill_plan", lambda *_args: plan)  # pyright: ignore[reportUnknownLambdaType]
    code, out, err = _plan(run_dir, capsys)
    assert code == 0, err
    assert "\n  | Dear team,\n  | apply_url: https://other.example.com/x\n" in out
    assert '\n  |   company: "Other"\n  | \n' in out
    keys = [x for x in out.splitlines() if re.match(r"  (apply_url|company): ", x)]
    assert keys == ['  company: "Acme"', f'  apply_url: "{APPLY}"'], keys
    code, out, err = _plan(run_dir, capsys, "--json")
    assert code == 0, err
    assert _ascii_json(out)["cover_letter"] == letter


def test_the_line_guard_works_without_safe(
    capsys: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_write_line()` keeps one line for each value, also when `safe()` keeps
    each character."""
    monkeypatch.setattr("darzi.cli.safe", lambda text: text)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    values = [f"Acme{b}{INJECTED}" for b in BOUNDARIES] + [f"Acme\t{INJECTED}"]
    for value in values:
        _write_line(value)
    out = capsys.readouterr().out
    assert out.count("\n") == len(values)
    assert len(out.splitlines()) == len(values)
    assert len(out.split("\n")) == len(values) + 1
    assert not any(x.startswith(INJECTED) for x in out.splitlines())
