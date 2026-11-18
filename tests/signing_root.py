"""Test helpers for the signed approval: a temporary key and a signed data root.

Each key is made by the test in a temporary folder, with no passphrase. No
key and no signed file is part of the repo. These helpers never read a file in
the home folder of the user.
"""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
from pathlib import Path

from darzi.cli import approve_steps
from darzi.runs.signing import SSH_KEYGEN, sig_path

from .conftest import DATA

NAMESPACE = "darzi-approval"
SIGNER = "owner"
# The key line of tests/data/facts.toml. The path does not exist on purpose.
SYNTHETIC_KEY = 'key = "/nonexistent/darzi-test/approval_key"'


def make_key(folder: Path, name: str = "approval_key") -> Path:
    """Make an ed25519 key with no passphrase in `folder`; return its path."""
    folder.mkdir(parents=True, exist_ok=True)
    key = folder / name
    subprocess.run(
        [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def allowed_line(key: Path, signer: str = SIGNER, namespace: str = NAMESPACE) -> str:
    """Return the allowed_signers line of the public key of `key`."""
    public = key.with_name(key.name + ".pub").read_text(encoding="utf-8").split()
    return f'{signer} namespaces="{namespace}" {public[0]} {public[1]}\n'


def setup_signing(root: Path, keys: Path) -> Path:
    """Give the data root `root` a test key, allowed_signers and [approval].

    Return the path of the private key.
    """
    key = make_key(keys)
    (root / "allowed_signers").write_text(allowed_line(key), encoding="utf-8")
    toml = root / "facts.toml"
    line = f'key = "{key}"'
    if toml.is_file():
        text = toml.read_text(encoding="utf-8")
        assert SYNTHETIC_KEY in text
        toml.write_text(text.replace(SYNTHETIC_KEY, line), encoding="utf-8")
    else:
        toml.write_text(
            "[approval]\n"
            f'signer = "{SIGNER}"\n'
            f'namespace = "{NAMESPACE}"\n'
            'allowed_signers = "allowed_signers"\n'
            f"{line}\n",
            encoding="utf-8",
        )
    return key


def approve(root: Path, target: Path, answer: str = "y") -> int:
    """Run the steps of `facts approve` below the terminal test."""
    return approve_steps(target, root, lambda _prompt: answer, dt.date(2026, 11, 17))


def signed_copy(tmp_path: Path) -> Path:
    """Return a copy of tests/data with a test key and both selections approved."""
    root = tmp_path / "root"
    shutil.copytree(DATA, root)
    setup_signing(root, tmp_path / "keys")
    for name in ("ux.yaml", "corporate-tech.yaml"):
        assert approve(root, root / "selections" / name) == 0
    return root


def signature_verifies(root: Path, record: Path) -> bool:
    """Return True when the key list of `root` accepts the signature of `record`.

    This checks only the signature, not the rules of `verify_approval()`.
    """
    result = subprocess.run(
        [
            SSH_KEYGEN,
            "-Y",
            "verify",
            "-f",
            str(root / "allowed_signers"),
            "-I",
            SIGNER,
            "-n",
            NAMESPACE,
            "-s",
            str(sig_path(record)),
        ],
        input=record.read_bytes(),
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def sign_bytes(
    key: Path, record: Path, data: bytes, namespace: str = NAMESPACE
) -> None:
    """Write `data` to `record` and sign it with `key` (a test key only)."""
    record.write_bytes(data)
    sig_path(record).unlink(missing_ok=True)
    subprocess.run(
        [SSH_KEYGEN, "-Y", "sign", "-f", str(key), "-n", namespace, str(record)],
        check=True,
        capture_output=True,
    )
