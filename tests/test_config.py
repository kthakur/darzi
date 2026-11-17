"""`config.py`: the [owner] section of facts.toml (work item W-4)."""

from __future__ import annotations

import copy
import importlib
import os
import tomllib
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

import pytest

from darzi.config import DEFAULT_OWNER, FACTS_TOML, Approval, Config, load_config
from darzi.facts.repo import InputError

from .conftest import DATA

# The [approval] section of tests/data/facts.toml.
SYNTHETIC = Approval(
    signer="owner",
    namespace="darzi-approval",
    allowed_signers="allowed_signers",
    key="/nonexistent/darzi-test/approval_key",
)

# Each section of docs/SPEC.md section 5.
SECTIONS = {"owner", "approval", "render", "form", "fit", "handover"}


def _write(tmp_path: Path, text: str) -> Path:
    (tmp_path / FACTS_TOML).write_text(text, encoding="utf-8")
    return tmp_path


def test_synthetic_facts_toml_loads() -> None:
    assert load_config(DATA) == Config(owner_name="Test Person", approval=SYNTHETIC)


def test_synthetic_facts_toml_has_each_section_and_no_real_path() -> None:
    with (DATA / FACTS_TOML).open("rb") as f:
        data = tomllib.load(f)
    assert set(data) == SECTIONS
    assert set(data["render"]) == {"adapter", "resume_matcher"}
    key = Path(data["approval"]["key"])
    rm_path = Path(data["render"]["resume_matcher"]["path"])
    for path in (key, rm_path):
        assert path.is_absolute() and not path.exists()
        assert "~" not in str(path) and "Users" not in path.parts
        assert "home" not in path.parts
    for host in data["form"]["blocked_hosts"]:
        assert host == "example.com" or host.endswith(".example.com")
    assert data["fit"] == {"green": 0.7, "yellow": 0.4}


def _values(value: Any) -> Iterator[str]:
    """Yield each text value in a parsed TOML value, at each depth."""
    if isinstance(value, dict):
        for item in value.values():  # pyright: ignore[reportUnknownVariableType]
            yield from _values(item)
    elif isinstance(value, list):
        for item in value:  # pyright: ignore[reportUnknownVariableType]
            yield from _values(item)
    elif isinstance(value, str):
        yield value


def test_synthetic_facts_toml_points_to_no_real_place() -> None:
    # A later test that uses these values must not reach the owner's renderer,
    # key or Resume Matcher install.
    with (DATA / FACTS_TOML).open("rb") as f:
        data = tomllib.load(f)
    texts = list(_values(data))
    assert "Test Person" in texts
    for text in texts:
        for word in ("localhost", "127.0.0.1", "::1", "~", "/Users/", "/home/"):
            assert word not in text, f"{word!r} in {text!r}"
    rm = data["render"]["resume_matcher"]
    for url in (rm["frontend_url"], rm["backend_url"]):
        host = urlsplit(url).hostname
        assert host is not None and host.endswith(".invalid")
    assert not Path(data["approval"]["key"]).exists()
    assert not Path(rm["path"]).exists()


def test_no_facts_toml_gives_the_owner(tmp_path: Path) -> None:
    assert load_config(tmp_path).owner_name == DEFAULT_OWNER == "the owner"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[fit]\ngreen = 0.7\n",
        "[owner]\n",
        "[owner]\nemail = 'test.person@example.com'\n",
    ],
)
def test_no_owner_or_no_name_gives_the_owner(tmp_path: Path, text: str) -> None:
    assert load_config(_write(tmp_path, text)).owner_name == "the owner"


def test_the_name_is_read(tmp_path: Path) -> None:
    text = "[owner]\nname = 'Caf\u00e9 Person'\n"
    assert load_config(_write(tmp_path, text)).owner_name == "Caf\u00e9 Person"


def test_unknown_sections_and_keys_are_ignored(tmp_path: Path) -> None:
    text = "[owner]\nname = 'Test Person'\nnick = 'TP'\n[later]\nx = 1\n"
    assert load_config(_write(tmp_path, text)).owner_name == "Test Person"


