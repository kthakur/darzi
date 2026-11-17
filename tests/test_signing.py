"""The signed approval: `verify_approval()` and its callers (docs/SPEC.md section 7).

Each test uses a temporary copy of the synthetic data root and a key that the
test makes in a temporary folder, with no passphrase.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from darzi.cli import main
from darzi.config import Approval, load_config
from darzi.facts.check import load_run
from darzi.facts.selection import is_approved, load_selection, selection_hash
from darzi.runs import signing
from darzi.runs.approve import run_is_approved
from darzi.runs.signing import (
    FINGERPRINT,
    NAME,
    SSH_KEYGEN,
    Verdict,
    allowed_signers_path,
    key_path,
    record_bytes,
    selection_record,
    settings_problem,
    sig_path,
    sign_environment,
    sign_record,
    verify_approval,
)

from .conftest import DATA
from .signing_root import (
    allowed_line,
    approve,
    make_key,
    sign_bytes,
    signed_copy,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "darzi"
DAY = dt.date(2026, 11, 17)
RUN = "2026-11-17-acme-product-designer"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A signed copy of the synthetic data root (both selections approved)."""
    return signed_copy(tmp_path)


@pytest.fixture
def key(root: Path, tmp_path: Path) -> Path:
    """The private test key of `root`."""
    return tmp_path / "keys" / "approval_key"


@pytest.fixture
def run_dir(root: Path, tmp_path: Path) -> Path:
    """A run folder in `root` with tailored.json = base.json, approved."""
    run = root / "applications" / RUN
    selection = root / "selections" / "ux.yaml"
    assert main(["export", str(selection), "--run", str(run), "--repo", str(root)]) == 0
    shutil.copy(run / "base.json", run / "tailored.json")
    (run / "cover_letter.md").write_text("Dear team,\nI design forms.\n", "utf-8")
    assert approve(root, run) == 0
    return run


def _settings(root: Path) -> Approval:
    return load_config(root).approval


def _selection(root: Path, name: str = "ux.yaml") -> Verdict:
    path = root / "selections" / name
    return is_approved(root, _settings(root), path, load_selection(path))


def _run(root: Path, run: Path) -> Verdict:
    return run_is_approved(root, _settings(root), run, load_run(run))


def _not(verdict: Verdict, reason: str) -> None:
    assert verdict.approved is False
    assert verdict.fingerprint is None
    assert reason in verdict.reason, verdict.reason


def _set_toml(root: Path, old: str, new: str) -> None:
    toml = root / "facts.toml"
    text = toml.read_text(encoding="utf-8")
    assert old in text
    toml.write_text(text.replace(old, new), encoding="utf-8")


def _ux_record(root: Path) -> Path:
    return selection_record(root / "selections" / "ux.yaml")


def _ux_hash(root: Path) -> str:
    return selection_hash(load_selection(root / "selections" / "ux.yaml"))


# --- The good case ------------------------------------------------------------


def test_a_signed_selection_is_approved(root: Path) -> None:
    verdict = _selection(root)
    assert verdict.approved is True and verdict.reason == ""
    assert verdict.fingerprint is not None
    assert re.fullmatch(r"SHA256:[A-Za-z0-9+/]+", verdict.fingerprint)
    data = json.loads(_ux_record(root).read_text(encoding="utf-8"))
    assert data == {
        "schema_version": 2,
        "kind": "selection",
        "target": "ux",
        "date": "2026-11-17",
        "content_hash": _ux_hash(root),
        "signer": "owner",
    }
    assert _ux_record(root).name == "ux.approval.json"
    assert sig_path(_ux_record(root)).name == "ux.approval.json.sig"


def test_a_signed_run_is_approved(root: Path, run_dir: Path) -> None:
    assert _run(root, run_dir).approved is True
    record = json.loads((run_dir / "approval.json").read_text(encoding="utf-8"))
    assert record["kind"] == "run" and record["target"] == RUN
    assert (run_dir / "approval.json.sig").is_file()


# --- The negative tests of docs/SPEC.md section 7 -----------------------------


def test_a_hand_written_record_with_no_signature(root: Path) -> None:
    sig_path(_ux_record(root)).unlink()
    _not(_selection(root), "ux.approval.json.sig is absent")


