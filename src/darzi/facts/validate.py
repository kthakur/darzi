"""`facts validate`: check master.yaml against D-1 to D-6 and D-8.

The JSON Schema covers structure (D-1), dates (D-4), types (D-5) and bullet tags
(D-6). Code covers what a schema cannot: unique IDs (D-2) and known sources (D-3).
This module never writes a file (D-7).
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from jsonschema import Draft202012Validator, ValidationError

Severity = Literal["error", "warning"]

ID_SECTIONS = ("roles", "projects", "education")
SOURCE_KEYS = ("sources", "contact_sources")


@dataclass(frozen=True)
class Finding:
    """One problem: rule ID, severity, location, message, and the failing text."""

    rule: str
    severity: Severity
    location: str
    message: str
    text: str = ""


def validate(
    master: Any, schema: dict[str, Any], selections: dict[str, Any] | None = None
) -> list[Finding]:
    """Return all findings for `master` (parsed master.yaml), schema checks first.

    `selections` maps a selection file path to its parsed content (for D-8).
    """
    findings = list(_schema_findings(master, schema))
    if isinstance(master, dict):
        findings += _duplicate_ids(master)
        findings += _unknown_sources(master)
        findings += _missing_display_names(master, selections or {})
    return findings


DISPLAY_FIELDS = {
    "roles": ("org", "display_org"),
    "projects": ("org", "display_org"),
    "education": ("institution", "display_institution"),
}
MAX_PRINT_LENGTH = 50


def _missing_display_names(
    master: dict[str, Any], selections: dict[str, Any]
) -> list[Finding]:
    """D-8: warn when a selected entry has a long name and no display name."""
    used: dict[str, str] = {}
    for sel_path, sel in selections.items():
        for entry_id in _selected_ids(sel):
            used.setdefault(entry_id, sel_path)
    findings: list[Finding] = []
    for section, (field, display) in DISPLAY_FIELDS.items():
        entries: Any = master.get(section)
        if not isinstance(entries, list):
            continue
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict) or entry.get("id") not in used:
                continue
            name: Any = entry.get(field)
            if isinstance(name, str) and len(name) > MAX_PRINT_LENGTH:
                if not entry.get(display):
                    findings.append(
                        Finding(
                            "D-8",
                            "warning",
                            location(master, [section, i]),
                            f"{field} has {len(name)} characters and no {display}, "
                            f"and {used[entry['id']]} uses this entry. "
                            f"Ask the owner for a short {display}.",
                        )
                    )
    return findings


def _selected_ids(selection: Any) -> Iterator[str]:
    """Yield the entry IDs a selection uses (`work`, `projects`, `education`)."""
    if not isinstance(selection, dict):
        return
    for key in ("work", "projects"):
        items: Any = selection.get(key)
        for item in items if isinstance(items, list) else []:
            item_id: Any = item.get("id") if isinstance(item, dict) else item
            if isinstance(item_id, str):
                yield item_id
    education: Any = selection.get("education")
    for item in education if isinstance(education, list) else []:
        if isinstance(item, str):
            yield item


def _schema_findings(master: Any, schema: dict[str, Any]) -> Iterator[Finding]:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(master), key=lambda e: list(e.absolute_path))
    for err in errors:
        for leaf in _leaves(err):
            yield _to_finding(master, leaf)


def _leaves(err: ValidationError) -> Iterator[ValidationError]:
    """Yield the most specific errors (anyOf and allOf wrap the real cause)."""
    if err.validator in ("anyOf", "oneOf") and _key(err.absolute_path) in (
        "start",
        "end",
        "from",
        "until",
    ):
        yield err  # one clear date message, not one per alternative
    elif err.context:
        for sub in err.context:
            yield from _leaves(sub)
    else:
        yield err


def _key(path: Sequence[Any]) -> Any:
    return path[-1] if path else None


def _to_finding(master: Any, err: ValidationError) -> Finding:
    path = list(err.absolute_path)
    where = location(master, path)
    key = _key(path)
    value = err.instance
    if key in ("start", "end", "from", "until"):
        return Finding(
            "D-4",
            "error",
            where,
            f"Date {value!r} is not valid. Use YYYY-MM, YYYY, 'present' or ''.",
        )
    if key == "type" and len(path) == 3 and path[0] in ("roles", "projects"):
        allowed = ", ".join(schema_enum(err))
        return Finding(
            "D-5",
            "error",
            where,
            f"Type {value!r} is not valid. Use one of: {allowed}.",
        )
    if len(path) >= 5 and path[2] == "bullets" and path[-2] == "tags":
        return Finding(
            "D-6",
            "error",
            where,
            f"Tag {value!r} is not in the tag list of master.schema.json. Use a "
            "listed tag, or add the tag to the schema first.",
        )
    if key == "id" and err.validator == "pattern":
        return Finding(
            "D-2",
            "error",
            where,
            f"ID {value!r} is not lowercase and hyphenated. Choose a new ID; "
            "never rename one that a variant uses.",
        )
    return Finding("D-1", "error", where, f"{err.message}. Fix it to match the schema.")


def schema_enum(err: ValidationError) -> list[str]:
    """Return the allowed values of an enum error, as strings."""
    allowed: Any = err.validator_value
    return [str(v) for v in allowed] if isinstance(allowed, list) else []


def _duplicate_ids(master: dict[str, Any]) -> list[Finding]:
    seen: dict[str, str] = {}
    findings: list[Finding] = []
    for where, entry_id in _ids(master):
        if entry_id in seen:
            findings.append(
                Finding(
                    "D-2",
                    "error",
                    where,
                    f"ID {entry_id!r} is also used at {seen[entry_id]}. "
                    "IDs must be unique across roles, projects, bullets and education.",
                )
            )
        else:
            seen[entry_id] = where
    return findings


def _ids(master: dict[str, Any]) -> Iterator[tuple[str, str]]:
    for section in ID_SECTIONS:
        entries: Any = master.get(section)
        if not isinstance(entries, list):
            continue
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            entry_id: Any = entry.get("id")
            if isinstance(entry_id, str):
                yield location(master, [section, i]), entry_id
            bullets: Any = entry.get("bullets")
            if not isinstance(bullets, list):
                continue
            for j, bullet in enumerate(bullets):
                bullet_id: Any = bullet.get("id") if isinstance(bullet, dict) else None
                if isinstance(bullet_id, str):
                    yield location(master, [section, i, "bullets", j]), bullet_id


def _unknown_sources(master: dict[str, Any]) -> list[Finding]:
    known: Any = master.get("source_files")
    known_ids = set(known) if isinstance(known, dict) else set()
    findings: list[Finding] = []
    for path, source in _source_refs(master, []):
        if source not in known_ids:
            findings.append(
                Finding(
                    "D-3",
                    "error",
                    location(master, path),
                    f"Source {source!r} is not a key of source_files. "
                    "Add it to source_files or fix the id.",
                )
            )
    return findings


def _source_refs(node: Any, path: list[Any]) -> Iterator[tuple[list[Any], Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            if path == [] and key == "source_files":
                continue
            if key in SOURCE_KEYS and isinstance(value, list):
                for i, source in enumerate(value):
                    yield [*path, key, i], source
            else:
                yield from _source_refs(value, [*path, key])
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _source_refs(value, [*path, i])


def location(master: Any, path: Sequence[Any]) -> str:
    """Format a path for people: list items show their id or name, if they have one.

    Example: ``roles[acme].bullets[acme-checkout].tags[1]``.
    """
    text = ""
    node: Any = master
    for step in path:
        if isinstance(step, int):
            item: Any = (
                node[step] if isinstance(node, list) and step < len(node) else None
            )
            label: Any = (
                item.get("id") or item.get("name") if isinstance(item, dict) else None
            )
            text += f"[{label}]" if isinstance(label, str) and label else f"[{step}]"
            node = item
        else:
            text += f".{step}" if text else str(step)
            node = node.get(step) if isinstance(node, dict) else None
    return text or "(top level)"
