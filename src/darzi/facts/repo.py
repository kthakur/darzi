"""Find the data root and load its files (read only; PyYAML safe_load for reads).

The schemas come from the package data (`darzi.schemas.load_schema`), not from
the data root (docs/SPEC.md section 4).
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from darzi.paths import is_file

MASTER = "master.yaml"
RULES = "rules.yaml"


class InputError(Exception):
    """An input file is missing or cannot be read. The CLI exits with code 2."""


def find_repo(start: Path | None = None) -> Path:
    """Return the nearest folder at or above `start` that holds master.yaml."""
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        if is_file(folder / MASTER):
            return folder
    raise InputError(f"No {MASTER} found. Run inside the repo or pass --repo PATH.")


def _place(exc: yaml.YAMLError) -> str:
    """Return " (line N, column M)" for a YAML error that has a place, else ""."""
    mark: Any = getattr(exc, "problem_mark", None)
    if mark is None:
        return ""
    return f" (line {mark.line + 1}, column {mark.column + 1})"


def _yaml_call(path: Path, call: Callable[[], Any]) -> Any:
    """Return the result of one PyYAML call on the text of `path`.

    Raises InputError, with a message of one line, for each error of the call.
    """
    try:
        return call()
    except yaml.YAMLError as exc:
        # The text of the YAML error has more than one line and shows a line of
        # the file. Give only the place of the error.
        raise InputError(
            f"{path} is not valid YAML{_place(exc)}. Fix the YAML."
        ) from exc
    except ValueError as exc:
        # PyYAML raises a plain ValueError for a value such as a date that does
        # not exist (2026-09-31).
        raise InputError(
            f"{path} has a value that is not valid, for example a date that "
            "does not exist. Fix the value, or put it in quotes."
        ) from exc
    except RecursionError as exc:
        raise InputError(
            f"{path} has lists or mappings that are nested too deep. Fix the YAML."
        ) from exc
    except Exception as exc:
        # The PyYAML constructors raise other classes too for a bad value, for
        # example KeyError for "!!bool maybe" and IndexError for '!!int ""'.
        # This broad catch is for the one PyYAML call only.
        raise InputError(
            f"{path} has a value that YAML cannot read. "
            "Fix the value, or put it in quotes."
        ) from exc


def _has_alias(text: str) -> bool:
    """Return True when the YAML `text` uses an alias (*name)."""
    events = yaml.parse(text, Loader=yaml.SafeLoader)
    return any(isinstance(event, yaml.AliasEvent) for event in events)


def read_yaml(
    path: Path,
    fix: str,
    *,
    max_bytes: int | None = None,
    aliases: bool = True,
) -> Any:
    """Return the parsed YAML file `path` (PyYAML safe_load).

    Raises InputError, with a message of one line, when the file is missing
    (the message ends with `fix`), cannot be read, is not UTF-8, is not valid
    YAML, or has a value that YAML cannot read. With `max_bytes`, a larger file
    is refused. With `aliases=False`, a file that uses a YAML alias is refused.
    """
    try:
        if not is_file(path):
            raise InputError(f"{path} does not exist. {fix}")
        with path.open("rb") as f:
            raw = f.read() if max_bytes is None else f.read(max_bytes + 1)
        if max_bytes is not None and len(raw) > max_bytes:
            raise InputError(
                f"{path} is larger than {max_bytes // 1024} KiB. "
                "Keep only the values that the file needs."
            )
        # The same text as Path.read_text(): UTF-8, with each line end as "\n".
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise InputError(f"{path} is not UTF-8 text. Save it as UTF-8.") from exc
    except OSError as exc:
        raise InputError(
            f"{path} cannot be read: {exc.strerror}. "
            "Check the permissions of the file and its folder."
        ) from exc
    if not aliases and _yaml_call(path, lambda: _has_alias(text)):
        raise InputError(
            f"{path} uses a YAML alias (*name). "
            "Write each value in full, with no & anchor and no * alias."
        )
    return _yaml_call(path, lambda: yaml.safe_load(text))


def load_master(repo: Path) -> Any:
    """Return the parsed master.yaml of `repo`."""
    return read_yaml(repo / MASTER, "Pass the repo folder with --repo.")


def load_selections(repo: Path) -> dict[str, Any]:
    """Return each `selections/*.yaml` file, parsed, by its repo-relative path."""
    return {
        str(path.relative_to(repo)): read_yaml(path, "Restore it from git.")
        for path in sorted((repo / "selections").glob("*.yaml"))
    }


def load_rules(repo: Path) -> Any:
    """Return the parsed rules.yaml of `repo`."""
    return read_yaml(repo / RULES, "Restore it from git.")
