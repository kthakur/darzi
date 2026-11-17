"""Find the data root and load its files (read only; PyYAML safe_load for reads).

The schemas come from the package data (`darzi.schemas.load_schema`), not from
the data root (docs/SPEC.md section 4).
"""

from pathlib import Path
from typing import Any

import yaml

MASTER = "master.yaml"
RULES = "rules.yaml"


class InputError(Exception):
    """An input file is missing or cannot be read. The CLI exits with code 2."""


def find_repo(start: Path | None = None) -> Path:
    """Return the nearest folder at or above `start` that holds master.yaml."""
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        if (folder / MASTER).is_file():
            return folder
    raise InputError(f"No {MASTER} found. Run inside the repo or pass --repo PATH.")


def load_master(repo: Path) -> Any:
    """Return the parsed master.yaml of `repo`."""
    path = repo / MASTER
    if not path.is_file():
        raise InputError(f"{path} does not exist. Pass the repo folder with --repo.")
    try:
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise InputError(f"{path} is not valid YAML: {exc}. Fix the YAML.") from exc


def load_selections(repo: Path) -> dict[str, Any]:
    """Return each `selections/*.yaml` file, parsed, by its repo-relative path."""
    selections: dict[str, Any] = {}
    for path in sorted((repo / "selections").glob("*.yaml")):
        try:
            with path.open(encoding="utf-8") as f:
                selections[str(path.relative_to(repo))] = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise InputError(f"{path} is not valid YAML: {exc}. Fix the YAML.") from exc
    return selections


def load_rules(repo: Path) -> Any:
    """Return the parsed rules.yaml of `repo`."""
    path = repo / RULES
    if not path.is_file():
        raise InputError(f"{path} does not exist. Restore it from git.")
    try:
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise InputError(f"{path} is not valid YAML: {exc}. Fix the YAML.") from exc
