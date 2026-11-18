"""`facts finalize RUN --handover`, the `signed` field and `[handover] copy_to` (W-13).

Synthetic data only. The fakes of the W-8 tests: a fake renderer CLI, a fake
page counter, the real Word builder and the real round-trip check. A signed
run is approved with a key that the test makes in a temporary folder. Each
refusal test checks that the run folder and the copy folder did not change.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from darzi.cli import main
from darzi.facts.check import load_run
from darzi.facts.repo import InputError, load_rules
from darzi.render.resume_matcher import ResumeMatcher
from darzi.runs import finalize as finalize_module
from darzi.runs.finalize import (
    CopyFailed,
    Finalized,
    Refused,
    finalize,
    finalize_handover,
    handover_scan,
)
from darzi.runs.runs import content_hash

from .render_fakes import fake_install
from .signing_root import approve
from .test_finalize import (  # pyright: ignore[reportPrivateUsage]
    DAY,
    MANIFEST_KEYS,
    NAMES,
    RUN,
    PdfCli,
    _finalize,  # pyright: ignore[reportPrivateUsage]
    _fingerprint,  # pyright: ignore[reportPrivateUsage]
    _make_run,  # pyright: ignore[reportPrivateUsage]
    _sha,  # pyright: ignore[reportPrivateUsage]
    _status,  # pyright: ignore[reportPrivateUsage]
    _tree,  # pyright: ignore[reportPrivateUsage]
)

SRC = Path(__file__).resolve().parents[1] / "src" / "darzi"
HANDOVER_KEYS = [
    k for k in MANIFEST_KEYS if k not in ("approval_hash", "signer_fingerprint")
]
APPLY = "https://careers.example.com/apply/1"
# Values that the R-1 and PV-3 rules of tests/data/rules.yaml flag. They are
# built in parts, so that the privacy gate of the repo does not read them as
# real contact data.
PRIVATE = {
    "email": "someone" + "@" + "example.org",
    "phone": "555" + "-010-" + "0142",
    "term": "Zorbek",
}
SCAN_FIELDS = [
    ("workExperience", "website"),
    ("workExperience", "github"),
    ("personalProjects", "website"),
    ("personalProjects", "github"),
    ("personalProjects", "location"),
    ("education", "location"),
    ("sectionMeta", "displayName"),
]

Json = dict[str, Any]


def _handover(
    run: Path,
    cli: PdfCli | None = None,
    *,
    office: bool = True,
    repo: Path | None = None,
) -> Finalized:
    root = repo or run.parents[1]
    install = fake_install(Path(tempfile.mkdtemp(dir=root.parent)))
    renderer = ResumeMatcher(install, cli or PdfCli())
    return finalize_handover(
        root,
        run,
        renderer_for=lambda _repo, _render: renderer,
        count_pages=lambda _path: 1,
        office_ready=lambda: office,
        today=DAY,
    )


def _manifest(run: Path) -> Json:
    return json.loads((run / "manifest.json").read_text(encoding="utf-8"))


def _set_copy_to(root: Path, value: str) -> None:
    toml = root / "facts.toml"
    text = toml.read_text(encoding="utf-8")
    assert text.count('copy_to = ""') == 1
    toml.write_text(text.replace('copy_to = ""', f"copy_to = {json.dumps(value)}"))


def _edit_tailored(run: Path, change: Callable[[Json], None]) -> None:
    path = run / "tailored.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _refused(run: Path, match: str, *, copy: Path | None = None) -> None:
    """Assert that the hand-over refuses with exit code 1 and changes nothing."""
    before = _tree(run)
    copy_before = _tree(copy) if copy is not None else None
    with pytest.raises(Refused, match=match):
        _handover(run)
    assert _tree(run) == before
    if copy is not None:
        assert _tree(copy) == copy_before


def _invalid(run: Path, match: str, copy: Path) -> None:
    """Assert that the hand-over refuses with exit code 2 and changes nothing."""
    before, copy_before = _tree(run), _tree(copy)
    with pytest.raises(InputError, match=match) as info:
        _handover(run)
    assert not isinstance(info.value, Refused)
    assert _tree(run) == before
    assert _tree(copy) == copy_before


@pytest.fixture
def new_run(tmp_path: Path) -> Path:
    """A run folder with no approval and a cover letter."""
    return _make_run(tmp_path)


@pytest.fixture
def approved_run(tmp_path: Path) -> Path:
    """An approved run folder with a cover letter, in its own data root."""
    run = _make_run(tmp_path / "approved")
    assert approve(run.parents[1], run) == 0
    return run


@pytest.fixture
def phone(tmp_path: Path) -> Path:
    """A copy_to folder outside the data root."""
    folder = tmp_path / "phone"
    folder.mkdir()
    return folder


# --- The hand-over files and the unsigned manifest -------------------------------


def test_handover_writes_three_files_and_an_unsigned_manifest(new_run: Path) -> None:
    assert not (new_run / "approval.json").exists()
    done = _handover(new_run)
    manifest = _manifest(new_run)
    assert done.manifest == manifest and done.copied_to is None
    assert manifest["signed"] is False
    assert sorted(p.name for p in (new_run / "out").iterdir()) == NAMES
    for item in manifest["files"]:
        assert _sha(new_run / item["path"]) == item["sha256"]
    assert _status(new_run) == "finalized"
    assert not (new_run / "approval.json").exists()


def test_handover_manifest_has_no_approval_hash_and_no_signer(new_run: Path) -> None:
    _handover(new_run)
    assert list(_manifest(new_run)) == HANDOVER_KEYS


def test_handover_on_an_approved_run_still_writes_signed_false(
    approved_run: Path,
) -> None:
    _handover(approved_run)
    manifest = _manifest(approved_run)
    assert manifest["signed"] is False
    assert list(manifest) == HANDOVER_KEYS


def test_signed_finalize_always_writes_signed_true(approved_run: Path) -> None:
    _finalize(approved_run)
    manifest = _manifest(approved_run)
    assert list(manifest) == MANIFEST_KEYS
    assert manifest["signed"] is True
    run = load_run(approved_run)
    assert manifest["approval_hash"] == content_hash(run.tailored, run.letter)
    assert manifest["signer_fingerprint"] == _fingerprint(approved_run)


def test_finalize_handover_has_no_parameter_that_writes_signed_true() -> None:
    names = ["repo", "run_dir", "renderer_for", "count_pages", "office_ready", "today"]
    assert list(inspect.signature(finalize_handover).parameters) == names
    assert list(inspect.signature(finalize).parameters) == names
    build = inspect.signature(finalize_module._build_and_commit)  # pyright: ignore[reportPrivateUsage]
    assert build.parameters["signing"].default is inspect.Parameter.empty


@pytest.mark.parametrize(
    "signing",
    [
        {},
        {"signed": True},
        {"signed": 1},
        {"signed": None},
        {"signed": False, "approval_hash": "a"},
        {"signed": False, "approval_hash": "a", "signer_fingerprint": "b"},
        {"signed": True, "approval_hash": "", "signer_fingerprint": "b"},
        {"signed": True, "approval_hash": "a", "signer_fingerprint": None},
    ],
)
def test_the_build_refuses_approval_fields_of_no_gate(signing: Json) -> None:
    check = finalize_module._check_signing  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ValueError, match="not the approval fields"):
        check(signing)


def test_no_environment_variable_reaches_the_gates() -> None:
    reads = re.compile(r"\b(environ|environb|getenv|getenvb)\b")
    found = {
        str(path.relative_to(SRC)): len(reads.findall(path.read_text("utf-8")))
        for path in SRC.rglob("*.py")
        if reads.search(path.read_text("utf-8"))
    }
    # Only the copy of the environment for the ssh-keygen call (spec section 7).
    assert found == {"runs/signing.py": 1}
    text = (SRC / "runs" / "signing.py").read_text("utf-8")
    assert "env = dict(os.environ)" in text


def test_the_flag_is_the_only_caller_of_the_hand_over_gate() -> None:
    callers = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if "finalize_handover" in path.read_text("utf-8")
    ]
    assert sorted(callers) == ["cli.py", "runs/finalize.py"]
    cli = (SRC / "cli.py").read_text("utf-8")
    assert "gate = finalize_handover if handover is True else finalize" in cli


# --- Signing later, and the reverse ----------------------------------------------


def test_signed_finalize_replaces_a_handover_result(new_run: Path) -> None:
    _handover(new_run)
    assert _manifest(new_run)["signed"] is False
    assert approve(new_run.parents[1], new_run) == 0
    _finalize(new_run)
    manifest = _manifest(new_run)
    assert manifest["signed"] is True and list(manifest) == MANIFEST_KEYS
    assert manifest["signer_fingerprint"] == _fingerprint(new_run)
    for item in manifest["files"]:
        assert _sha(new_run / item["path"]) == item["sha256"]
    assert _status(new_run) == "finalized"


def test_a_second_handover_replaces_a_handover_result(new_run: Path) -> None:
    _handover(new_run)
    _handover(new_run)
    assert _manifest(new_run)["signed"] is False


def test_handover_refuses_a_run_with_a_signed_manifest(
    approved_run: Path, phone: Path
) -> None:
    _finalize(approved_run)
    _set_copy_to(approved_run.parents[1], str(phone))
    _refused(approved_run, "A hand-over never replaces a signed result", copy=phone)


@pytest.mark.parametrize(
    "text",
    [b"{not json", b"[]", b"{}", b'{"signed": "false"}', b'{"signed": 0}', b"\xff"],
)
def test_handover_refuses_a_manifest_that_it_cannot_read(
    new_run: Path, text: bytes
) -> None:
    (new_run / "manifest.json").write_bytes(text)
    _refused(new_run, "manifest.json")


def test_handover_refuses_a_submitted_run(new_run: Path) -> None:
    _handover(new_run)
    meta = yaml.safe_load((new_run / "run.yaml").read_text(encoding="utf-8"))
    meta["status"] = "submitted"
    (new_run / "run.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    _refused(new_run, "status 'submitted'")


def test_handover_refuses_check_errors(new_run: Path) -> None:
    _edit_tailored(new_run, lambda d: d["personalInfo"].update(email=""))
    _refused(new_run, "facts check has")


def test_handover_refuses_an_unreviewable_part(new_run: Path) -> None:
    _edit_tailored(new_run, lambda d: d.update(extra="text"))
    _refused(new_run, "parts that the checks cannot read: top-level key 'extra'")


def test_handover_refuses_a_hidden_section(new_run: Path) -> None:
    _edit_tailored(new_run, lambda d: d["sectionMeta"][0].update(isVisible=False))
    _refused(new_run, "is hidden")


def test_handover_refuses_without_libreoffice(new_run: Path) -> None:
    before = _tree(new_run)
    with pytest.raises(Refused, match="LibreOffice"):
        _handover(new_run, office=False)
    assert _tree(new_run) == before


# --- The scan of the fields that facts check does not read ------------------------


def _put(data: Json, section: str, key: str, value: str) -> None:
    entries = data[section]
    assert entries, f"the test data has no {section}"
    entries[0][key] = value


@pytest.mark.parametrize(("section", "key"), SCAN_FIELDS)
@pytest.mark.parametrize(
    ("kind", "rule"), [("email", "R-1"), ("phone", "R-1"), ("term", "PV-3")]
)
def test_the_scan_finds_private_data(
    new_run: Path, phone: Path, section: str, key: str, kind: str, rule: str
) -> None:
    value = f"see {PRIVATE[kind]} here"
    _edit_tailored(new_run, lambda d: _put(d, section, key, value))
    _set_copy_to(new_run.parents[1], str(phone))
    _refused(new_run, rf"{rule} at {re.escape(section)}\[0\]\.{key}", copy=phone)


def test_facts_check_alone_does_not_read_these_fields(new_run: Path) -> None:
    # The reason for the scan: the same values pass facts check.
    for section, key in SCAN_FIELDS:
        _edit_tailored(
            new_run, lambda d, s=section, k=key: _put(d, s, k, PRIVATE["term"])
        )
    assert main(["check", str(new_run), "--repo", str(new_run.parents[1])]) == 0
    rules = load_rules(new_run.parents[1])
    found = handover_scan(load_run(new_run).tailored, rules)
    assert {f.location for f in found} == {f"{s}[0].{k}" for s, k in SCAN_FIELDS}
    assert {f.rule for f in found} == {"PV-3"}


def test_the_scan_passes_clean_fields(new_run: Path) -> None:
    rules = load_rules(new_run.parents[1])
    assert handover_scan(load_run(new_run).tailored, rules) == []


def test_the_scan_ignores_the_bullet_exemption(new_run: Path) -> None:
    rules = load_rules(new_run.parents[1])
    rules["privacy_patterns"]["allowed_bullet_ids"] = ["0"]
    tailored = load_run(new_run).tailored
    tailored["education"][0]["location"] = PRIVATE["term"]
    assert [f.rule for f in handover_scan(tailored, rules)] == ["PV-3"]


# --- [handover] copy_to ------------------------------------------------------------


def test_no_copy_when_copy_to_is_not_set(new_run: Path, tmp_path: Path) -> None:
    done = _handover(new_run)
    assert done.copied_to is None


def test_the_copy_holds_only_the_manifest_files(new_run: Path, phone: Path) -> None:
    _set_copy_to(new_run.parents[1], str(phone))
    done = _handover(new_run)
    target = phone / RUN
    assert done.copied_to == target
    assert sorted(p.name for p in phone.iterdir()) == [RUN]
    assert sorted(p.name for p in target.iterdir()) == NAMES
    for item in _manifest(new_run)["files"]:
        copied = target / Path(item["path"]).name
        assert _sha(copied) == item["sha256"]
        assert not copied.is_symlink()


def test_a_relative_copy_to_is_relative_to_the_data_root(new_run: Path) -> None:
    root = new_run.parents[1]
    folder = root.parent / "relative-phone"
    folder.mkdir()
    _set_copy_to(root, "../relative-phone")
    assert _handover(new_run).copied_to == root / "../relative-phone" / RUN
    assert sorted(p.name for p in (folder / RUN).iterdir()) == NAMES


def test_a_tilde_is_the_home_folder_of_the_user_database(
    new_run: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, other = tmp_path / "db-home", tmp_path / "env-home"
    (home / "Sync").mkdir(parents=True)
    (other / "Sync").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(other))
    user = SimpleNamespace(pw_dir=str(home))

    def getpwuid(_uid: int) -> SimpleNamespace:
        return user

    monkeypatch.setattr(finalize_module.pwd, "getpwuid", getpwuid)
    _set_copy_to(new_run.parents[1], "~/Sync")
    assert _handover(new_run).copied_to == home / "Sync" / RUN
    assert list((other / "Sync").iterdir()) == []


def test_a_tilde_with_a_user_name_is_invalid(new_run: Path, phone: Path) -> None:
    _set_copy_to(new_run.parents[1], "~other/Sync")
    _invalid(new_run, "only ~/ for your own home folder", phone)


def test_a_copy_to_that_does_not_exist_is_invalid(new_run: Path, phone: Path) -> None:
    _set_copy_to(new_run.parents[1], str(phone / "absent"))
    _invalid(new_run, "does not exist", phone)
    assert not (phone / "absent").exists()


def test_a_copy_to_that_is_a_file_is_invalid(new_run: Path, phone: Path) -> None:
    (phone / "file").write_text("x", encoding="utf-8")
    _set_copy_to(new_run.parents[1], str(phone / "file"))
    _invalid(new_run, "is not a folder", phone)


def test_a_copy_to_that_is_not_writable_is_invalid(new_run: Path, phone: Path) -> None:
    folder = phone / "locked"
    folder.mkdir()
    folder.chmod(0o500)
    try:
        if os.access(folder, os.W_OK):
            pytest.skip("this user can write to a read-only folder")
        _set_copy_to(new_run.parents[1], str(folder))
        _invalid(new_run, "is not writable", phone)
    finally:
        folder.chmod(0o700)


@pytest.mark.parametrize("where", [".", "applications", "out-of-run", "link"])
def test_a_copy_to_inside_the_data_root_is_invalid(
    new_run: Path, phone: Path, where: str
) -> None:
    root = new_run.parents[1]
    if where == "out-of-run":
        (root / "phone").mkdir()
        value = "phone"
    elif where == "link":
        # A link outside the data root whose real path is inside it.
        (phone / "inside").symlink_to(root / "applications")
        value = str(phone / "inside")
    else:
        value = where
    _set_copy_to(root, value)
    _invalid(new_run, "is inside the data root", phone)


def test_a_copy_to_that_holds_the_data_root_is_invalid(new_run: Path) -> None:
    root = new_run.parents[1]
    _set_copy_to(root, "..")
    before = _tree(new_run)
    with pytest.raises(InputError, match="data root .* is inside the copy_to"):
        _handover(new_run)
    assert _tree(new_run) == before


def test_copy_to_may_be_a_link_to_a_folder(new_run: Path, phone: Path) -> None:
    link = phone.parent / "phone-link"
    link.symlink_to(phone)
    _set_copy_to(new_run.parents[1], str(link))
    _handover(new_run)
    assert sorted(p.name for p in (phone / RUN).iterdir()) == NAMES


def test_a_run_sub_folder_that_is_a_link_is_invalid(
    new_run: Path, phone: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (phone / RUN).symlink_to(elsewhere)
    _set_copy_to(new_run.parents[1], str(phone))
    _invalid(new_run, "is a symbolic link", phone)
    assert list(elsewhere.iterdir()) == []


def test_a_copied_file_that_is_a_link_is_invalid(
    new_run: Path, phone: Path, tmp_path: Path
) -> None:
    (phone / RUN).mkdir()
    target = tmp_path / "target.pdf"
    target.write_bytes(b"old")
    (phone / RUN / NAMES[2]).symlink_to(target)
    _set_copy_to(new_run.parents[1], str(phone))
    _invalid(new_run, "is a symbolic link or a folder", phone)
    assert target.read_bytes() == b"old"


def test_a_file_of_the_same_name_is_replaced_and_others_stay(
    new_run: Path, phone: Path
) -> None:
    (phone / RUN).mkdir()
    (phone / RUN / NAMES[2]).write_bytes(b"old")
    (phone / RUN / "notes.txt").write_bytes(b"mine")
    _set_copy_to(new_run.parents[1], str(phone))
    _handover(new_run)
    pdf = next(f for f in _manifest(new_run)["files"] if f["kind"] == "resume_pdf")
    assert _sha(phone / RUN / NAMES[2]) == pdf["sha256"]
    assert (phone / RUN / "notes.txt").read_bytes() == b"mine"


def test_a_copy_that_fails_after_the_commit(
    new_run: Path, phone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_copy_to(new_run.parents[1], str(phone))
    real = finalize_module.write_atomic

    def write(path: Path, data: bytes) -> None:
        if phone in path.parents:
            raise OSError("synthetic fault")
        real(path, data)

    monkeypatch.setattr(finalize_module, "write_atomic", write)
    with pytest.raises(CopyFailed) as info:
        _handover(new_run)
    message = str(info.value)
    assert "the run is finalized" in message and "synthetic fault" in message
    assert f"{new_run / 'out'}/" in message
    assert _status(new_run) == "finalized"
    assert _manifest(new_run)["signed"] is False
    assert sorted(p.name for p in (new_run / "out").iterdir()) == NAMES


def test_a_copied_file_with_a_changed_hash_is_not_copied(
    new_run: Path, phone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_copy_to(new_run.parents[1], str(phone))
    real = finalize_module._commit  # pyright: ignore[reportPrivateUsage]

    def commit(run_dir: Path, *args: Any) -> None:
        real(run_dir, *args)
        (run_dir / "out" / NAMES[2]).write_bytes(b"changed")

    monkeypatch.setattr(finalize_module, "_commit", commit)
    with pytest.raises(CopyFailed, match="does not have the manifest hash"):
        _handover(new_run)
    assert not (phone / RUN / NAMES[2]).exists()


def test_signed_finalize_makes_no_copy(approved_run: Path, phone: Path) -> None:
    _set_copy_to(approved_run.parents[1], str(phone))
    done = _finalize(approved_run)
    assert done.copied_to is None
    assert list(phone.iterdir()) == []


# --- The command ------------------------------------------------------------------


def _cli(run: Path, *extra: str) -> int:
    return main(["finalize", str(run), "--repo", str(run.parents[1]), *extra])


@pytest.fixture
def fake_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PdfCli:
    cli = PdfCli()
    folder = tmp_path / "fake-rm"
    folder.mkdir()

    def from_settings(_repo: Path, _render: object) -> ResumeMatcher:
        return ResumeMatcher(fake_install(Path(tempfile.mkdtemp(dir=folder))), cli)

    monkeypatch.setattr("darzi.render.resume_matcher.from_settings", from_settings)

    def word_pages(_path: Path) -> int:
        return 1

    monkeypatch.setattr("darzi.cli.word_pages", word_pages)
    monkeypatch.setattr("darzi.cli.office_present", lambda: True)
    return cli


def test_the_command_text(
    new_run: Path, phone: Path, fake_tools: PdfCli, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_copy_to(new_run.parents[1], str(phone))
    assert _cli(new_run, "--handover") == 0
    out = capsys.readouterr().out
    assert "out/Test_Person_Resume.pdf: 1 pages" in out
    assert "(NOT signed: hand-over files for the owner to use by hand" in out
    assert "fill-plan refuses them). Status: finalized." in out
    assert f"copied the files to {phone / RUN}" in out


def test_the_command_json(
    new_run: Path, fake_tools: PdfCli, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(new_run, "--handover", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["manifest"] == _manifest(new_run)
    assert data["manifest"]["signed"] is False
    assert data["copied_to"] is None


def test_without_the_flag_a_new_run_is_not_approved(
    new_run: Path, fake_tools: PdfCli, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _tree(new_run)
    assert _cli(new_run) == 1
    assert "is not approved" in capsys.readouterr().err
    assert _tree(new_run) == before


def test_the_command_exit_codes(
    approved_run: Path,
    phone: Path,
    fake_tools: PdfCli,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_copy_to(approved_run.parents[1], str(phone / "absent"))
    assert _cli(approved_run, "--handover") == 2
    assert "does not exist" in capsys.readouterr().err
    _set_copy_to_back(approved_run.parents[1], str(phone / "absent"))
    assert _cli(approved_run) == 0
    capsys.readouterr()
    assert _cli(approved_run, "--handover") == 1
    err = capsys.readouterr().err
    assert "A hand-over never replaces a signed result" in err
    assert "Traceback" not in err


def _set_copy_to_back(root: Path, value: str) -> None:
    toml = root / "facts.toml"
    text = toml.read_text(encoding="utf-8")
    toml.write_text(text.replace(f"copy_to = {json.dumps(value)}", 'copy_to = ""'))


def test_the_command_copy_failure(
    new_run: Path,
    phone: Path,
    fake_tools: PdfCli,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_copy_to(new_run.parents[1], str(phone))

    def copy_files(*_args: object) -> None:
        raise OSError("synthetic fault")

    monkeypatch.setattr(finalize_module, "_copy_files", copy_files)
    assert _cli(new_run, "--handover") == 1
    err = capsys.readouterr().err
    assert "the run is finalized" in err and "synthetic fault" in err
    assert _status(new_run) == "finalized"


# --- fill-plan and submitted on a real hand-over run --------------------------------


def _with_apply_url(run: Path) -> None:
    with (run / "run.yaml").open("a", encoding="utf-8") as f:
        f.write(f"apply_url: {APPLY}\n")


def _fill_plan_refuses(
    run: Path, capsys: pytest.CaptureFixture[str], match: str, *extra: str
) -> None:
    before = _tree(run)
    code = main(["fill-plan", str(run), "--repo", str(run.parents[1]), *extra])
    out, err = capsys.readouterr()
    assert code == 1, err
    assert out == ""
    assert "Traceback" not in err
    assert match in err, err
    assert _tree(run) == before


@pytest.mark.parametrize("extra", [(), ("--json",)])
def test_fill_plan_refuses_a_real_handover_manifest(
    approved_run: Path, capsys: pytest.CaptureFixture[str], extra: tuple[str, ...]
) -> None:
    # Spec section 7, the fifth negative test, on a real hand-over result. The
    # run is approved, so the refusal comes from signed: false.
    _with_apply_url(approved_run)
    _handover(approved_run)
    capsys.readouterr()
    _fill_plan_refuses(approved_run, capsys, "hand-over manifest", *extra)


def test_fill_plan_refuses_an_unapproved_handover_run(
    new_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_apply_url(new_run)
    _handover(new_run)
    _fill_plan_refuses(new_run, capsys, "is not approved")


def _edit_to_signed_true(run: Path) -> None:
    data = _manifest(run)
    data["signed"] = True
    (run / "manifest.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_fill_plan_refuses_a_handover_manifest_edited_to_signed_true(
    new_run: Path, approved_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No approval: the approval check refuses first.
    _with_apply_url(new_run)
    _handover(new_run)
    _edit_to_signed_true(new_run)
    _fill_plan_refuses(new_run, capsys, "is not approved")
    # A valid approval: the manifest lacks approval_hash and signer_fingerprint.
    _with_apply_url(approved_run)
    _handover(approved_run)
    _edit_to_signed_true(approved_run)
    _fill_plan_refuses(
        approved_run, capsys, "does not have the keys that finalize writes"
    )


def test_submitted_accepts_a_real_handover_run(
    new_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _handover(new_run)
    code = main(["submitted", str(new_run), "--repo", str(new_run.parents[1])])
    assert code == 0, capsys.readouterr().err
    assert _status(new_run) == "submitted"
    _refused(new_run, "status 'submitted'")


# --- Plain run paths and the stricter manifest test (F-4) -------------------------


@pytest.fixture
def sync(tmp_path: Path) -> Path:
    """The parent folder of the copy_to folder `phone`; it holds nothing else."""
    folder = tmp_path / "sync"
    (folder / "phone").mkdir(parents=True)
    return folder


def _path_refused(run: Path, path: Path, sync: Path, *, signed: bool) -> None:
    """Assert exit code 2 for `path`, and no change in the data root or `sync`."""
    root = run.parents[1]
    before, sync_before = _tree(root), _tree(sync)
    with pytest.raises(InputError, match="is not a plain run path") as info:
        if signed:
            _finalize(path, repo=root)
        else:
            _handover(path, repo=root)
    assert not isinstance(info.value, Refused)
    assert "applications/<run>" in str(info.value)
    assert _tree(root) == before
    assert _tree(sync) == sync_before


@pytest.mark.parametrize("case", ["notes/..", "out/..", ".. from notes"])
def test_handover_refuses_a_run_path_that_is_not_plain(
    new_run: Path, sync: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    _set_copy_to(new_run.parents[1], str(sync / "phone"))
    if case == "out/..":
        _handover(new_run)
    (new_run / "notes").mkdir()
    path = new_run / case
    if case == ".. from notes":
        monkeypatch.chdir(new_run / "notes")
        path = Path("..")
    _path_refused(new_run, path, sync, signed=False)


def test_signed_finalize_refuses_a_run_path_that_is_not_plain(
    approved_run: Path, sync: Path
) -> None:
    _finalize(approved_run)
    manifest = _manifest(approved_run)
    _path_refused(approved_run, approved_run / "out" / "..", sync, signed=True)
    assert _manifest(approved_run) == manifest
    assert sorted(p.name for p in (approved_run / "out").iterdir()) == NAMES


def test_the_command_with_a_dot_copies_into_the_run_sub_folder(
    new_run: Path,
    sync: Path,
    fake_tools: PdfCli,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `.` alone is the real run folder (choice (b) of F-4), on both paths.
    root, phone = new_run.parents[1], sync / "phone"
    _set_copy_to(root, str(phone))
    monkeypatch.chdir(new_run)
    assert main(["finalize", ".", "--repo", str(root), "--handover"]) == 0, (
        capsys.readouterr().err
    )
    assert _manifest(new_run)["run"] == RUN
    assert sorted(p.name for p in sync.iterdir()) == ["phone"]
    assert sorted(p.name for p in phone.iterdir()) == [RUN]
    assert sorted(p.name for p in (phone / RUN).iterdir()) == NAMES


@pytest.mark.parametrize("relative", [False, True])
def test_a_plain_run_path_works_on_both_paths(
    approved_run: Path, sync: Path, monkeypatch: pytest.MonkeyPatch, relative: bool
) -> None:
    root, phone = approved_run.parents[1], sync / "phone"
    _set_copy_to(root, str(phone))
    seen: list[Path] = []
    real = finalize_module._commit  # pyright: ignore[reportPrivateUsage]

    def commit(run_dir: Path, *args: Any) -> None:
        seen.append(run_dir)
        real(run_dir, *args)

    monkeypatch.setattr(finalize_module, "_commit", commit)
    path = approved_run
    if relative:
        monkeypatch.chdir(root)
        path = Path("applications") / RUN
    done = _handover(path, repo=root)
    assert done.copied_to == phone / RUN
    assert sorted(p.name for p in phone.iterdir()) == [RUN]
    assert sorted(p.name for p in (phone / RUN).iterdir()) == NAMES
    assert _manifest(approved_run)["run"] == RUN
    _finalize(path, repo=root)
    assert _manifest(approved_run)["signed"] is True
    # Each step used the one real path of the run folder.
    assert seen == [approved_run.resolve(), approved_run.resolve()]


@pytest.mark.parametrize("name", ["", ".", ".."])
def test_a_copy_sub_folder_name_that_is_not_plain_is_invalid(
    sync: Path, name: str
) -> None:
    before = _tree(sync)
    copy_target = finalize_module._copy_target  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(InputError, match="is not a plain folder name"):
        copy_target(sync / "phone", name)
    assert _tree(sync) == before


def test_a_copy_sub_folder_outside_copy_to_is_invalid(sync: Path) -> None:
    before = _tree(sync)
    copy_target = finalize_module._copy_target  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(InputError, match="not a folder directly in the copy_to"):
        copy_target(sync / "phone", "notes/..")
    assert _tree(sync) == before


def test_handover_refuses_a_run_name_that_is_not_plain_before_the_build(
    new_run: Path, sync: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_copy_to(new_run.parents[1], str(sync / "phone"))

    def run_target(_path: Path) -> str:
        return ".."

    monkeypatch.setattr(finalize_module, "run_target", run_target)
    _invalid(new_run, "is not a plain folder name", sync)


@pytest.mark.parametrize("parts", [("..",), (RUN, ".."), ("..", "phone", "..", "x")])
def test_the_copy_writes_only_into_a_sub_folder_of_copy_to(
    new_run: Path, sync: Path, parts: tuple[str, ...]
) -> None:
    _handover(new_run)
    phone = sync / "phone"
    before = _tree(sync)
    copy_files = finalize_module._copy_files  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(InputError, match="not a folder directly in the copy_to"):
        copy_files(new_run, _manifest(new_run)["files"], phone.joinpath(*parts), phone)
    assert _tree(sync) == before


def test_a_copy_outside_copy_to_after_the_commit_fails_with_exit_1(
    new_run: Path, sync: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone = sync / "phone"
    _set_copy_to(new_run.parents[1], str(phone))
    real = finalize_module._copy_files  # pyright: ignore[reportPrivateUsage]

    def copy_files(run_dir: Path, files: Any, target: Path, copy_dir: Path) -> None:
        real(run_dir, files, target / "..", copy_dir)

    monkeypatch.setattr(finalize_module, "_copy_files", copy_files)
    before = _tree(sync)
    with pytest.raises(CopyFailed, match="the run is finalized") as info:
        _handover(new_run)
    assert f"{new_run / 'out'}/" in str(info.value)
    assert _tree(sync) == before
    assert _manifest(new_run)["signed"] is False


def _signed_false_with_approval_keys(run: Path) -> None:
    _finalize(run)
    data = _manifest(run)
    data["signed"] = False
    (run / "manifest.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def _duplicate_signed(run: Path) -> None:
    _handover(run)
    path = run / "manifest.json"
    text = path.read_text(encoding="utf-8")
    assert text.count('"signed": false,') == 1
    text = text.replace('"signed": false,', '"signed": true,\n  "signed": false,')
    path.write_text(text, encoding="utf-8")


def _edited(change: Callable[[Json], object]) -> Callable[[Path], None]:
    def edit(run: Path) -> None:
        _handover(run)
        data = _manifest(run)
        change(data)
        (run / "manifest.json").write_text(json.dumps(data), encoding="utf-8")

    return edit


@pytest.mark.parametrize(
    ("case", "match"),
    [
        ("approval-keys-kept", "is a signed manifest"),
        ("duplicate-signed", "a key is in the manifest two times"),
        ("extra-key", "is not a hand-over manifest"),
        ("missing-key", "is not a hand-over manifest"),
    ],
)
def test_handover_replaces_only_a_real_handover_manifest(
    tmp_path: Path, sync: Path, case: str, match: str
) -> None:
    run = _make_run(tmp_path / case)
    if case == "approval-keys-kept":
        assert approve(run.parents[1], run) == 0
    edits: dict[str, Callable[[Path], None]] = {
        "approval-keys-kept": _signed_false_with_approval_keys,
        "duplicate-signed": _duplicate_signed,
        "extra-key": _edited(lambda d: d.update(note="x")),
        "missing-key": _edited(lambda d: d.pop("files")),
    }
    edits[case](run)
    _set_copy_to(run.parents[1], str(sync / "phone"))
    _refused(run, match, copy=sync)
