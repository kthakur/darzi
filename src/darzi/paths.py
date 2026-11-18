"""File tests that behave the same on each Python version.

On Python 3.12 and 3.13, `Path.is_file()`, `Path.is_dir()` and `Path.exists()`
raise an OSError such as PermissionError for a path that cannot be read. On
Python 3.14 they return False. Then a folder that cannot be read looks like a
folder that does not exist. These functions call `stat()` and keep the behavior
of Python 3.12 on each version: they return False only for the errors for which
Python 3.12 returns False, and raise each other OSError.

`write_atomic()` writes a file with a temporary file and one rename.
"""

import errno
import os
import stat
from pathlib import Path

# The errors for which Path.is_file() of Python 3.12 returns False.
_IGNORED_ERRNOS = (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP)
# ERROR_INVALID_NAME and ERROR_CANT_RESOLVE_FILENAME (Windows only).
_IGNORED_WINERRORS = (123, 1921)


def _stat(path: Path) -> os.stat_result | None:
    """Return the stat result of `path`, or None when the path does not exist.

    Raises each OSError for which Python 3.12 also raises, for example
    PermissionError.
    """
    try:
        return path.stat()
    except OSError as exc:
        winerror: object = getattr(exc, "winerror", None)
        if exc.errno in _IGNORED_ERRNOS or winerror in _IGNORED_WINERRORS:
            return None
        raise
    except ValueError:
        # A null character in the name, or a name that the file system cannot
        # encode.
        return None


def is_file(path: Path) -> bool:
    """Return True when `path` is a file (or a link to a file)."""
    result = _stat(path)
    return result is not None and stat.S_ISREG(result.st_mode)


def is_dir(path: Path) -> bool:
    """Return True when `path` is a folder (or a link to a folder)."""
    result = _stat(path)
    return result is not None and stat.S_ISDIR(result.st_mode)


def exists(path: Path) -> bool:
    """Return True when `path` exists. A broken link does not exist."""
    return _stat(path) is not None


def write_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path` with a temporary file and one rename.

    The temporary file is `.<name>.tmp` in the same folder. An old temporary
    file is removed first. The new one is made with O_EXCL and O_NOFOLLOW, so
    a symbolic link at that name is never followed. A reader sees the old file
    or the complete new file, never a part of it.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(tmp, flags, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