def test_a_hand_written_record_and_signature(root: Path, tmp_path: Path) -> None:
    record = _ux_record(root)
    record.write_bytes(record_bytes("selection", "ux", DAY, _ux_hash(root), "owner"))
    sig_path(record).write_text(
        "-----BEGIN SSH SIGNATURE-----\nU1NIU0lH\n-----END SSH SIGNATURE-----\n",
        encoding="utf-8",
    )
    _not(_selection(root), "is not valid")


def test_a_valid_signature_for_different_content(root: Path) -> None:
    # The record changes after the signature: the date is one day later.
    record = _ux_record(root)
    text = record.read_text(encoding="utf-8")
    record.write_text(text.replace("2026-11-17", "2026-11-18"), encoding="utf-8")
    _not(_selection(root), "is not valid")


def test_a_changed_selection_after_approval(root: Path) -> None:
    path = root / "selections" / "ux.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("max_pages: 2", "max_pages: 1"), encoding="utf-8")
    _not(_selection(root), "the content changed after the approval")


def test_a_signature_from_a_key_not_in_allowed_signers(
    root: Path, tmp_path: Path
) -> None:
    other = make_key(tmp_path / "other")
    data = record_bytes("selection", "ux", DAY, _ux_hash(root), "owner")
    sign_bytes(other, _ux_record(root), data)
    _not(_selection(root), "is not valid")


def test_a_changed_tailored_json_after_approval(root: Path, run_dir: Path) -> None:
    tailored = run_dir / "tailored.json"
    data = json.loads(tailored.read_text(encoding="utf-8"))
    data["summary"] = "A different summary."
    tailored.write_text(json.dumps(data), encoding="utf-8")
    _not(_run(root, run_dir), "the content changed after the approval")


def test_a_changed_cover_letter_after_approval(root: Path, run_dir: Path) -> None:
    (run_dir / "cover_letter.md").write_text("Dear team,\nChanged.\n", "utf-8")
    _not(_run(root, run_dir), "the content changed after the approval")


# --- A valid pair in the wrong place ------------------------------------------


def test_a_run_pair_copied_to_another_run(root: Path, run_dir: Path) -> None:
    other = run_dir.with_name("2026-11-18-acme-product-designer")
    shutil.copytree(run_dir, other)
    assert _run(root, run_dir).approved is True
    _not(_run(root, other), "the approval record is for a different run")


def test_a_selection_pair_copied_to_another_selection(root: Path) -> None:
    folder = root / "selections"
    shutil.copy(folder / "ux.yaml", folder / "ux2.yaml")
    shutil.copy(_ux_record(root), folder / "ux2.approval.json")
    shutil.copy(sig_path(_ux_record(root)), folder / "ux2.approval.json.sig")
    _not(_selection(root, "ux2.yaml"), "the approval record is for a different")


def test_a_run_record_used_for_a_selection(root: Path, key: Path) -> None:
    data = record_bytes("run", "ux", DAY, _ux_hash(root), "owner")
    sign_bytes(key, _ux_record(root), data)
    _not(_selection(root), "the approval record is not for a selection")


def test_a_selection_record_used_for_a_run(
    root: Path, run_dir: Path, key: Path
) -> None:
    record = run_dir / "approval.json"
    good = json.loads(record.read_text(encoding="utf-8"))
    data = record_bytes("selection", RUN, DAY, good["content_hash"], "owner")
    sign_bytes(key, record, data)
    _not(_run(root, run_dir), "the approval record is not for a run")


# --- The settings -------------------------------------------------------------


def test_a_wrong_namespace_in_the_signature(root: Path, key: Path) -> None:
    data = record_bytes("selection", "ux", DAY, _ux_hash(root), "owner")
    sign_bytes(key, _ux_record(root), data, namespace="other-namespace")
    _not(_selection(root), "is not valid")


def test_a_changed_namespace_in_facts_toml(root: Path) -> None:
    _set_toml(root, 'namespace = "darzi-approval"', 'namespace = "other-namespace"')
    _not(_selection(root), "is not valid")


