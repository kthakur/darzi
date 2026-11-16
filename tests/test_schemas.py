"""The schemas as package data, and the synthetic data root (work item W-1)."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from darzi.schemas import ANSWERS, MASTER, NAMES, RULES, SELECTION, load_schema

DATA = Path(__file__).resolve().parent / "data"

# Each synthetic file in the data root, with the schema it must match.
FILES: list[tuple[str, str]] = [
    ("master.yaml", MASTER),
    ("rules.yaml", RULES),
    ("selections/ux.yaml", SELECTION),
    ("selections/corporate-tech.yaml", SELECTION),
    ("answers.yaml", ANSWERS),
]


def load(name: str) -> Any:
    with (DATA / name).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def errors(data: Any, schema_name: str) -> list[str]:
    validator = Draft202012Validator(load_schema(schema_name))
    return [e.message for e in validator.iter_errors(data)]


@pytest.mark.parametrize("name", NAMES)
def test_schema_loads_and_is_valid(name: str) -> None:
    schema = load_schema(name)
    Draft202012Validator.check_schema(schema)
    assert schema["$id"] == name


def test_unknown_schema_is_refused() -> None:
    with pytest.raises(ValueError, match="does not exist"):
        load_schema("job.schema.json")


@pytest.mark.parametrize(("file", "schema_name"), FILES)
def test_synthetic_file_is_valid(file: str, schema_name: str) -> None:
    assert errors(load(file), schema_name) == []


def test_data_root_has_the_section_10_files() -> None:
    selections = sorted(p.name for p in (DATA / "selections").glob("*.yaml"))
    assert len(selections) == 2
    assert (DATA / "questions.md").is_file()


Change = Callable[[Any], object]

BROKEN: list[tuple[str, str, Change]] = [
    ("master.yaml", MASTER, lambda d: d["roles"][0].update(start="2021-13")),
    ("master.yaml", MASTER, lambda d: d["roles"][0]["bullets"][0].pop("tags")),
    ("master.yaml", MASTER, lambda d: d["roles"][0].update(type="salaried")),
    ("rules.yaml", RULES, lambda d: d["banned_words"].pop("severity")),
    ("rules.yaml", RULES, lambda d: d.pop("tech_terms")),
    ("selections/ux.yaml", SELECTION, lambda d: d.pop("type")),
    ("selections/ux.yaml", SELECTION, lambda d: d.update(tier=4)),
    ("answers.yaml", ANSWERS, lambda d: d["answers"][0].update(id="Bad_Id")),
]


@pytest.mark.parametrize(("file", "schema_name", "change"), BROKEN)
def test_broken_file_fails(file: str, schema_name: str, change: Change) -> None:
    data = copy.deepcopy(load(file))
    change(data)
    assert errors(data, schema_name) != []


def _descriptions(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("description", "title") and isinstance(value, str):
                found.append(value)
            else:
                found += _descriptions(value)
    elif isinstance(node, list):
        for item in node:
            found += _descriptions(item)
    return found


@pytest.mark.parametrize("name", NAMES)
def test_descriptions_point_to_no_old_document(name: str) -> None:
    text = json.dumps(_descriptions(load_schema(name)))
    for old in ("SRS", "AGENTS.md", "D-1", "§"):
        assert old not in text, f"{name} still points to {old}"
