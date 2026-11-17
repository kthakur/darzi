"""Read the settings in `facts.toml` of the data root (docs/SPEC.md section 5).

This module reads only `<data root>/facts.toml`. It reads no environment
variable. Each work item adds the section that it needs: `[owner]` (W-4) and
`[approval]` (W-5).
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from darzi.facts.repo import InputError
from darzi.paths import is_file

FACTS_TOML = "facts.toml"
DEFAULT_OWNER = "the owner"


# The keys of [approval] (docs/SPEC.md section 5). Each is a text.
APPROVAL_KEYS = ("signer", "namespace", "allowed_signers", "key")


@dataclass(frozen=True)
class Approval:
    """The `[approval]` settings, as text. None means: not set.

    `allowed_signers` is relative to the data root and `key` can start with
    `~`. The module `darzi.runs.signing` checks and resolves both paths.
    """

    signer: str | None = None
    namespace: str | None = None
    allowed_signers: str | None = None
    key: str | None = None


@dataclass(frozen=True)
class Config:
    """The settings of one data root."""

    owner_name: str = DEFAULT_OWNER
    approval: Approval = Approval()


def load_config(repo: Path) -> Config:
    """Return the settings of the data root `repo`.

    An absent `facts.toml`, `[owner]` section or `name` gives the name
    "the owner". An absent `[approval]` section or value gives None for that
    value. Raises InputError when the file is not valid TOML, when `[owner]`
    or `[approval]` is not a table, or when `name` or an `[approval]` value is
    not a non-empty string.
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
    return Config(owner_name=_owner_name(path, data), approval=_approval(path, data))


def _owner_name(path: Path, data: dict[str, Any]) -> str:
    owner: Any = data.get("owner")
    if owner is None:
        return DEFAULT_OWNER
    if not isinstance(owner, dict):
        raise InputError(f"{path}: owner must be a table. Write it as [owner].")
    name: Any = owner.get("name")
    if name is None:
        return DEFAULT_OWNER
    if not isinstance(name, str) or not name.strip():
        raise InputError(
            f"{path}: [owner] name must be a text that is not empty. Fix the name."
        )
    return name


def _approval(path: Path, data: dict[str, Any]) -> Approval:
    section: Any = data.get("approval")
    if section is None:
        return Approval()
    if not isinstance(section, dict):
        raise InputError(f"{path}: approval must be a table. Write it as [approval].")
    values: dict[str, str | None] = {}
    for key in APPROVAL_KEYS:
        value: Any = section.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise InputError(
                f"{path}: [approval] {key} must be a text that is not empty. "
                f"Fix the {key}."
            )
        values[key] = value
    return Approval(**values)