def test_a_changed_signer_in_facts_toml(root: Path, key: Path) -> None:
    _set_toml(root, 'signer = "owner"', 'signer = "someone"')
    _not(_selection(root), "is not valid")
    # The same key under the new name: the signature is good, the record is not.
    with (root / "allowed_signers").open("a", encoding="utf-8") as f:
        f.write(allowed_line(key, signer="someone"))
    _not(_selection(root), "the approval record names a different signer")


def test_an_absent_allowed_signers(root: Path) -> None:
    (root / "allowed_signers").unlink()
    _not(_selection(root), "must be a file inside the data root")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('signer = "owner"', 'signer = "-owner"'),
        ('signer = "owner"', 'signer = "-n"'),
        ('namespace = "darzi-approval"', 'namespace = "-darzi-approval"'),
        ('namespace = "darzi-approval"', 'namespace = "-f"'),
    ],
    ids=["signer", "signer-option", "namespace", "namespace-option"],
)
def test_a_signer_or_namespace_that_starts_with_a_dash(
    root: Path, old: str, new: str
) -> None:
    # A value that starts with "-" could look like an option to ssh-keygen.
    _set_toml(root, old, new)
    assert "not permitted" in settings_problem(root, _settings(root))
    _not(_selection(root), "has characters that are not permitted")


def test_the_synthetic_signer_and_namespace_are_permitted(root: Path) -> None:
    # The values of tests/data/facts.toml and of spec section 5.
    settings = _settings(root)
    assert (settings.signer, settings.namespace) == ("owner", "darzi-approval")
    assert settings_problem(root, settings) == ""


@pytest.mark.parametrize(
    "value", ["owner", "darzi-approval", "0", "a", "owner@example.com", "a1._@+-"]
)
def test_name_permits_a_first_letter_or_digit(value: str) -> None:
    assert NAME.fullmatch(value)


@pytest.mark.parametrize(
    "value", ["", "-", "-owner", "--", ".owner", "_owner", "@owner", "+owner", "a b"]
)
def test_name_refuses_other_first_characters(value: str) -> None:
    assert NAME.fullmatch(value) is None


@pytest.mark.parametrize("name", ["signer", "namespace", "allowed_signers"])
def test_an_absent_approval_setting(root: Path, name: str) -> None:
    toml = root / "facts.toml"
    lines = toml.read_text(encoding="utf-8").splitlines(keepends=True)
    toml.write_text(
        "".join(x for x in lines if not x.startswith(f"{name} =")), encoding="utf-8"
    )
    _not(_selection(root), f"facts.toml has no [approval] {name}")


def test_an_absent_approval_section(root: Path) -> None:
    _not(
        is_approved(
            root,
            Approval(),
            root / "selections" / "ux.yaml",
            load_selection(root / "selections" / "ux.yaml"),
        ),
        "facts.toml has no [approval] signer",
    )