@pytest.mark.parametrize("text", ["[owner\nname = 'x'\n", "name = \n", "[owner]\nname"])
def test_bad_toml_is_refused(tmp_path: Path, text: str) -> None:
    with pytest.raises(InputError, match=r"facts\.toml is not valid TOML"):
        load_config(_write(tmp_path, text))


def test_toml_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    (tmp_path / FACTS_TOML).write_bytes(b"[owner]\nname = 'Caf\xe9'\n")
    with pytest.raises(InputError, match="not valid TOML"):
        load_config(tmp_path)


@pytest.mark.parametrize("text", ["owner = 'x'\n", "owner = 5\n", "owner = []\n"])
def test_owner_that_is_not_a_table_is_refused(tmp_path: Path, text: str) -> None:
    with pytest.raises(InputError, match="owner must be a table"):
        load_config(_write(tmp_path, text))


@pytest.mark.parametrize(
    "value", ["5", "''", "'   '", "true", "['Test Person']", "{a = 1}"]
)
def test_name_that_is_not_a_text_is_refused(tmp_path: Path, value: str) -> None:
    with pytest.raises(InputError, match=r"\[owner\] name must be a text"):
        load_config(_write(tmp_path, f"[owner]\nname = {value}\n"))


def test_a_facts_toml_that_cannot_be_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "[owner]\nname = 'Test Person'\n")

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(InputError) as exc:
        load_config(tmp_path)
    path = tmp_path / FACTS_TOML
    assert str(exc.value) == (
        f"{path} cannot be read: Permission denied. "
        "Check the permissions of the file and its folder."
    )


def test_a_facts_toml_that_cannot_be_tested_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The file test (stat()) too raises an OSError, for example for a folder that
    # the user cannot open.
    def fail(*args: Any, **kwargs: Any) -> bool:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "stat", fail)
    with pytest.raises(InputError, match="cannot be read: Permission denied"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        "[owner]\nage = 1" + "0" * 5000 + "\n",
        "x = " + "[" * 5000 + "]" * 5000 + "\n",
    ],
    ids=["long-integer", "deep-list"],
)
def test_a_facts_toml_value_that_toml_cannot_read_is_refused(
    tmp_path: Path, text: str
) -> None:
    # Issue 13: tomllib raises a plain ValueError or RecursionError here, not a
    # TOMLDecodeError.
    (tmp_path / FACTS_TOML).write_text(text, encoding="utf-8")
    with pytest.raises(InputError) as exc:
        load_config(tmp_path)
    message = str(exc.value)
    assert message.startswith(str(tmp_path / FACTS_TOML))
    assert "\n" not in message and len(message) < 300


def test_a_folder_named_facts_toml_gives_the_owner(tmp_path: Path) -> None:
    (tmp_path / FACTS_TOML).mkdir()
    assert load_config(tmp_path).owner_name == "the owner"


def test_config_reads_no_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only <data root>/facts.toml counts. The environment changes nothing.
    monkeypatch.setenv("DARZI_OWNER", "Somebody Else")
    monkeypatch.setenv("FACTS_TOML", str(DATA / FACTS_TOML))
    monkeypatch.setenv("DARZI_DATA_ROOT", str(DATA))
    assert load_config(tmp_path).owner_name == "the owner"
    source = Path(__file__).resolve().parents[1] / "src" / "darzi" / "config.py"
    text = source.read_text(encoding="utf-8")
    for word in ("import os", "from os", "os.environ", "getenv", "environb"):
        assert word not in text


