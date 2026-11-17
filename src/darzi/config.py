"""Read the settings in `facts.toml` of the data root (docs/SPEC.md section 5).

This module reads only `<data root>/facts.toml`. It reads no environment
variable. Each work item adds the section that it needs. Work item W-4 reads
`[owner]` only.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from darzi.facts.repo import InputError
from darzi.paths import is_file

FACTS_TOML = "facts.toml"
DEFAULT_OWNER = "the owner"


@dataclass(frozen=True)
class Config:
    """The settings of one data root."""

    owner_name: str = DEFAULT_OWNER


def load_config(repo: Path) -> Config:
    """Return the settings of the data root `repo`.

    An absent `facts.toml`, `[owner]` section or `name` gives the name
    "the owner". Raises InputError when the file is not valid TOML, when
    `[owner]` is not a table, or when `name` is not a non-empty string.
    """
    path = repo / FACTS_TOML
    try:
        if not is_file(path):
            return Config()
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise InputError(f"{path} is not valid TOML: {exc}. Fix the TOML.") from exc
    except (ValueError, RecursionError) as exc:
        # tomllib raises a plain ValueError for an integer of more than 4300
        # digits, and RecursionError for tables or lists nested too deep.
        raise InputError(
            f"{path} has a value that TOML cannot read, for example a very long "
            "number or a very deep nesting. Fix the TOML."
        ) from exc
    except OSError as exc:
        raise InputError(
            f"{path} cannot be read: {exc.strerror}. "
            "Check the permissions of the file and its folder."
        ) from exc
    owner: Any = data.get("owner")
    if owner is None:
        return Config()
    if not isinstance(owner, dict):
        raise InputError(f"{path}: owner must be a table. Write it as [owner].")
    name: Any = owner.get("name")
    if name is None:
        return Config()
    if not isinstance(name, str) or not name.strip():
        raise InputError(
            f"{path}: [owner] name must be a text that is not empty. Fix the name."
        )
    return Config(owner_name=name)
