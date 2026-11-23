"""The run lock and the render lock: a normal lock, a held lock, a named pipe."""

from __future__ import annotations

import fcntl
import os
import tempfile
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from darzi.facts.repo import InputError
from darzi.render import resume_matcher
from darzi.render.port import RenderError
from darzi.runs import lock
from darzi.runs.lock import locked

from .render_fakes import FakeCli, fake_renderer

# A lock that waits on a named pipe would wait with no end. The test waits this
# many seconds, then fails.
TIME_LIMIT = 5.0


@pytest.fixture(autouse=True)
def own_temp_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give the lock files a temporary folder of the test."""
    folder = tmp_path / "system-tmp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


def _in_time(work: Callable[[], object]) -> BaseException | None:
    """Run `work` in a thread. Return its exception, or None. Fail on a wait."""
    caught: list[BaseException] = []

    def target() -> None:
        try:
            work()
        except BaseException as exc:  # noqa: BLE001 - the test reads it.
            caught.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(TIME_LIMIT)
    assert not thread.is_alive(), "the lock waits on a named pipe"
    return caught[0] if caught else None


@contextmanager
def _reader(path: Path) -> Generator[None]:
    """Hold a reader open on the named pipe at `path`."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        yield
    finally:
        os.close(fd)


def _take_run_lock(run_dir: Path) -> None:
    with locked(run_dir, InputError):
        pass


def test_a_normal_run_lock_works_two_times(tmp_path: Path) -> None:
    _take_run_lock(tmp_path)
    _take_run_lock(tmp_path)
    assert lock.lock_path(tmp_path).is_file()


def test_a_held_run_lock_refuses_at_once(tmp_path: Path) -> None:
    fd = os.open(lock.lock_path(tmp_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        error = _in_time(lambda: _take_run_lock(tmp_path))
    finally:
        os.close(fd)
    assert isinstance(error, InputError)
    assert str(error) == (
        f"another finalize, fill-plan, fit or submitted of {tmp_path} runs now. "
        "Wait for it to end, then try again."
    )


def test_a_named_pipe_with_no_reader_at_the_run_lock(tmp_path: Path) -> None:
    os.mkfifo(lock.lock_path(tmp_path))
    error = _in_time(lambda: _take_run_lock(tmp_path))
    assert isinstance(error, InputError)
    assert str(error).startswith(f"cannot open the lock file of {tmp_path}: ")


def test_a_named_pipe_with_a_reader_at_the_run_lock(tmp_path: Path) -> None:
    path = lock.lock_path(tmp_path)
    os.mkfifo(path)
    with _reader(path):
        error = _in_time(lambda: _take_run_lock(tmp_path))
    assert isinstance(error, InputError)
    assert str(error) == (
        f"the lock file of {tmp_path} is not a regular file. "
        "Check the temporary folder."
    )


def _render(root: Path) -> None:
    fake_renderer(root, FakeCli()).render({}, {}, root / "out.pdf")


def _render_lock(root: Path) -> Path:
    """Return the render lock path of the fake install that _render() makes."""
    renderer = fake_renderer(root / "probe", FakeCli())
    data_dir = root / renderer.config.data_dir.relative_to(root / "probe")
    return resume_matcher.lock_path(data_dir)


def test_a_named_pipe_with_no_reader_at_the_render_lock(tmp_path: Path) -> None:
    os.mkfifo(_render_lock(tmp_path))
    error = _in_time(lambda: _render(tmp_path))
    assert isinstance(error, RenderError)
    assert str(error).startswith("cannot open the lock file for the data folder ")


def test_a_named_pipe_with_a_reader_at_the_render_lock(tmp_path: Path) -> None:
    path = _render_lock(tmp_path)
    os.mkfifo(path)
    with _reader(path):
        error = _in_time(lambda: _render(tmp_path))
    assert isinstance(error, RenderError)
    assert "is not a regular file. Check the temporary folder." in str(error)


def test_a_held_render_lock_refuses_at_once(tmp_path: Path) -> None:
    fd = os.open(_render_lock(tmp_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        error = _in_time(lambda: _render(tmp_path))
    finally:
        os.close(fd)
    assert isinstance(error, RenderError)
    assert "another render uses" in str(error)
