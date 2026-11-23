"""The kinds of the rules.yaml values that the key test reads, and its hints."""

from __future__ import annotations

import copy
import random
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from darzi.cli import main
from darzi.facts.check import (
    require_keys,
    require_public_keys,
    require_selection_keys,
)
from darzi.facts.repo import InputError, load_rules
from darzi.facts.rules import validate_rules
from darzi.schemas import RULES, load_schema

from .checking import tailored, write_run
from .conftest import DATA

SELECTION = Path("selections") / "corporate-tech.yaml"
NO_MAP: dict[str, Any] = {"entries": []}
TEXT = ("text", "text")
LIST = ("a list of text", "a list")
REGEX = ("a list of text or a mapping of text", "a list or a mapping")
REQUIRED_ANY = ("a mapping of lists of text", "a mapping")
PERIODS = ("a list of mappings", "a list")


def _message(name: str, key: str, kind: tuple[str, str]) -> str:
    return (
        f"rules.yaml: {name}.{key} is not {kind[0]}. Write it as {kind[1]}, "
        "then run the command again."
    )


def _with(name: str, key: str, value: Any) -> dict[str, Any]:
    rules = copy.deepcopy(load_rules(DATA))
    rules[name][key] = value
    return rules


CHECK_CASES = [
    ("banned_words", "terms", None, LIST),
    ("banned_words", "terms", "word", LIST),
    ("banned_words", "terms", 5, LIST),
    ("banned_words", "terms", [1], LIST),
    ("banned_words", "id", 5, TEXT),
    ("banned_words", "severity", None, TEXT),
    ("years_patterns", "regex", None, REGEX),
    ("years_patterns", "regex", 5, REGEX),
    ("years_patterns", "regex", {"a": 1}, REGEX),
    ("employer_of_record", "required_any", ["a"], REQUIRED_ANY),
    ("employer_of_record", "required_any", {"a": "b"}, REQUIRED_ANY),
    ("not_working_periods", "periods", None, PERIODS),
    ("not_working_periods", "periods", ["2020"], PERIODS),
]


@pytest.mark.parametrize(("name", "key", "value", "kind"), CHECK_CASES)
def test_a_wrong_kind_for_check(
    name: str, key: str, value: Any, kind: tuple[str, str]
) -> None:
    with pytest.raises(InputError) as caught:
        require_keys(_with(name, key, value), NO_MAP)
    assert str(caught.value) == _message(name, key, kind)


PUBLIC_CASES = [
    ("removed_items", "terms", None, LIST),
    ("doubtful_items", "terms", "word", LIST),
    ("privacy_patterns", "regex", 5, REGEX),
    ("private_contact_patterns", "terms", {"a": "b"}, LIST),
    ("private_contact_patterns", "id", None, TEXT),
]


@pytest.mark.parametrize(("name", "key", "value", "kind"), PUBLIC_CASES)
def test_a_wrong_kind_for_the_public_copy(
    name: str, key: str, value: Any, kind: tuple[str, str]
) -> None:
    with pytest.raises(InputError) as caught:
        require_public_keys(_with(name, key, value))
    assert str(caught.value) == _message(name, key, kind)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "root"
    shutil.copytree(DATA, folder)
    return folder


def _write_rules(root: Path, name: str, key: str, value: Any) -> None:
    rules: Any = yaml.safe_load((root / "rules.yaml").read_text(encoding="utf-8"))
    if key:
        rules[name][key] = value
    else:
        rules[name] = value
    (root / "rules.yaml").write_text(yaml.safe_dump(rules), encoding="utf-8")


def _run(command: str, root: Path, tmp_path: Path) -> int:
    repo = ["--repo", str(root)]
    if command == "check RUN":
        run = write_run(tmp_path / "check-run", tailored())
        return main(["check", str(run), *repo])
    if command == "check":
        return main(["check", str(root / SELECTION), *repo])
    if command == "export":
        out = tmp_path / "run"
        return main(["export", str(root / SELECTION), "--run", str(out), *repo])
    return main([command, *repo])


