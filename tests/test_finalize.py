"""`facts finalize RUN` and `manifest.json` (W-8).

The tests use synthetic data and a key that the test makes in a temporary
folder. The run is approved below the terminal test, as in the W-5 tests. CI
has no Resume Matcher and no LibreOffice: the tests use the real adapter with
a fake CLI that writes a small PDF from the content that the adapter sent, a
fake page counter, the real Word builder and the real round-trip check.
"""

from __future__ import annotations

import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from darzi.cli import main
from darzi.config import load_config
from darzi.facts.check import load_run
from darzi.facts.repo import InputError
from darzi.facts.validate import Finding
from darzi.render import resume_matcher
from darzi.render.port import RenderError
from darzi.render.resume_matcher import ResumeMatcher, Runner, clone_commit
from darzi.runs import finalize as finalize_module
from darzi.runs.approve import run_is_approved
from darzi.runs.finalize import (
    NO_LINKS,
    Finalized,
    Refused,
    RoundTripFailed,
    finalize,
    lock_path,
)
from darzi.runs.roundtrip import check_letter, check_resume, extract_text
from darzi.runs.runs import content_hash
from darzi.runs.signing import record_bytes, run_target

from .pdf_fakes import pdf_bytes
from .render_fakes import FakeCli, fake_install, fake_renderer
from .resume_pdf import pdf_lines
from .signing_root import approve, make_key, sign_bytes, signed_copy

RUN = "2026-11-17-acme-product-designer"
DAY = dt.date(2026, 11, 18)
LETTER = "Dear team,\n\nI design forms for Acme.\n\nKind regards,\nTest Person\n"
RUN_YAML = (
    "schema_version: 1\n"
    "company: Acme\n"
    "role: Product Designer\n"
    "created: '2026-11-17'\n"
    "type: ux\n"
    "selection_hash: null\n"
    "status: new\n"
    "print_settings:\n"
    "  compactMode: true\n"
)
MANIFEST_KEYS = [
    "schema_version",
    "run",
    "date",
    "signed",
    "approval_hash",
    "signer_fingerprint",
    "renderer",
    "print_settings",
    "files",
]
NAMES = [
    "Test_Person_Cover_Letter.docx",
    "Test_Person_Resume.docx",
    "Test_Person_Resume.pdf",
]

Resume = dict[str, Any]


