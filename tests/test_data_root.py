"""A target belongs to the data root whose key list approves it (F-2).

`in_data_root()` is the one rule: a selection is `selections/<name>.yaml` and a
run is `applications/<run>` of the same data root. `verify_approval()`, the
approve steps and `finalize` use it. The tests use two temporary data roots
with two throw-away keys, and synthetic data only.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
from pathlib import Path

import pytest

from darzi.cli import approve_steps, main
from darzi.config import load_config
from darzi.facts.check import load_run
from darzi.facts.selection import is_approved, load_selection, selection_hash
from darzi.runs.approve import run_is_approved
from darzi.runs.runs import content_hash
from darzi.runs.signing import (
    in_data_root,
    record_bytes,
    run_target,
    selection_record,
    sig_path,
    verify_approval,
)

from .conftest import DATA
from .signing_root import (
    SIGNER,
    approve,
    setup_signing,
    sign_bytes,
    signature_verifies,
    signed_copy,
)

RUN = "2026-11-17-acme-product-designer"
DAY = dt.date(2026, 11, 17)


def _tree(folder: Path) -> dict[str, str | bytes]:
    """Return each path of `folder` with its bytes; no link is followed."""
    tree: dict[str, str | bytes] = {}
    for top, folders, files in os.walk(folder):
        for name in folders + files:
            path = Path(top) / name
            key = str(path.relative_to(folder))
            if path.is_symlink():
                tree[key] = "link " + os.readlink(path)
            elif path.is_file():
                tree[key] = path.read_bytes()
            else:
                tree[key] = "folder"
    return tree


def _no_ask(_prompt: str) -> str:
    pytest.fail("the owner was asked")


def _yes(_prompt: str) -> str:
    return "y"


def _ux(root: Path) -> Path:
    return root / "selections" / "ux.yaml"


def _key(root: Path) -> Path:
    return root.parent / "keys" / "approval_key"


def _with_run(root: Path) -> Path:
    """Make the run RUN in `root` (tailored.json = base.json); return it."""
    run = root / "applications" / RUN
    assert main(["export", str(_ux(root)), "--run", str(run), "--repo", str(root)]) == 0
    shutil.copy(run / "base.json", run / "tailored.json")
    (run / "cover_letter.md").write_text("Dear team,\nI design forms.\n", "utf-8")
    return run


def _two_roots(tmp_path: Path) -> tuple[Path, Path]:
    """Return two signed data roots, each with its own throw-away key."""
    return signed_copy(tmp_path / "one"), signed_copy(tmp_path / "two")


def _sign_selection_with(key: Path, path: Path) -> Path:
    """Sign a record of the selection `path` with `key`; return the record."""
    digest = selection_hash(load_selection(path))
    record = selection_record(path)
    sign_bytes(key, record, record_bytes("selection", "ux", DAY, digest, SIGNER))
    return record


def _sign_run_with(key: Path, run: Path) -> Path:
    """Sign a record of the run `run` with `key`; return the record."""
    loaded = load_run(run)
    digest = content_hash(loaded.tailored, loaded.letter)
    record = run / "approval.json"
    sign_bytes(key, record, record_bytes("run", run_target(run), DAY, digest, SIGNER))
    return record


def _selection_verdict(repo: Path, path: Path) -> tuple[bool, str]:
    verdict = is_approved(repo, load_config(repo).approval, path, load_selection(path))
    return verdict.approved, verdict.reason


def _run_verdict(repo: Path, run: Path) -> tuple[bool, str]:
    verdict = run_is_approved(repo, load_config(repo).approval, run, load_run(run))
    return verdict.approved, verdict.reason


# --- The rule -----------------------------------------------------------------


def test_the_rule_accepts_only_a_direct_child_of_the_same_root(
    tmp_path: Path,
) -> None:
    root1, root2 = _two_roots(tmp_path)
    run = _with_run(root1)
    (root1 / "selections" / "sub").mkdir()
    shutil.copy(_ux(root1), root1 / "selections" / "sub" / "ux.yaml")
    shutil.copytree(run, root1 / "applications" / "sub" / RUN)
    (root1 / "selections" / "alias.yaml").symlink_to(_ux(root1))
    (root1 / "applications" / "alias").symlink_to(run)
    (root2 / "through").symlink_to(root1 / "selections")
    (root1 / "selections" / "ux.txt").write_text("not yaml\n", encoding="utf-8")
    assert in_data_root(root1, "selection", _ux(root1))
    assert in_data_root(root1, "run", run)
    assert not in_data_root(root2, "selection", _ux(root1))
    assert not in_data_root(root2, "run", run)
    assert not in_data_root(root1, "selection", root1 / "selections/sub/ux.yaml")
    assert not in_data_root(root1, "run", root1 / "applications" / "sub" / RUN)
    assert not in_data_root(root1, "selection", root1 / "selections/alias.yaml")
    assert not in_data_root(root1, "run", root1 / "applications" / "alias")
    assert not in_data_root(root2, "selection", root2 / "through" / "ux.yaml")
    assert not in_data_root(root1, "selection", root1 / "selections" / "ux.txt")
    assert not in_data_root(root1, "run", _ux(root1))
    assert not in_data_root(root1, "other", _ux(root1))
    with pytest.raises(OSError):
        in_data_root(root1, "run", root1 / "applications" / "missing")


# --- verify_approval() --------------------------------------------------------


def test_verify_refuses_a_selection_of_another_root(tmp_path: Path) -> None:
    root1, root2 = _two_roots(tmp_path)
    record = _sign_selection_with(_key(root2), _ux(root1))
    # The key list of root 2 accepts the signature: only the rule refuses it.
    assert signature_verifies(root2, record)
    approved, reason = _selection_verdict(root2, _ux(root1))
    assert not approved
    assert "is not a selection of the data root" in reason
    settings = load_config(root2).approval
    digest = selection_hash(load_selection(_ux(root1)))
    verdict = verify_approval(
        root2, settings, "selection", "ux", record, digest, target_path=_ux(root1)
    )
    assert not verdict.approved
    assert verdict.fingerprint is None


def test_verify_refuses_a_run_of_another_root(tmp_path: Path) -> None:
    root1, root2 = _two_roots(tmp_path)
    run = _with_run(root1)
    record = _sign_run_with(_key(root2), run)
    assert signature_verifies(root2, record)
    approved, reason = _run_verdict(root2, run)
    assert not approved
    assert "is not a run of the data root" in reason


def test_verify_refuses_a_selection_in_a_deeper_folder(tmp_path: Path) -> None:
    root1 = signed_copy(tmp_path / "one")
    sub = root1 / "selections" / "sub"
    sub.mkdir()
    for name in ("ux.yaml", "ux.approval.json", "ux.approval.json.sig"):
        shutil.copy(root1 / "selections" / name, sub / name)
    assert signature_verifies(root1, sub / "ux.approval.json")
    approved, reason = _selection_verdict(root1, sub / "ux.yaml")
    assert not approved
    assert "is not a selection of the data root" in reason


def test_verify_refuses_a_link_to_a_selection(tmp_path: Path) -> None:
    root1 = signed_copy(tmp_path / "one")
    link = root1 / "selections" / "alias.yaml"
    link.symlink_to(_ux(root1))
    assert _selection_verdict(root1, _ux(root1))[0]
    approved, reason = _selection_verdict(root1, link)
    assert not approved
    assert "is not a selection of the data root" in reason


def test_verify_refuses_a_link_to_a_run(tmp_path: Path) -> None:
    root1 = signed_copy(tmp_path / "one")
    run = _with_run(root1)
    assert approve_steps(run, root1, _yes, DAY) == 0
    assert _run_verdict(root1, run)[0]
    link = root1 / "applications" / "2026-11-17-acme-alias"
    link.symlink_to(run)
    approved, reason = _run_verdict(root1, link)
    assert not approved
    assert "is not a run of the data root" in reason


def test_verify_refuses_a_path_through_a_link_to_another_root(
    tmp_path: Path,
) -> None:
    root1, root2 = _two_roots(tmp_path)
    (root2 / "through").symlink_to(root1 / "selections")
    _sign_selection_with(_key(root2), _ux(root1))
    approved, reason = _selection_verdict(root2, root2 / "through" / "ux.yaml")
    assert not approved
    assert "is not a selection of the data root" in reason


def test_verify_accepts_a_relative_path_with_dot_dot_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root1 = signed_copy(tmp_path / "one")
    run = _with_run(root1)
    assert approve_steps(run, root1, _yes, DAY) == 0
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    selection = Path("../one/root/selections/../selections/ux.yaml")
    relative_run = Path("../one/root/applications/../applications") / RUN
    assert _selection_verdict(Path("../one/root"), selection) == (True, "")
    assert _run_verdict(Path("../one/root"), relative_run) == (True, "")


# --- facts export -------------------------------------------------------------


def test_export_refuses_a_selection_of_another_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root1, root2 = _two_roots(tmp_path)
    _sign_selection_with(_key(root2), _ux(root1))
    before = (_tree(root1), _tree(root2))
    run = root2 / "applications" / RUN
    args = ["export", str(_ux(root1)), "--run", str(run), "--repo", str(root2)]
    assert main(args) == 1
    err = capsys.readouterr().err
    assert "is not approved" in err
    assert "is not a selection of the data root" in err
    assert (_tree(root1), _tree(root2)) == before


def test_export_refuses_a_link_to_a_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root1 = signed_copy(tmp_path / "one")
    link = root1 / "selections" / "alias.yaml"
    link.symlink_to(_ux(root1))
    before = _tree(root1)
    run = root1 / "applications" / RUN
    assert main(["export", str(link), "--run", str(run), "--repo", str(root1)]) == 1
    assert "is not a selection of the data root" in capsys.readouterr().err
    assert _tree(root1) == before


def test_export_of_a_relative_path_with_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root1 = signed_copy(tmp_path / "one")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    selection = "../one/root/selections/../selections/ux.yaml"
    run = f"../one/root/applications/{RUN}"
    assert main(["export", selection, "--run", run, "--repo", "../one/root"]) == 0
    assert (root1 / "applications" / RUN / "base.json").is_file()


def test_export_of_a_relative_path_without_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root1 = signed_copy(tmp_path / "one")
    monkeypatch.chdir(root1 / "selections")
    assert main(["export", "ux.yaml", "--run", f"../applications/{RUN}"]) == 0
    assert (root1 / "applications" / RUN / "base.json").is_file()


# --- The approve steps --------------------------------------------------------


def test_approve_refuses_a_selection_of_another_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root1 = tmp_path / "one" / "root"
    shutil.copytree(DATA, root1)
    setup_signing(root1, tmp_path / "one" / "keys")
    root2 = signed_copy(tmp_path / "two")
    capsys.readouterr()
    before = (_tree(root1), _tree(root2))
    assert approve_steps(_ux(root1), root2, _no_ask, DAY) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "is not a selection of the data root" in captured.err
    assert (_tree(root1), _tree(root2)) == before


def test_approve_refuses_a_run_of_another_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root1, root2 = _two_roots(tmp_path)
    run = _with_run(root1)
    capsys.readouterr()
    before = (_tree(root1), _tree(root2))
    assert approve_steps(run, root2, _no_ask, DAY) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "is not a run of the data root" in captured.err
    # check.json is not written either: the refusal comes before each step.
    assert (_tree(root1), _tree(root2)) == before


@pytest.mark.parametrize("case", ["sub", "link"])
def test_approve_refuses_a_selection_that_is_not_a_direct_child(
    tmp_path: Path, case: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root1 = tmp_path / "root"
    shutil.copytree(DATA, root1)
    setup_signing(root1, tmp_path / "keys")
    if case == "sub":
        (root1 / "selections" / "sub").mkdir()
        target = root1 / "selections" / "sub" / "ux.yaml"
        shutil.copy(_ux(root1), target)
    else:
        target = root1 / "selections" / "alias.yaml"
        target.symlink_to(_ux(root1))
    before = _tree(root1)
    assert approve_steps(target, root1, _no_ask, DAY) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "is not a selection of the data root" in captured.err
    assert _tree(root1) == before


@pytest.mark.parametrize("case", ["sub", "link"])
def test_approve_refuses_a_run_that_is_not_a_direct_child(
    tmp_path: Path, case: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root1 = signed_copy(tmp_path / "one")
    run = _with_run(root1)
    if case == "sub":
        target = root1 / "applications" / "sub" / RUN
        shutil.copytree(run, target)
    else:
        target = root1 / "applications" / "2026-11-17-acme-alias"
        target.symlink_to(run)
    capsys.readouterr()
    before = _tree(root1)
    assert approve_steps(target, root1, _no_ask, DAY) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "is not a run of the data root" in captured.err
    assert _tree(root1) == before


def test_approve_a_relative_selection_without_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root1 = tmp_path / "root"
    shutil.copytree(DATA, root1)
    setup_signing(root1, tmp_path / "keys")
    monkeypatch.chdir(root1 / "applications")
    target = Path("../selections/../selections/ux.yaml")
    assert approve_steps(target, None, _yes, DAY) == 0
    assert sig_path(selection_record(_ux(root1))).is_file()
    assert _selection_verdict(root1, _ux(root1)) == (True, "")


def test_approve_a_relative_run_with_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root1 = signed_copy(tmp_path / "one")
    run = _with_run(root1)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    target = Path("../one/root/applications/../applications") / RUN
    assert approve(Path("../one/root"), target) == 0
    assert _run_verdict(root1, run) == (True, "")