@pytest.mark.parametrize("value", ["5", "true", '""', "[1]"])
def test_a_wrong_type_in_facts_toml_is_invalid_input(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    _set_toml(root, 'signer = "owner"', f"signer = {value}")
    selection = root / "selections" / "ux.yaml"
    run = tmp_path / "run"
    code = main(["export", str(selection), "--run", str(run), "--repo", str(root)])
    assert code == 2
    assert "[approval] signer must be a text" in capsys.readouterr().err
    assert not run.exists()


# --- allowed_signers must be inside the data root -----------------------------


def _outside_copy(root: Path, tmp_path: Path) -> Path:
    outside = tmp_path / "outside" / "allowed_signers"
    outside.parent.mkdir()
    shutil.copy(root / "allowed_signers", outside)
    return outside


def test_an_absolute_allowed_signers_path_is_refused(
    root: Path, tmp_path: Path
) -> None:
    outside = _outside_copy(root, tmp_path)
    _set_toml(root, '"allowed_signers"', f'"{outside}"')
    _not(_selection(root), "must be a file inside the data root")


def test_a_dot_dot_allowed_signers_path_is_refused(root: Path, tmp_path: Path) -> None:
    _outside_copy(root, tmp_path)
    _set_toml(root, '"allowed_signers"', '"../outside/allowed_signers"')
    _not(_selection(root), "must be a file inside the data root")


def test_a_link_out_of_the_data_root_is_refused(root: Path, tmp_path: Path) -> None:
    outside = _outside_copy(root, tmp_path)
    (root / "allowed_signers").unlink()
    (root / "allowed_signers").symlink_to(outside)
    _not(_selection(root), "must be a file inside the data root")


@pytest.mark.parametrize("value", ["~/allowed_signers", "", ".", "selections"])
def test_other_allowed_signers_values_are_refused(root: Path, value: str) -> None:
    settings = Approval("owner", "darzi-approval", value, None)
    assert allowed_signers_path(root, settings) is None


# --- The record itself (each one has a valid signature) -----------------------


def _record(root: Path, **change: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(record_bytes("selection", "ux", DAY, "", "owner"))
    data["content_hash"] = _ux_hash(root)
    data.update(change)
    return data


BAD_RECORDS: list[tuple[str, Any, str]] = [
    ("extra-key", {"note": "x"}, "exactly the six keys"),
    ("version-1", {"schema_version": 1}, "not schema version 2"),
    ("version-text", {"schema_version": "2"}, "not schema version 2"),
    ("version-float", {"schema_version": 2.0}, "not schema version 2"),
    ("bad-date", {"date": "2026-02-30"}, "no valid date"),
    ("short-date", {"date": "20261117"}, "no valid date"),
    ("no-date", {"date": None}, "no valid date"),
    ("upper-hash", {"content_hash": "A" * 64}, "no valid content hash"),
    ("short-hash", {"content_hash": "a" * 63}, "no valid content hash"),
    ("other-hash", {"content_hash": "a" * 64}, "the content changed"),
    ("other-target", {"target": "corporate-tech"}, "for a different selection"),
    ("other-signer", {"signer": "someone"}, "names a different signer"),
]


@pytest.mark.parametrize(
    ("change", "reason"),
    [(c, r) for _, c, r in BAD_RECORDS],
    ids=[i for i, _, _ in BAD_RECORDS],
)
def test_a_signed_record_with_a_bad_claim(
    root: Path, key: Path, change: dict[str, Any], reason: str
) -> None:
    data = json.dumps(_record(root, **change)).encode("utf-8")
    sign_bytes(key, _ux_record(root), data)
    _not(_selection(root), reason)


def test_a_signed_record_with_a_missing_key(root: Path, key: Path) -> None:
    data = _record(root)
    del data["date"]
    sign_bytes(key, _ux_record(root), json.dumps(data).encode("utf-8"))
    _not(_selection(root), "exactly the six keys")


@pytest.mark.parametrize(
    "raw",
    [
        b"not json\n",
        b"[1, 2]\n",
        b'{"kind": "selection", "kind": "selection"}\n',
        b"\xff\xfe\n",
    ],
    ids=["not-json", "array", "duplicate-key", "not-utf8"],
)
def test_a_signed_record_that_does_not_parse(root: Path, key: Path, raw: bytes) -> None:
    sign_bytes(key, _ux_record(root), raw)
    verdict = _selection(root)
    assert verdict.approved is False
    assert "approval record" in verdict.reason


def test_a_duplicate_key_with_valid_values_is_refused(root: Path, key: Path) -> None:
    good = record_bytes("selection", "ux", DAY, _ux_hash(root), "owner")
    raw = good.replace(b'"kind": "selection",', b'"kind": "run", "kind": "selection",')
    assert raw != good
    sign_bytes(key, _ux_record(root), raw)
    _not(_selection(root), "is not a valid approval record")


def test_a_record_that_is_too_large(root: Path, key: Path) -> None:
    good = record_bytes("selection", "ux", DAY, _ux_hash(root), "owner")
    sign_bytes(key, _ux_record(root), good + b" " * 5000)
    _not(_selection(root), "too large")


def test_a_folder_in_place_of_the_record(root: Path) -> None:
    _ux_record(root).unlink()
    _ux_record(root).mkdir()
    _not(_selection(root), "is absent")


# --- ssh-keygen ---------------------------------------------------------------


def test_ssh_keygen_has_a_fixed_absolute_path() -> None:
    assert SSH_KEYGEN == "/usr/bin/ssh-keygen"
    assert Path(SSH_KEYGEN).is_file()


def test_an_absent_ssh_keygen_is_not_approved(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(signing, "SSH_KEYGEN", "/nonexistent/ssh-keygen")
    _not(_selection(root), "is not valid")


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired("ssh-keygen", 30),
        OSError("no"),
        RuntimeError("unexpected"),
    ],
    ids=["timeout", "os-error", "other"],
)
def test_an_error_of_the_subprocess_is_not_approved(
    root: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(signing.subprocess, "run", fail)
    verdict = _selection(root)
    assert verdict.approved is False and verdict.fingerprint is None


def test_a_good_exit_with_no_fingerprint_is_not_approved(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def quiet(args: list[str], **_kwargs: Any) -> Any:
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(signing.subprocess, "run", quiet)
    _not(_selection(root), "is not valid")


def test_a_bad_exit_with_a_fingerprint_is_not_approved(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real output of a good verify, with a fingerprint, but a non-zero
    # exit code. The exit code alone must give "not approved".
    real = subprocess.run
    outputs: list[str] = []

    def failed(args: list[str], **kwargs: Any) -> Any:
        result = real(args, **kwargs)
        assert result.returncode == 0
        outputs.append((result.stdout + result.stderr).decode("utf-8", "replace"))
        return subprocess.CompletedProcess(args, 1, result.stdout, result.stderr)

    monkeypatch.setattr(signing.subprocess, "run", failed)
    _not(_selection(root), "is not valid")
    [output] = outputs
    assert FINGERPRINT.search(output)


def test_a_bad_exit_with_a_made_up_fingerprint_is_not_approved(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = b'Good "darzi-approval" signature for owner with ED25519 key SHA256:'
    text += b"A" * 43 + b"\n"
    assert FINGERPRINT.search(text.decode())

    def failed(args: list[str], **_kwargs: Any) -> Any:
        return subprocess.CompletedProcess(args, 255, text, b"")

    monkeypatch.setattr(signing.subprocess, "run", failed)
    _not(_selection(root), "is not valid")


def test_verify_reads_the_record_one_time(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bytes that ssh-keygen gets are the bytes that the parser gets.
    record = _ux_record(root)
    good = record.read_bytes()
    seen: list[bytes] = []
    real = subprocess.run

    def spy(args: list[str], **kwargs: Any) -> Any:
        seen.append(kwargs["input"])
        result = real(args, **kwargs)
        # A change of the file after the verify call has no effect.
        record.write_bytes(good.replace(b"2026-11-17", b"2026-11-18"))
        return result

    monkeypatch.setattr(signing.subprocess, "run", spy)
    assert _selection(root).approved is True
    assert seen == [good]


def test_the_verify_call(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SSH_AUTH_SOCK", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"):
        monkeypatch.setenv(name, "/tmp/x")
    monkeypatch.setenv("DARZI_TEST_KEEP", "1")
    calls: list[tuple[list[str], dict[str, Any]]] = []
    real = subprocess.run

    def spy(args: list[str], **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return real(args, **kwargs)

    monkeypatch.setattr(signing.subprocess, "run", spy)
    assert _selection(root).approved is True
    [(args, kwargs)] = calls
    assert args[:3] == [SSH_KEYGEN, "-Y", "verify"]
    assert args[3:9] == [
        "-f",
        str((root / "allowed_signers").resolve()),
        "-I",
        "owner",
        "-n",
        "darzi-approval",
    ]
    assert "shell" not in kwargs and kwargs["timeout"] == 30
    # The verify call gets the same cleaned environment as the sign call.
    env = kwargs["env"]
    assert not {"SSH_AUTH_SOCK", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"} & set(env)
    assert env["DARZI_TEST_KEEP"] == "1"


def test_the_sign_call_has_no_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("SSH_AUTH_SOCK", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"):
        monkeypatch.setenv(name, "/tmp/x")
    monkeypatch.setenv("DARZI_TEST_KEEP", "1")
    env = sign_environment()
    assert not {"SSH_AUTH_SOCK", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"} & set(env)
    assert env["DARZI_TEST_KEEP"] == "1"
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def spy(args: list[str], **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(signing.subprocess, "run", spy)
    record = tmp_path / "r.json"
    assert sign_record(tmp_path / "k", "darzi-approval", record) is True
    [(args, kwargs)] = calls
    assert args == [
        SSH_KEYGEN,
        "-Y",
        "sign",
        "-f",
        str((tmp_path / "k").resolve()),
        "-n",
        "darzi-approval",
        str(record.resolve()),
    ]
    assert "shell" not in kwargs
    assert "SSH_AUTH_SOCK" not in kwargs["env"]


def test_a_failed_sign_call_gives_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(signing, "SSH_KEYGEN", "/nonexistent/ssh-keygen")
    assert sign_record(tmp_path / "k", "darzi-approval", tmp_path / "r") is False


def test_key_path_uses_the_user_database(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import pwd

    monkeypatch.setenv("HOME", "/nonexistent/other-home")
    home = pwd.getpwuid(os.getuid()).pw_dir
    settings = Approval(key="~/k")
    assert key_path(settings) == Path(home) / "k"
    assert key_path(Approval(key="~other/k")) is None
    assert key_path(Approval(key="/abs/k")) == Path("/abs/k")
    assert key_path(Approval()) is None


def test_an_unknown_kind_is_not_approved(root: Path) -> None:
    verdict = verify_approval(
        root, _settings(root), "other", "ux", _ux_record(root), _ux_hash(root)
    )
    _not(verdict, "is not a kind of approval")


# --- export -------------------------------------------------------------------


def test_export_refuses_a_selection_with_only_the_old_yaml_block(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The old approval block holds the right date and hash; it has no effect.
    path = root / "selections" / "ux.yaml"
    text = path.read_text(encoding="utf-8")
    digest = _ux_hash(root)
    old = "approval:\n  date: null\n  content_hash: null\n"
    assert old in text
    path.write_text(
        text.replace(
            old, f"approval:\n  date: '2026-11-17'\n  content_hash: {digest}\n"
        ),
        encoding="utf-8",
    )
    assert _ux_hash(root) == digest
    _ux_record(root).unlink()
    sig_path(_ux_record(root)).unlink()
    run = tmp_path / "run"
    assert main(["export", str(path), "--run", str(run), "--repo", str(root)]) == 1
    err = capsys.readouterr().err
    assert "is not approved: ux.approval.json is absent" in err
    assert f"The owner runs: facts approve {path}" in err
    assert err.count("\n") == 1
    assert not run.exists()


def test_export_refuses_the_synthetic_data_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # tests/data has no allowed_signers: nothing in it is approved.
    run = tmp_path / "run"
    selection = DATA / "selections" / "ux.yaml"
    assert main(["export", str(selection), "--run", str(run), "--repo", str(DATA)]) == 1
    assert "is not approved" in capsys.readouterr().err
    assert not run.exists()


def test_export_refuses_a_changed_selection(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = root / "selections" / "ux.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("max_pages: 2", "max_pages: 1"), encoding="utf-8")
    run = tmp_path / "run"
    assert main(["export", str(path), "--run", str(run), "--repo", str(root)]) == 1
    assert "the content changed after the approval" in capsys.readouterr().err
    assert not run.exists()


# --- Guards for hard rule 6 ---------------------------------------------------


def test_src_reads_the_environment_only_for_the_ssh_keygen_calls() -> None:
    # os.environ is read in one place only: sign_environment(). Only the verify
    # call and the sign call use it.
    word = re.compile(r"os\.environ|environb|getenv|putenv|sign_environment\(\)")
    found = [
        f"{py.relative_to(SRC)}: {line.strip()}"
        for py in sorted(SRC.rglob("*.py"))
        for line in py.read_text(encoding="utf-8").splitlines()
        if word.search(line)
    ]
    assert len(found) == 4, found
    assert found[0] == "runs/signing.py: env=sign_environment(),"
    assert found[1] == "runs/signing.py: def sign_environment() -> dict[str, str]:"
    assert found[2] == "runs/signing.py: env = dict(os.environ)"
    assert found[3].startswith("runs/signing.py: args, env=sign_environment(),")


def test_src_has_no_shell_call_and_one_program() -> None:
    text = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))
    assert "shell=True" not in text
    assert "os.system" not in text and "Popen" not in text
    assert "shutil.which" not in text