@pytest.mark.parametrize("command", ["check RUN", "export", "public", "query"])
@pytest.mark.parametrize("value", [None, 5, "word"], ids=["null", "number", "text"])
def test_the_commands_refuse_a_wrong_kind(
    root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    value: Any,
) -> None:
    _write_rules(root, "removed_items", "terms", value)
    if command == "check RUN":
        # check RUN reads removed_items too; banned_words comes first.
        _write_rules(root, "banned_words", "terms", value)
        name = "banned_words"
    else:
        name = "removed_items"
    assert _run(command, root, tmp_path) == 2
    out, err = capsys.readouterr()
    assert out == ""
    word = command.split()[0]
    assert err == f"facts {word}: {_message(name, 'terms', LIST)}\n"
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("command", ["check", "export", "public", "query"])
def test_the_hint_without_id_and_severity(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    # check SELECTION, export, public and query do not read id and severity of
    # each entry, so the hint does not name them.
    _write_rules(root, "removed_items", "", ["a"])
    assert _run(command, root, tmp_path) == 2
    assert capsys.readouterr().err == (
        f"facts {command}: rules.yaml: removed_items is not a mapping. "
        "Write it as a mapping.\n"
    )


def test_the_hint_of_check_run_names_id_and_severity() -> None:
    rules = copy.deepcopy(load_rules(DATA))
    rules["titles"] = ["a"]
    with pytest.raises(InputError) as caught:
        require_keys(rules, NO_MAP)
    assert str(caught.value) == (
        "rules.yaml: titles is not a mapping. Write it as a mapping with id and "
        "severity."
    )


def test_no_rule_entries_for_the_public_copy() -> None:
    with pytest.raises(InputError) as caught:
        require_public_keys(None)
    assert str(caught.value) == (
        "rules.yaml has no rule entries. Add the rule entries to rules.yaml, "
        "each one a mapping."
    )


# A file that works today must still work: the synthetic rules.yaml and many
# random variations of it that the schema accepts.

WORDS = ["alpha", "beta", "two words", "Acme", "x"]


def _terms(rng: random.Random) -> list[str]:
    return [rng.choice(WORDS) for _ in range(rng.randint(0, 4))]


def _vary(rules: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    """Return a copy of `rules` with random values of the kinds of the schema."""
    out = copy.deepcopy(rules)
    for name, entry in out.items():
        if not isinstance(entry, dict) or "severity" not in entry:
            continue
        entry["severity"] = rng.choice(["error", "warning"])
        if "terms" in entry:
            entry["terms"] = _terms(rng)
        if "regex" in entry and name != "private_contact_patterns":
            patterns = [rf"\b{rng.choice(WORDS)}\b" for _ in range(rng.randint(0, 3))]
            if name != "privacy_patterns" and rng.random() < 0.5:
                entry["regex"] = patterns
            else:
                entry["regex"] = {f"p{i}": p for i, p in enumerate(patterns)}
        if "required_any" in entry:
            entry["required_any"] = {
                f"id-{i}": _terms(rng) for i in range(rng.randint(0, 3))
            }
        if "periods" in entry:
            entry["periods"] = [
                {"from": f"20{10 + i}-01", "to": f"20{10 + i}-06", "source": "x"}
                for i in range(rng.randint(0, 3))
            ]
    return out


def test_the_synthetic_rules_and_300_variations_pass_the_key_test() -> None:
    rules = load_rules(DATA)
    schema = load_schema(RULES)
    rng = random.Random(20261122)
    variations = [rules] + [_vary(rules, rng) for _ in range(300)]
    for variation in variations:
        assert validate_rules(variation, schema) == []
        require_keys(variation, NO_MAP)
        require_public_keys(variation)
        require_selection_keys(variation)
