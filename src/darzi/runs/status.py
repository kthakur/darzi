"""List the runs of a data root for `facts status` (docs/SPEC.md section 6).

The command reads each `applications/<run>/run.yaml`. It writes nothing.
"""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from darzi.facts.repo import InputError
from darzi.facts.validate import Finding
from darzi.runs.runs import APPLICATIONS, STATUSES, load_meta

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


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def list_runs(repo: Path) -> tuple[list[RunRow], list[Finding]]:
    """Return one row for each run folder of `repo`, in folder-name order.

    A folder with a `run.yaml` that is missing or cannot be read gives an error
    and no row. A status value that is not in STATUSES gives a warning. Raises
    InputError when the `applications` folder cannot be read.
    """
    rows: list[RunRow] = []
    findings: list[Finding] = []
    folder = repo / APPLICATIONS
    if not folder.is_dir():
        return rows, findings
    try:
        runs = sorted(p for p in folder.iterdir() if p.is_dir())
    except OSError as exc:
        raise InputError(f"{folder} cannot be read: {exc.strerror}.") from exc
    for run_dir in runs:
        try:
            meta = load_meta(run_dir)
        except InputError as exc:
            findings.append(Finding(RULE, "error", run_dir.name, str(exc)))
            continue
        row = RunRow(
            run=run_dir.name,
            date=_text(meta.get("created")),
            company=_text(meta.get("company")),
            role=_text(meta.get("role")),
            type=_text(meta.get("type")),
            status=_text(meta.get("status")),
        )
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