class _NoEnvironment(MutableMapping[Any, Any]):
    """An environment mapping. Each access records its name and fails the test.

    Each method of a mapping fails, also the methods that only make a view
    (keys, items, values), the text forms (str, repr, format), the "|" operators,
    copy and pickle. An attribute that the class does not have fails too.
    """

    def __init__(self, reads: list[str]) -> None:
        object.__setattr__(self, "_reads", reads)

    def _fail(self, name: str) -> NoReturn:
        reads: list[str] = object.__getattribute__(self, "_reads")
        reads.append(name)
        pytest.fail(f"load_config read the environment ({name})")

    def __getattr__(self, name: str) -> Any:
        self._fail(name)

    def __setattr__(self, name: str, value: Any) -> None:
        self._fail("__setattr__")

    def __getitem__(self, key: Any) -> Any:
        self._fail("__getitem__")

    def __setitem__(self, key: Any, value: Any) -> None:
        self._fail("__setitem__")

    def __delitem__(self, key: Any) -> None:
        self._fail("__delitem__")

    def __iter__(self) -> Iterator[Any]:
        self._fail("__iter__")

    def __len__(self) -> int:
        self._fail("__len__")

    def __contains__(self, key: object) -> bool:
        self._fail("__contains__")

    def __eq__(self, other: object) -> bool:
        self._fail("__eq__")

    def __ne__(self, other: object) -> bool:
        self._fail("__ne__")

    def __bool__(self) -> bool:
        self._fail("__bool__")

    def __repr__(self) -> str:
        self._fail("__repr__")

    def __str__(self) -> str:
        self._fail("__str__")

    def __format__(self, spec: str) -> str:
        self._fail("__format__")

    def __or__(self, other: Any) -> Any:
        self._fail("__or__")

    def __ror__(self, other: Any) -> Any:
        self._fail("__ror__")

    def __ior__(self, other: Any) -> Any:
        self._fail("__ior__")

    def __copy__(self) -> Any:
        self._fail("__copy__")

    def __deepcopy__(self, memo: Any) -> Any:
        self._fail("__deepcopy__")

    def __reduce__(self) -> Any:
        self._fail("__reduce__")

    def __reduce_ex__(self, protocol: Any) -> Any:
        self._fail("__reduce_ex__")

    def copy(self) -> Any:
        self._fail("copy")

    def get(self, *args: Any, **kwargs: Any) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
        self._fail("get")

    def keys(self) -> Any:
        self._fail("keys")

    def items(self) -> Any:
        self._fail("items")

    def values(self) -> Any:
        self._fail("values")

    def pop(self, *args: Any, **kwargs: Any) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
        self._fail("pop")

    def popitem(self) -> Any:
        self._fail("popitem")

    def setdefault(self, *args: Any, **kwargs: Any) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
        self._fail("setdefault")

    def update(self, *args: Any, **kwargs: Any) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
        self._fail("update")

    def clear(self) -> None:
        self._fail("clear")


def test_the_environment_trap_fails_on_each_kind_of_read() -> None:
    # Proof that the trap below catches each read: each call fails the test
    # (pytest.fail raises an exception that is not an Exception subclass).
    reads: list[str] = []
    trap = _NoEnvironment(reads)
    calls: list[Any] = [
        lambda: trap["HOME"],
        lambda: trap.get("HOME"),
        lambda: trap.pop("HOME", None),
        lambda: trap.setdefault("HOME", "x"),
        lambda: "HOME" in trap,
        lambda: len(trap),
        lambda: bool(trap),
        lambda: list(trap),
        lambda: trap.keys(),
        lambda: trap.items(),
        lambda: trap.values(),
        lambda: dict(trap),
        lambda: str(trap),
        lambda: repr(trap),
        lambda: f"{trap}",
        lambda: trap | {},
        lambda: {} | trap,
        lambda: trap.copy(),
        lambda: copy.copy(trap),
        lambda: trap == {},
        lambda: trap.encodekey,
    ]
    for call in calls:
        with pytest.raises(pytest.fail.Exception):
            call()
    assert len(reads) >= len(calls)


def test_config_does_not_read_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each access to os.environ, os.environb or the low-level environ of the
    # posix (or nt) module fails the test. The context gives back the true
    # environment before pytest uses it again.
    reads: list[str] = []
    low_level = importlib.import_module(os.name)
    with monkeypatch.context() as m:
        m.setattr(os, "environ", _NoEnvironment(reads))
        m.setattr(os, "environb", _NoEnvironment(reads), raising=False)
        m.setattr(low_level, "environ", _NoEnvironment(reads))
        synthetic = load_config(DATA)
        empty = load_config(tmp_path)
        _write(tmp_path, "[owner]\nname = 'Test Person'\n")
        written = load_config(tmp_path)
    assert reads == []
    assert synthetic == Config(owner_name="Test Person", approval=SYNTHETIC)
    assert written == Config(owner_name="Test Person")
    assert empty == Config()
    source = Path(__file__).resolve().parents[1] / "src" / "darzi" / "config.py"
    lines = source.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith(("import os", "from os")) for line in lines)
