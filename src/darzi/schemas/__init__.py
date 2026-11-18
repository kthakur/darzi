"""The JSON Schemas of the data root files, stored as package data.

The schemas come with the package, not from the data root (docs/SPEC.md
section 4). Each file is a JSON Schema, draft 2020-12.
"""

import json
from importlib.resources import files
from typing import Any

MASTER = "master.schema.json"
RULES = "rules.schema.json"
SELECTION = "selection.schema.json"
ANSWERS = "answers.schema.json"
REQUIREMENTS = "requirements.schema.json"
NAMES = (MASTER, RULES, SELECTION, ANSWERS, REQUIREMENTS)


def load_schema(name: str) -> dict[str, Any]:
    """Return the schema `name` (for example `MASTER`) from the package data."""
    if name not in NAMES:
        raise ValueError(
            f"Schema {name!r} does not exist. Use one of: {', '.join(NAMES)}."
        )
    text = files(__package__).joinpath(name).read_text(encoding="utf-8")
    schema: dict[str, Any] = json.loads(text)
    return schema
