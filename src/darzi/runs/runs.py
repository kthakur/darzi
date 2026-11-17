"""Run folders: one folder for each application (docs/SPEC.md section 8).

The folder is `applications/<YYYY-MM-DD>-<company>-<role>/`. `new_run` writes
`job.md` before each other file, so a job post is never lost when a later step
fails.
"""

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from darzi.facts.repo import InputError

APPLICATIONS = "applications"
JOB = "job.md"
RUN_YAML = "run.yaml"
APPROVAL = "approval.json"
RUN_SCHEMA_VERSION = 1
# The status values of a run (docs/SPEC.md section 8).
STATUSES = ("new", "finalized", "submitted")


def slug(text: str) -> str:
    """Return a lowercase, hyphenated slug of `text` ("Acme Labs" -> "acme-labs").

    Only the ASCII letters a-z and the digits 0-9 stay. Each other run of
    characters becomes one hyphen. Raises InputError when nothing stays.
    """
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not value:
        raise InputError(f"{text!r} gives an empty name. Use letters or digits.")
    return value


def run_name(company: str, role: str, day: dt.date) -> str:
    """Return the run folder name for one application."""
    return f"{day.isoformat()}-{slug(company)}-{slug(role)}"


def new_run(repo: Path, company: str, role: str, job_text: str, day: dt.date) -> Path:
    """Make the run folder: `job.md` first, then `run.yaml`. Never overwrite.

    Raises InputError for an empty job post, a company or a role that gives an
    empty name, and a run folder that already exists.
    """
    if not job_text.strip():
        raise InputError("The job post is empty. Paste the full job post.")
    run = repo / APPLICATIONS / run_name(company, role, day)
    if run.exists():
        raise InputError(f"{run} already exists. Use it, or choose another role name.")
    run.mkdir(parents=True)
    (run / JOB).write_text(job_text.rstrip() + "\n", encoding="utf-8")
    meta = {
        "schema_version": RUN_SCHEMA_VERSION,
        "company": company,
        "role": role,
        "created": day.isoformat(),
        "type": None,
        "selection_hash": None,
        "status": "new",
    }
    (run / RUN_YAML).write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")
    return run


def content_hash(tailored: dict[str, Any], letter: str | None) -> str:
    """Return the SHA-256 of the canonical tailored.json plus cover_letter.md."""
    canonical = json.dumps(
        tailored, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    payload = canonical + "\n---cover_letter.md---\n" + (letter or "")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _place(exc: yaml.YAMLError) -> str:
    """Return " (line N, column M)" for a YAML error that has a place, else ""."""
    mark: Any = getattr(exc, "problem_mark", None)
    if mark is None:
        return ""
    return f" (line {mark.line + 1}, column {mark.column + 1})"


def load_meta(run_dir: Path) -> dict[str, Any]:
    """Return the parsed `run.yaml` of `run_dir`. An empty file gives {}.

    Raises InputError when the file is missing, cannot be read, is not valid
    YAML, has a value that YAML cannot read, or is not a mapping. Each message is
    one line.
    """
    path = run_dir / RUN_YAML
    # Each file-system call is in the try: is_file() too raises an OSError, for
    # example for a run folder that the user cannot open.
    try:
        if not path.is_file():
            raise InputError(f"{path} does not exist. Make the run with facts new-run.")
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InputError(f"{path} is not UTF-8 text. Save it as UTF-8.") from exc
    except OSError as exc:
        raise InputError(
            f"{path} cannot be read: {exc.strerror}. "
            "Check the permissions of the file and its folder."
        ) from exc
    try:
        meta: Any = yaml.safe_load(text) or {}
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
        # This broad catch is for this one call only.
        raise InputError(
            f"{path} has a value that YAML cannot read. "
            "Fix the value, or put it in quotes."
        ) from exc
    if not isinstance(meta, dict):
        raise InputError(f"{path} is not a mapping. Fix the YAML.")
    return meta
