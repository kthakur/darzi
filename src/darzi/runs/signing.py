"""The signed approval record (docs/SPEC.md section 7).

An approval is two files: a small JSON record and its OpenSSH signature
(`<record>.sig`). `verify_approval()` is the one function that decides
"approved". `facts export` uses it now; `finalize` and `fill-plan` use it later.
It fails closed: each absent file or setting, each read error and each error of
`ssh-keygen` gives "not approved", never an exception.

The settings come only from `[approval]` in `<data root>/facts.toml`. No
environment variable and no flag replaces them or skips the signature.

`ssh-keygen` is called by the absolute path SSH_KEYGEN, with a list of arguments
and no shell. A PATH lookup is not used: a program earlier on the PATH could
then answer in place of OpenSSH.
"""

from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import pwd
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from darzi.config import Approval
from darzi.paths import is_file

# The OpenSSH tool. macOS and the Linux distributions install it here.
SSH_KEYGEN = "/usr/bin/ssh-keygen"
SCHEMA_VERSION = 2
KINDS = ("selection", "run")
RECORD_KEYS = frozenset(
    ("schema_version", "kind", "target", "date", "content_hash", "signer")
)
SIG_SUFFIX = ".sig"
# A record is near 200 bytes and a signature near 300 bytes.
MAX_RECORD = 4 * 1024
MAX_SIG = 16 * 1024
VERIFY_TIMEOUT = 30
# The owner types the passphrase in this time.
SIGN_TIMEOUT = 600
# The sign call must not use an agent or a program that gives the passphrase.
SIGN_ENV_REMOVED = ("SSH_AUTH_SOCK", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE")
HASH = re.compile(r"[0-9a-f]{64}")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# A signer or a namespace: visible ASCII with no space, quote or comma.
NAME = re.compile(r"[A-Za-z0-9._@+-]+")
FINGERPRINT = re.compile(r"\bkey (SHA256:[A-Za-z0-9+/=]+)")


@dataclass(frozen=True)
class Verdict:
    """The result of `verify_approval()`.

    `approved` is True only when the signature and each claim of the record are
    good. `reason` says what failed (empty when approved). `fingerprint` is the
    SHA256 fingerprint of the key that signed (None when not approved).
    """

    approved: bool
    reason: str = ""
    fingerprint: str | None = None


def _no(reason: str) -> Verdict:
    return Verdict(False, reason)


def sig_path(record: Path) -> Path:
    """Return the signature file of the record `record`."""
    return record.with_name(record.name + SIG_SUFFIX)


def selection_record(selection: Path) -> Path:
    """Return the approval record of `selections/<type>.yaml`."""
    return selection.with_name(selection_target(selection) + ".approval.json")


def selection_target(selection: Path) -> str:
    """Return the `target` of a selection record: the file name without .yaml."""
    return selection.resolve().name.removesuffix(".yaml")


def run_target(run_dir: Path) -> str:
    """Return the `target` of a run record: the name of the run folder."""
    return run_dir.resolve().name


def record_bytes(
    kind: str, target: str, day: dt.date, content_hash: str, signer: str
) -> bytes:
    """Return the bytes of a new approval record (JSON, two-space indent)."""
    data = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "target": target,
        "date": day.isoformat(),
        "content_hash": content_hash,
        "signer": signer,
    }
    return (json.dumps(data, indent=2) + "\n").encode("utf-8")


def settings_problem(repo: Path, settings: Approval) -> str:
    """Return what is wrong with the verify settings, or "" when they are good."""
    for key in ("signer", "namespace", "allowed_signers"):
        if getattr(settings, key) is None:
            return f"facts.toml has no [approval] {key}"
    if not NAME.fullmatch(settings.signer or ""):
        return (
            "the [approval] signer in facts.toml has characters that are not permitted"
        )
    if not NAME.fullmatch(settings.namespace or ""):
        return (
            "the [approval] namespace in facts.toml has characters that are "
            "not permitted"
        )
    if allowed_signers_path(repo, settings) is None:
        return (
            "the [approval] allowed_signers in facts.toml must be a file inside "
            "the data root"
        )
    return ""


def allowed_signers_path(repo: Path, settings: Approval) -> Path | None:
    """Return the `allowed_signers` file, or None when it is not permitted.

    The value must be a relative path with no `..` part. The real path (after
    each link) must be inside the real data root, and it must be a file.
    """
    value = settings.allowed_signers
    if value is None:
        return None
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or value.startswith("~"):
        return None
    try:
        root = repo.resolve(strict=True)
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or path == root or not is_file(path):
            return None
    except (OSError, ValueError, RuntimeError):
        return None
    return path


def _read_limited(path: Path, limit: int) -> bytes | None:
    """Return the bytes of `path`, or None when absent, unreadable or too large."""
    try:
        if not is_file(path):
            return None
        with path.open("rb") as f:
            data = f.read(limit + 1)
    except (OSError, ValueError):
        return None
    return None if len(data) > limit else data


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key is in the record two times")
    return dict(pairs)