@dataclass
class PdfCli(FakeCli):
    """The fake CLI; its PDF shows the resume that the adapter sent."""

    change: Callable[[Resume], None] | None = None

    def _answer(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "export_resume_pdf":
            record = next(
                r for r in self.records if r["resume_id"] == args["resume_id"]
            )
            data: Resume = copy.deepcopy(record["data"])
            if self.change is not None:
                self.change(data)
            self.write_pdf = pdf_bytes(pdf_lines(data))
        return super()._answer(tool, args)


def _make_run(tmp_path: Path, letter: bool = True) -> Path:
    root = signed_copy(tmp_path / "signed")
    run = root / "applications" / RUN
    selection = root / "selections" / "ux.yaml"
    assert main(["export", str(selection), "--run", str(run), "--repo", str(root)]) == 0
    shutil.copy(run / "base.json", run / "tailored.json")
    if letter:
        (run / "cover_letter.md").write_text(LETTER, encoding="utf-8")
    (run / "run.yaml").write_text(RUN_YAML, encoding="utf-8")
    return run


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """An approved run folder with a cover letter."""
    run = _make_run(tmp_path)
    assert approve(run.parents[1], run) == 0
    return run


def _renderer(run: Path, cli: Runner) -> ResumeMatcher:
    return ResumeMatcher(fake_install(Path(tempfile.mkdtemp(dir=run.parents[2]))), cli)


def _finalize(
    run: Path,
    cli: Runner | None = None,
    *,
    office: bool = True,
    count: Callable[[Path], int] = lambda _path: 1,
) -> Finalized:
    renderer = _renderer(run, cli or PdfCli())
    return finalize(
        run.parents[1],
        run,
        renderer_for=lambda _repo, _render: renderer,
        count_pages=count,
        office_ready=lambda: office,
        today=DAY,
    )


def _tree(folder: Path) -> dict[str, str | bytes]:
    """Return each path of `folder` with its bytes; no link is followed."""
    tree: dict[str, str | bytes] = {}
    for top, folders, files in os.walk(folder):
        for name in folders + files:
            path = Path(top) / name
            key = str(path.relative_to(folder))
            if path.is_symlink():
                tree[key] = "link " + os.readlink(path)
            elif path.is_dir():
                tree[key] = "folder"
            else:
                tree[key] = path.read_bytes()
    return tree


def _manifest(run: Path) -> dict[str, Any]:
    return json.loads((run / "manifest.json").read_text(encoding="utf-8"))


def _status(run: Path) -> Any:
    return yaml.safe_load((run / "run.yaml").read_text(encoding="utf-8"))["status"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fingerprint(run: Path) -> str | None:
    root = run.parents[1]
    verdict = run_is_approved(root, load_config(root).approval, run, load_run(run))
    return verdict.fingerprint


# --- Success ------------------------------------------------------------------


def test_three_files_and_a_manifest(run_dir: Path) -> None:
    (run_dir / "check.json").unlink()
    before = _tree(run_dir)
    done = _finalize(run_dir)
    manifest = _manifest(run_dir)
    assert done.manifest == manifest
    assert list(manifest) == MANIFEST_KEYS
    text = (run_dir / "manifest.json").read_text(encoding="utf-8")
    assert text == json.dumps(manifest, indent=2) + "\n"
    run = load_run(run_dir)
    assert manifest["schema_version"] == 1
    assert manifest["run"] == RUN
    assert manifest["date"] == "2026-11-18"
    assert manifest["signed"] is True
    assert manifest["approval_hash"] == content_hash(run.tailored, run.letter)
    fingerprint = _fingerprint(run_dir)
    assert fingerprint and fingerprint.startswith("SHA256:")
    assert manifest["signer_fingerprint"] == fingerprint
    assert manifest["renderer"] == {
        "name": "resume-matcher",
        "template": "swiss-single",
        "page_size": "LETTER",
        "commit": None,
    }
    assert manifest["print_settings"] == {"pageSize": "LETTER", "compactMode": True}
    assert sorted(p.name for p in (run_dir / "out").iterdir()) == NAMES
    files = manifest["files"]
    assert [f["kind"] for f in files] == ["resume_pdf", "resume_word", "letter_word"]
    assert [f["reproducible"] for f in files] == [False, True, True]
    for item in files:
        assert item["path"].startswith("out/")
        assert item["sha256"] == _sha(run_dir / item["path"])
        assert item["pages"] == 1
    # Only out/, manifest.json and the status changed. check.json is not written.
    after = _tree(run_dir)
    assert {k for k in after if after[k] != before.get(k)} == {
        "out",
        *(f"out/{name}" for name in NAMES),
        "manifest.json",
        "run.yaml",
    }
    assert set(before) - set(after) == set()
    assert _status(run_dir) == "finalized"
    meta = (run_dir / "run.yaml").read_text(encoding="utf-8")
    assert meta == RUN_YAML.replace("status: new", "status: finalized")


def test_two_files_without_a_cover_letter(tmp_path: Path) -> None:
    run = _make_run(tmp_path, letter=False)
    assert approve(run.parents[1], run) == 0
    _finalize(run)
    files = _manifest(run)["files"]
    assert [f["kind"] for f in files] == ["resume_pdf", "resume_word"]
    assert sorted(p.name for p in (run / "out").iterdir()) == NAMES[1:]


def test_the_commit_of_the_clone_is_in_the_manifest(run_dir: Path) -> None:
    cli = PdfCli()
    renderer = _renderer(run_dir, cli)
    git = renderer.config.path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text("ab" * 20 + "\n", encoding="utf-8")
    finalize(
        run_dir.parents[1],
        run_dir,
        renderer_for=lambda _repo, _render: renderer,
        count_pages=lambda _path: 1,
        office_ready=lambda: True,
        today=DAY,
    )
    assert _manifest(run_dir)["renderer"]["commit"] == "ab" * 20


def test_the_approved_content_is_read_one_time(run_dir: Path) -> None:
    run = load_run(run_dir)
    approved = content_hash(run.tailored, run.letter)
    bullet = run.tailored["workExperience"][0]["description"][0]
    changed = copy.deepcopy(run.tailored)
    changed["workExperience"][0]["description"][0] = "Wrote a text nobody approved."
    cli = PdfCli()
    renderer = _renderer(run_dir, cli)

    def renderer_for(_repo: Path, _render: object) -> ResumeMatcher:
        # finalize calls this after the gate: change the files on disk now.
        (run_dir / "tailored.json").write_text(json.dumps(changed), "utf-8")
        (run_dir / "cover_letter.md").write_text("A new letter.\n", "utf-8")
        return renderer

    finalize(
        run_dir.parents[1],
        run_dir,
        renderer_for=renderer_for,
        count_pages=lambda _path: 1,
        office_ready=lambda: True,
        today=DAY,
    )
    manifest = _manifest(run_dir)
    assert manifest["approval_hash"] == approved
    sent = next(args for tool, args in cli.calls if tool == "create_master_resume")
    assert sent["resume_data"] == run.tailored
    out = run_dir / "out"
    for name in NAMES[1:]:
        assert "nobody approved" not in extract_text(out / name)
        assert check_resume(run.tailored, out / name) == []
        assert check_resume(changed, out / name) != []
    assert bullet != changed["workExperience"][0]["description"][0]
    assert check_letter(LETTER, out / NAMES[0]) == []
    for item in manifest["files"]:
        assert item["sha256"] == _sha(run_dir / item["path"])
    # The changed files on disk are not approved: the next finalize refuses.
    with pytest.raises(Refused, match="not approved"):
        _finalize(run_dir)


def test_a_second_finalize_replaces_the_files_and_the_manifest(run_dir: Path) -> None:
    _finalize(run_dir)
    first = _manifest(run_dir)
    (run_dir / "out" / "notes.txt").write_text("by hand\n", encoding="utf-8")
    _finalize(run_dir)
    second = _manifest(run_dir)
    assert sorted(p.name for p in (run_dir / "out").iterdir()) == NAMES
    assert [f["sha256"] for f in first["files"][1:]] == [
        f["sha256"] for f in second["files"][1:]
    ]
    for item in second["files"]:
        assert item["sha256"] == _sha(run_dir / item["path"])
    assert _status(run_dir) == "finalized"


def test_leftover_work_folders_are_removed(run_dir: Path) -> None:
    for name in (".finalize-new", ".finalize-old"):
        (run_dir / name).mkdir()
        (run_dir / name / "old.pdf").write_bytes(b"%PDF-old")
    _finalize(run_dir)
    assert not (run_dir / ".finalize-new").exists()
    assert not (run_dir / ".finalize-old").exists()
    assert sorted(p.name for p in (run_dir / "out").iterdir()) == NAMES


def test_the_order_of_the_writes(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _finalize(run_dir)
    events: list[str] = []
    rename, replace = os.rename, os.replace

    def on_rename(src: Any, dst: Any) -> None:
        src, dst = Path(src), Path(dst)
        if dst == run_dir / "out":
            # The old manifest is gone before the new files are in out/.
            assert not (run_dir / "manifest.json").exists()
        events.append(f"rename {src.name} {dst.name}")
        rename(src, dst)

    def on_replace(src: Any, dst: Any) -> None:
        dst = Path(dst)
        if dst.name == "manifest.json":
            # The new files are in out/ before the manifest is written.
            assert sorted(p.name for p in (run_dir / "out").iterdir()) == NAMES
            assert not (run_dir / ".finalize-new").exists()
            assert not (run_dir / ".finalize-old").exists()
        if dst.name == "run.yaml":
            assert (run_dir / "manifest.json").exists()
        events.append(f"replace {dst.name}")
        replace(src, dst)

    monkeypatch.setattr(os, "rename", on_rename)
    monkeypatch.setattr(os, "replace", on_replace)
    _finalize(run_dir)
    assert events == [
        "rename out .finalize-old",
        "rename .finalize-new out",
        "replace manifest.json",
        "replace run.yaml",
    ]


# --- Refusals: the run folder does not change ---------------------------------


def _refused(
    run: Path, error: type[Exception] = Refused, match: str | None = None, **kwargs: Any
) -> pytest.ExceptionInfo[Exception]:
    before = _tree(run)
    status = _status(run)
    with pytest.raises(error, match=match) as info:
        _finalize(run, **kwargs)
    assert _tree(run) == before
    assert _status(run) == status
    return info


def _change_tailored(run: Path) -> None:
    data = json.loads((run / "tailored.json").read_text(encoding="utf-8"))
    data["summary"] = "A summary that the owner did not see."
    (run / "tailored.json").write_text(json.dumps(data), encoding="utf-8")


def _change_letter(run: Path) -> None:
    with (run / "cover_letter.md").open("a", encoding="utf-8") as f:
        f.write("\nOne more line.\n")


def _remove_signature(run: Path) -> None:
    (run / "approval.json.sig").unlink()


def _submitted(run: Path) -> None:
    text = (run / "run.yaml").read_text(encoding="utf-8")
    text = text.replace("status: new", "status: submitted")
    (run / "run.yaml").write_text(text, "utf-8")


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (_change_tailored, "content changed"),
        (_change_letter, "content changed"),
        (_remove_signature, "not approved"),
        (_submitted, "status 'submitted'"),
    ],
)
def test_a_refusal_after_the_approval(
    run_dir: Path, change: Callable[[Path], None], match: str
) -> None:
    change(run_dir)
    _refused(run_dir, match=match)


def test_a_refusal_keeps_an_older_out_folder(run_dir: Path) -> None:
    _finalize(run_dir)
    _change_tailored(run_dir)
    _refused(run_dir, match="not approved")
    assert (run_dir / "manifest.json").exists()


def test_no_approval(tmp_path: Path) -> None:
    run = _make_run(tmp_path)
    _refused(run, match="not approved")


def test_a_hand_written_record_with_no_signature(tmp_path: Path) -> None:
    run = _make_run(tmp_path)
    loaded = load_run(run)
    digest = content_hash(loaded.tailored, loaded.letter)
    data = record_bytes("run", run_target(run), DAY, digest, "owner")
    (run / "approval.json").write_bytes(data)
    _refused(run, match="not approved")


def test_a_signature_from_a_key_not_in_allowed_signers(
    run_dir: Path, tmp_path: Path
) -> None:
    other = make_key(tmp_path / "other-keys")
    record = run_dir / "approval.json"
    sign_bytes(other, record, record.read_bytes())
    _refused(run_dir, match="not approved")


def test_a_valid_record_of_another_run(run_dir: Path) -> None:
    other = run_dir.with_name("2026-11-17-acme-ux-designer")
    shutil.copytree(run_dir, other)
    _refused(other, match="not approved")


def test_a_check_error(run_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    error = Finding("R-1", "error", "personalInfo.email", "synthetic error")

    def check(*_args: object) -> list[Finding]:
        return [error]

    monkeypatch.setattr(finalize_module, "check", check)
    _refused(run_dir, match="facts check has 1 errors")


def test_an_unreviewable_part(run_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:

    def unreviewable(_resume: object) -> list[str]:
        return ["extra"]

    monkeypatch.setattr(finalize_module, "unreviewable", unreviewable)
    _refused(run_dir, match="cannot show")


def _drop_bullet(resume: Resume) -> None:
    resume["workExperience"][0]["description"].pop()


def test_a_round_trip_finding_keeps_the_old_state(run_dir: Path) -> None:
    _finalize(run_dir)
    info = _refused(run_dir, RoundTripFailed, cli=PdfCli(change=_drop_bullet))
    assert isinstance(info.value, RoundTripFailed)
    last = len(load_run(run_dir).tailored["workExperience"][0]["description"]) - 1
    assert [f.location for f in info.value.findings] == [
        f"workExperience[0].description[{last}]"
    ]


def test_a_renderer_failure_keeps_the_old_state(run_dir: Path) -> None:
    _finalize(run_dir)
    cli = PdfCli(fail={"export_resume_pdf": "no browser"})
    _refused(run_dir, RenderError, match="no browser", cli=cli)


def test_libreoffice_absent(run_dir: Path) -> None:
    _finalize(run_dir)
    cli = PdfCli()
    _refused(run_dir, match="LibreOffice", cli=cli, office=False)
    assert cli.calls == []


def test_a_page_count_failure_keeps_the_old_state(run_dir: Path) -> None:
    _finalize(run_dir)

    def count(_path: Path) -> int:
        raise InputError("LibreOffice did not start.")

    _refused(run_dir, match="cannot count", count=count)


def test_a_held_lock(run_dir: Path) -> None:
    fd = os.open(lock_path(run_dir), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _refused(run_dir, match="another finalize")
    finally:
        os.close(fd)
    assert lock_path(run_dir).parent == Path(tempfile.gettempdir())


@pytest.mark.parametrize("name", NO_LINKS)
def test_a_symbolic_link_is_refused(run_dir: Path, tmp_path: Path, name: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = run_dir / name
    if target.exists():
        shutil.move(target, outside / name)
    target.symlink_to(outside / name)
    before = _tree(outside)
    _refused(run_dir, InputError, match="symbolic link")
    assert _tree(outside) == before


# --- The command --------------------------------------------------------------


@pytest.fixture
def fake_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PdfCli:
    cli = PdfCli()
    folder = tmp_path / "fake-rm"

    def from_settings(_repo: Path, _render: object) -> ResumeMatcher:
        return fake_renderer(Path(tempfile.mkdtemp(dir=folder)), cli)

    def word_pages(_path: Path) -> int:
        return 3

    monkeypatch.setattr(resume_matcher, "from_settings", from_settings)
    folder.mkdir()
    monkeypatch.setattr("darzi.cli.word_pages", word_pages)
    monkeypatch.setattr("darzi.cli.office_present", lambda: True)
    return cli


def _cli(run: Path, *extra: str) -> int:
    return main(["finalize", str(run), "--repo", str(run.parents[1]), *extra])


def test_the_command_json(
    run_dir: Path, fake_tools: PdfCli, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(run_dir, "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["command"] == "finalize"
    assert data["manifest"] == _manifest(run_dir)
    assert [f["pages"] for f in data["manifest"]["files"]] == [1, 3, 3]
    assert data["warnings"] == [
        "R-18: the Word r\u00e9sum\u00e9 has 3 pages; max_pages of the selection is 2."
    ]


def test_the_command_text(
    run_dir: Path, fake_tools: PdfCli, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(run_dir) == 0
    out = capsys.readouterr().out
    assert "out/Test_Person_Resume.pdf: 1 pages" in out
    assert "Status: finalized." in out


def test_the_command_exit_code_1(
    tmp_path: Path, fake_tools: PdfCli, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _make_run(tmp_path)
    assert _cli(run) == 1
    assert "is not approved" in capsys.readouterr().err


def test_the_command_round_trip_finding(
    run_dir: Path, fake_tools: PdfCli, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_tools.change = _drop_bullet
    assert _cli(run_dir, "--json") == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["errors"] == 1 and report["command"] == "finalize"
    assert "round-trip check" in captured.err
    assert not (run_dir / "out").exists()
    assert not (run_dir / "manifest.json").exists()


def test_the_command_exit_code_2(
    tmp_path: Path, fake_tools: PdfCli, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _make_run(tmp_path)
    (run / "run.yaml").unlink()
    assert _cli(run) == 2
    assert "run.yaml" in capsys.readouterr().err


def test_there_is_no_handover_option(run_dir: Path) -> None:
    before = _tree(run_dir)
    with pytest.raises(SystemExit) as info:
        _cli(run_dir, "--handover")
    assert info.value.code == 2
    assert _tree(run_dir) == before


def test_finalize_has_no_parameter_that_skips_the_approval() -> None:
    import inspect

    names = list(inspect.signature(finalize).parameters)
    assert names == [
        "repo",
        "run_dir",
        "renderer_for",
        "count_pages",
        "office_ready",
        "today",
    ]


# --- The commit of the Resume Matcher clone ----------------------------------


def _git(folder: Path, head: str) -> Path:
    git = folder / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text(head, encoding="utf-8")
    return git


def test_clone_commit_detached(tmp_path: Path) -> None:
    _git(tmp_path, "c" * 40 + "\n")
    assert clone_commit(tmp_path) == "c" * 40


def test_clone_commit_packed_refs(tmp_path: Path) -> None:
    git = _git(tmp_path, "ref: refs/heads/main\n")
    (git / "packed-refs").write_text(
        "# pack-refs with: peeled\n" + "d" * 40 + " refs/heads/main\n", "utf-8"
    )
    assert clone_commit(tmp_path) == "d" * 40


def test_clone_commit_of_a_worktree(tmp_path: Path) -> None:
    main_git = _git(tmp_path / "main", "ref: refs/heads/main\n")
    (main_git / "refs" / "heads").mkdir(parents=True)
    (main_git / "refs" / "heads" / "topic").write_text("e" * 40 + "\n", "utf-8")
    worktree = main_git / "worktrees" / "wt"
    worktree.mkdir(parents=True)
    (worktree / "HEAD").write_text("ref: refs/heads/topic\n", "utf-8")
    (worktree / "commondir").write_text("../..\n", "utf-8")
    clone = tmp_path / "wt"
    clone.mkdir()
    (clone / ".git").write_text(f"gitdir: {worktree}\n", "utf-8")
    assert clone_commit(clone) == "e" * 40


@pytest.mark.parametrize(
    "head", ["ref: refs/heads/none\n", "not a ref\n", "ref: refs/../../x\n", ""]
)
def test_clone_commit_gives_none(tmp_path: Path, head: str) -> None:
    _git(tmp_path, head)
    assert clone_commit(tmp_path) is None
    assert clone_commit(tmp_path / "absent") is None
