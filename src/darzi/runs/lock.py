"""The run lock: one command at a time changes or reads a run folder.

`finalize`, `fill-plan` and `submitted` take the same lock, so no command
changes a run while another reads or writes it. The lock is a file in the
system temporary folder (see `lock_path`), not in the run folder. So it is
never an output, and a refusal leaves the run folder as it was.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from darzi.facts.repo import InputError

LOCK_PREFIX = "darzi-finalize-"
LOCK_SUFFIX = ".lock"


def lock_path(run_dir: Path) -> Path:
    """Return the lock file of `run_dir`, in the system temporary folder.

    The name holds the SHA-256 hash of the resolved run folder path, so the
    same run folder always gives the same lock file.
    """
    key = hashlib.sha256(os.fsencode(run_dir.resolve())).hexdigest()[:32]
    return Path(tempfile.gettempdir()) / f"{LOCK_PREFIX}{key}{LOCK_SUFFIX}"


@contextmanager
def locked(run_dir: Path, error: type[InputError]) -> Generator[None]:
    """Hold the lock of `run_dir`. The system releases it when the process stops.

    Raise `error` when the lock file cannot be opened or another command
    holds the lock. The caller picks the error, so that its exit code stays.
    """
    try:
        # O_NOFOLLOW: a symbolic link in the temporary folder is refused.
        fd = os.open(
            lock_path(run_dir), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
    except (OSError, RuntimeError) as exc:
        raise error(
            f"cannot open the lock file of {run_dir}: {exc}. "
            "Check the temporary folder."
        ) from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise error(
                f"another finalize, fill-plan or submitted of {run_dir} runs now. "
                "Wait for it to end, then try again."
            ) from exc
        yield
    finally:
        os.close(fd)  # This also releases the lock.
