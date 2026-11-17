"""List the runs of a data root for `facts status` (docs/SPEC.md section 6).

The command reads each `applications/<run>/run.yaml`. It writes nothing.
"""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from darzi.facts.repo import InputError
from darzi.facts.validate import Finding
from darzi.paths import is_dir
from darzi.runs.runs import APPLICATIONS, RUN_YAML, STATUSES, load_meta

RULE = "status"


@dataclass(frozen=True)
class RunRow:
    """One run: its folder name and the values from its `run.yaml`.

    A value is None when `run.yaml` has no value for it.
    """

    run: str
    date: str | None
    company: str | None
    role: str | None
    type: str | None
    status: str | None


def _text(path: Path, key: str, value: Any) -> str | None:
    """Return `value` as text for the table, or None when there is no value.

    Accepts only text, a number, a date and None. Raises InputError for each
    other value, for example a list or a mapping, and for an integer that is too
    large to make into text. It never makes text from a list or a mapping: a
    value built from YAML aliases can be very large.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dt.date):  # also a datetime
        return value.isoformat()
    if isinstance(value, int | float):  # also a bool
        try:
            return str(value)
        except ValueError:
            # Python refuses to make text from an integer with more than 4300
            # digits, for example a long hexadecimal number.
            pass
    raise InputError(
        f"The value of {key} in {path} is not text. Write it as text, in quotes."
    )


def _is_run(path: Path) -> bool:
    """Return True when `path` is a folder. Raises InputError when it cannot tell."""
    try:
        return is_dir(path)
    except OSError as exc:
        raise InputError(
            f"{path} cannot be read: {exc.strerror}. Check the permissions."
        ) from exc


def _row(run_dir: Path) -> RunRow:
    """Return the row of one run folder. Raises InputError for a bad `run.yaml`."""
    meta = load_meta(run_dir)
    path = run_dir / RUN_YAML
    return RunRow(
        run=run_dir.name,
        date=_text(path, "created", meta.get("created")),
        company=_text(path, "company", meta.get("company")),
        role=_text(path, "role", meta.get("role")),
        type=_text(path, "type", meta.get("type")),
        status=_text(path, "status", meta.get("status")),
    )


def list_runs(repo: Path) -> tuple[list[RunRow], list[Finding]]:
    """Return one row for each run folder of `repo`, in folder-name order.

    A folder that cannot be read, or that has a `run.yaml` that is missing, cannot
    be read or has a value that is not text, gives an error and no row. The other
    runs still give a row. A status value that is not in STATUSES gives a
    warning. Raises InputError when the `applications` folder cannot be read.
    """
    rows: list[RunRow] = []
    findings: list[Finding] = []
    folder = repo / APPLICATIONS
    try:
        if not is_dir(folder):
            return rows, findings
        entries = sorted(folder.iterdir())
    except OSError as exc:
        raise InputError(
            f"{folder} cannot be read: {exc.strerror}. "
            "Check the permissions of the folder."
        ) from exc
    for run_dir in entries:
        try:
            if not _is_run(run_dir):
                continue
            row = _row(run_dir)
        except InputError as exc:
            findings.append(Finding(RULE, "error", run_dir.name, str(exc)))
            continue
        if row.status not in STATUSES:
            findings.append(
                Finding(
                    RULE,
                    "warning",
                    run_dir.name,
                    f"Status {row.status!r} is not one of {', '.join(STATUSES)}. "
                    "Fix the status in run.yaml.",
                )
            )
        rows.append(row)
    return rows, findings