def _good_signature(
    allowed: Path, signer: str, namespace: str, sig: Path, data: bytes
) -> tuple[bool, str | None]:
    """Run `ssh-keygen -Y verify` on `data`. Return (good, key fingerprint)."""
    args = [
        SSH_KEYGEN,
        "-Y",
        "verify",
        "-f",
        str(allowed),
        "-I",
        signer,
        "-n",
        namespace,
        "-s",
        str(sig.resolve()),
    ]
    try:
        result = subprocess.run(
            args,
            input=data,
            capture_output=True,
            timeout=VERIFY_TIMEOUT,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False, None
    if result.returncode != 0:
        return False, None
    output = (result.stdout + result.stderr).decode("utf-8", "replace")
    found = FINGERPRINT.search(output)
    if found is None:
        return False, None
    return True, found.group(1)


def _claims_problem(
    record: Any, kind: str, target: str, signer: str, current_hash: str
) -> str:
    """Return what is wrong with the claims of a parsed record, or ""."""
    if not isinstance(record, dict):
        return "the approval record is not a JSON object"
    fields: dict[str, Any] = record  # pyright: ignore[reportUnknownVariableType]
    if frozenset(fields) != RECORD_KEYS:
        return "the approval record does not have exactly the six keys"
    version: Any = fields["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        return f"the approval record is not schema version {SCHEMA_VERSION}"
    if fields["kind"] != kind:
        return f"the approval record is not for a {kind}"
    if fields["target"] != target:
        return f"the approval record is for a different {kind}"
    if fields["signer"] != signer:
        return "the approval record names a different signer"
    day: Any = fields["date"]
    if not isinstance(day, str) or not DATE.fullmatch(day):
        return "the approval record has no valid date"
    try:
        dt.date.fromisoformat(day)
    except ValueError:
        return "the approval record has no valid date"
    digest: Any = fields["content_hash"]
    if not isinstance(digest, str) or not HASH.fullmatch(digest):
        return "the approval record has no valid content hash"
    if not HASH.fullmatch(current_hash) or not hmac.compare_digest(
        digest, current_hash
    ):
        return "the content changed after the approval"
    return ""


def verify_approval(
    repo: Path,
    settings: Approval,
    kind: str,
    target: str,
    record: Path,
    current_hash: str,
) -> Verdict:
    """Decide whether `record` approves the content with hash `current_hash`.

    Approved means: `ssh-keygen` verifies the signature `<record>.sig` on the
    exact bytes of the record with the signer, the namespace and the
    `allowed_signers` file of `settings`; then the record has exactly the six
    keys, schema version 2, this `kind`, `target` and signer, a valid date, and
    a content hash equal to `current_hash`. Each other case is "not approved".
    This function does not raise for a bad file or setting.
    """
    try:
        return _verify(repo, settings, kind, target, record, current_hash)
    except Exception:  # each unexpected error means "not approved"
        return _no("the approval cannot be verified")


def _verify(
    repo: Path,
    settings: Approval,
    kind: str,
    target: str,
    record: Path,
    current_hash: str,
) -> Verdict:
    if kind not in KINDS:
        return _no(f"{kind!r} is not a kind of approval")
    problem = settings_problem(repo, settings)
    if problem:
        return _no(problem)
    allowed = allowed_signers_path(repo, settings)
    signer = settings.signer
    namespace = settings.namespace
    if allowed is None or signer is None or namespace is None:
        return _no("the [approval] settings are not complete")
    sig = sig_path(record)
    # Read the record one time. The same bytes go to ssh-keygen and to the
    # JSON parser, so a change of the file between the two steps has no effect.
    data = _read_limited(record, MAX_RECORD)
    if data is None:
        return _no(f"{record.name} is absent, cannot be read, or is too large")
    if _read_limited(sig, MAX_SIG) is None:
        return _no(f"{sig.name} is absent, cannot be read, or is too large")
    good, fingerprint = _good_signature(allowed, signer, namespace, sig, data)
    if not good:
        return _no(f"the signature {sig.name} is not valid for {record.name}")
    try:
        parsed: Any = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError):
        return _no(f"{record.name} is not a valid approval record")
    problem = _claims_problem(parsed, kind, target, signer, current_hash)
    if problem:
        return _no(problem)
    return Verdict(True, "", fingerprint)


def key_path(settings: Approval) -> Path | None:
    """Return the private key file of `[approval] key`, or None when not set.

    A leading `~/` means the home folder of the user in the system user
    database. The HOME environment variable is not read.
    """
    value = settings.key
    if value is None:
        return None
    if value == "~" or value.startswith("~/"):
        home = pwd.getpwuid(os.getuid()).pw_dir
        return Path(home) / value[2:]
    if value.startswith("~"):
        return None
    return Path(value)


def sign_environment() -> dict[str, str]:
    """Return a copy of the environment for the sign call, with no agent.

    SSH_AUTH_SOCK, SSH_ASKPASS and SSH_ASKPASS_REQUIRE are removed. Then only a
    passphrase that a person types at the terminal can open the key.
    """
    env = dict(os.environ)
    for name in SIGN_ENV_REMOVED:
        env.pop(name, None)
    return env


def sign_record(key: Path, namespace: str, record: Path) -> bool:
    """Sign `record` with `key`; ssh-keygen writes `<record>.sig`.

    stdin, stdout and stderr stay on the terminal, so that ssh-keygen can ask
    for the passphrase. Return True when ssh-keygen gives exit code 0.
    """
    args = [
        SSH_KEYGEN,
        "-Y",
        "sign",
        "-f",
        str(key.resolve()),
        "-n",
        namespace,
        str(record.resolve()),
    ]
    try:
        result = subprocess.run(
            args, env=sign_environment(), timeout=SIGN_TIMEOUT, check=False
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
