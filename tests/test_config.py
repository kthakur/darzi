"""`config.py`: the [owner] section of facts.toml (work item W-4)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from darzi.config import DEFAULT_OWNER, FACTS_TOML, Config, load_config
from darzi.facts.repo import InputError

from .conftest import DATA

# Each section of docs/SPEC.md section 5.
SECTIONS = {"owner", "approval", "render", "form", "fit", "handover"}


def _write(tmp_path: Path, text: str) -> Path:
    (tmp_path / FACTS_TOML).write_text(text, encoding="utf-8")
    return tmp_path


def test_synthetic_facts_toml_loads() -> None:
    assert load_config(DATA) == Config(owner_name="Test Person")


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
