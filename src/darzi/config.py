"""Read the settings in `facts.toml` of the data root (docs/SPEC.md section 5).

This module reads only `<data root>/facts.toml`. It reads no environment
variable. Each work item adds the section that it needs: `[owner]` (W-4),
`[approval]` (W-5), `[render]` (W-6) and `[form]` (W-9).
"""

import re
import tomllib
import unicodedata
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


# The keys of [render.resume_matcher] (docs/SPEC.md section 5). Each is a text.
RESUME_MATCHER_KEYS = (
    "path",
    "commit",
    "data_dir",
    "frontend_url",
    "backend_url",
    "node",
)


@dataclass(frozen=True)
class ResumeMatcherSettings:
    """The `[render.resume_matcher]` settings, as text. None means: not set.

    `path` and `data_dir` are relative to the data root. The module
    `darzi.render.resume_matcher` checks and resolves them at render time.
    """

    path: str | None = None
    commit: str | None = None
    data_dir: str | None = None
    frontend_url: str | None = None
    backend_url: str | None = None
    node: str | None = None


@dataclass(frozen=True)
class RenderSettings:
    """The `[render]` settings. None means: not set."""

    adapter: str | None = None
    resume_matcher: ResumeMatcherSettings = ResumeMatcherSettings()


@dataclass(frozen=True)
class FormSettings:
    """The `[form]` settings: the blocked host names, in the form of `host_name()`."""

    blocked_hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    """The settings of one data root.

    `render` is None without `[render]`, and `form` is None without `[form]`.
    """

    owner_name: str = DEFAULT_OWNER
    approval: Approval = Approval()
    render: RenderSettings | None = None
    form: FormSettings | None = None


# One label of an ASCII host name (RFC 1123): letters, digits and hyphens.
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# The dots that IDNA reads as a dot between labels.
_DOTS = re.compile("[\u002e\u3002\uff0e\uff61]")
MAX_HOST = 253


def host_name(text: str) -> str | None:
    """Return the host name `text` in one comparable ASCII form, or None.

    The steps: Unicode NFKC, each IDNA dot as ".", small letters, one trailing
    dot removed, then IDNA (the Python codec) to ASCII. The result must be
    labels of letters, digits and hyphens, separated by one dot, and the last
    label must not be only digits (an IP address is not a host name). Each
    other text gives None.
    """
    value = _DOTS.sub(".", unicodedata.normalize("NFKC", text)).lower()
    value = value.removesuffix(".")
    try:
        ascii_name = value.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    labels = ascii_name.split(".")
    if len(ascii_name) > MAX_HOST or not all(_LABEL.fullmatch(x) for x in labels):
        return None
    if labels[-1].isdigit():
        return None
    return ascii_name


def load_config(repo: Path) -> Config:
    """Return the settings of the data root `repo`.

    An absent `facts.toml`, `[owner]` section or `name` gives the name
    "the owner". An absent `[approval]` section or value gives None for that
    value, and the same for `[render]` and `[form]`. Raises InputError when the
    file is not valid TOML, when `[owner]`, `[approval]`, `[render]`,
    `[render.resume_matcher]` or `[form]` is not a table, when `name` or a
    value of these sections is not a non-empty string, or when `[form]` has no
    `blocked_hosts` list of plain host names.
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
    return Config(
        owner_name=_owner_name(path, data),
        approval=_approval(path, data),
        render=_render(path, data),
        form=_form(path, data),
    )


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


def _text(path: Path, section: str, key: str, value: Any) -> str | None:
    """Return `value` when it is None or a non-empty text, else raise InputError."""
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise InputError(
            f"{path}: [{section}] {key} must be a text that is not empty. "
            f"Fix the {key}."
        )
    return value


def _render(path: Path, data: dict[str, Any]) -> RenderSettings | None:
    section: Any = data.get("render")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise InputError(f"{path}: render must be a table. Write it as [render].")
    adapter = _text(path, "render", "adapter", section.get("adapter"))
    table: Any = section.get("resume_matcher")
    if table is None:
        return RenderSettings(adapter=adapter)
    if not isinstance(table, dict):
        raise InputError(
            f"{path}: render.resume_matcher must be a table. "
            "Write it as [render.resume_matcher]."
        )
    values = {
        key: _text(path, "render.resume_matcher", key, table.get(key))
        for key in RESUME_MATCHER_KEYS
    }
    return RenderSettings(
        adapter=adapter, resume_matcher=ResumeMatcherSettings(**values)
    )


def _form(path: Path, data: dict[str, Any]) -> FormSettings | None:
    section: Any = data.get("form")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise InputError(f"{path}: form must be a table. Write it as [form].")
    hosts: Any = section.get("blocked_hosts")
    if not isinstance(hosts, list):
        raise InputError(
            f"{path}: [form] blocked_hosts must be a list of host names. "
            "Write blocked_hosts = [] for no blocked host."
        )
    names: list[str] = []
    for entry in hosts:  # pyright: ignore[reportUnknownVariableType]
        name = _blocked_host(entry)
        if name is None:
            raise InputError(
                f"{path}: [form] blocked_hosts has {entry!r}, which is not a plain "
                'host name. Write only the name, for example "jobs.example.com".'
            )
        names.append(name)
    return FormSettings(tuple(names))


def _blocked_host(entry: Any) -> str | None:
    """Return the host name of one blocked_hosts entry, or None when not plain."""
    if not isinstance(entry, str) or entry != entry.strip():
        return None
    # No scheme, path, port, user, wildcard or space: only a name.
    if any(c in entry for c in ":/\\@*?#[]%") or any(c.isspace() for c in entry):
        return None
    return host_name(entry)
