"""`darzi.paths`: file tests that behave the same on each Python version (issue 13)."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from darzi import paths

SRC = Path(__file__).resolve().parent.parent / "src" / "darzi"
NOT_ROOT = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="a permission test needs a POSIX user that is not root",
)
TESTS: list[Callable[[Path], bool]] = [paths.is_file, paths.is_dir, paths.exists]


@contextmanager
def _closed(folder: Path) -> Generator[None]:
    """Remove all permissions of `folder`, and set them back after the test."""
    old = folder.stat().st_mode
    folder.chmod(0)
    try:
        yield
    finally:
        folder.chmod(old)


def test_a_file(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("x\n", encoding="utf-8")
    assert (paths.is_file(path), paths.is_dir(path), paths.exists(path)) == (
        True,
        False,
        True,
    )


def test_a_folder(tmp_path: Path) -> None:
    assert (paths.is_file(tmp_path), paths.is_dir(tmp_path)) == (False, True)
    assert paths.exists(tmp_path)


@pytest.mark.parametrize("test", TESTS)
def test_no_file(tmp_path: Path, test: Callable[[Path], bool]) -> None:
    assert test(tmp_path / "nothing") is False
    # A name below a file: ENOTDIR.
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    assert test(tmp_path / "a.txt" / "b") is False


@pytest.mark.parametrize("test", TESTS)
def test_a_link_loop_and_a_broken_link(
    tmp_path: Path, test: Callable[[Path], bool]
) -> None:
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    (tmp_path / "broken").symlink_to(tmp_path / "nothing")
    assert test(tmp_path / "loop") is False
    assert test(tmp_path / "broken") is False


@pytest.mark.parametrize("test", TESTS)
def test_a_null_character_in_the_name(
    tmp_path: Path, test: Callable[[Path], bool]
) -> None:
    assert test(tmp_path / "a\x00b") is False


@NOT_ROOT
@pytest.mark.parametrize("test", TESTS)
def test_a_folder_that_cannot_be_read_raises(
    tmp_path: Path, test: Callable[[Path], bool]
) -> None:
    # The behavior of Python 3.12 on each version: PermissionError, not False.
    closed = tmp_path / "closed"
    closed.mkdir()
    (closed / "a.txt").write_text("x\n", encoding="utf-8")
    with _closed(closed):
        with pytest.raises(PermissionError):
            test(closed / "a.txt")


def test_src_uses_the_helpers() -> None:
    """Each file test in src/darzi uses darzi.paths, not a Path method.

    The one exception is new_run() in runs/runs.py, which issue 13 does not
    change.
    """
    call = re.compile(r"\.(is_file|is_dir|exists)\(\)")
    found = [
        f"{py.relative_to(SRC)}:{number}: {line.strip()}"
        for py in sorted(SRC.rglob("*.py"))
        if py.name != "paths.py"
        for number, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1)
        if call.search(line)
    ]
    assert found == ["runs/runs.py:67: if run.exists():"]
