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

from darzi.facts.repo import InputError, read_yaml
from darzi.paths import is_file

APPLICATIONS = "applications"
JOB = "job.md"
RUN_YAML = "run.yaml"
APPROVAL = "approval.json"
RUN_SCHEMA_VERSION = 1
# The status values of a run (docs/SPEC.md section 8).
STATUSES = ("new", "finalized", "submitted")
# The largest run.yaml that load_meta() reads: 64 KiB.
MAX_RUN_YAML = 64 * 1024


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


def new_run(
    repo: Path,
    company: str,
    role: str,
    job_text: str,
    day: dt.date,
    apply_url: str | None = None,
) -> Path:
    """Make the run folder: `job.md` first, then `run.yaml`. Never overwrite.

    `apply_url` is the URL of the job post for `new-run --url`. When it is None,
    `run.yaml` has no `apply_url` key.

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
    meta: dict[str, Any] = {
        "schema_version": RUN_SCHEMA_VERSION,
        "company": company,
        "role": role,
        "created": day.isoformat(),
        "type": None,
        "selection_hash": None,
        "status": "new",
    }
    if apply_url is not None:
        meta["apply_url"] = apply_url
    (run / RUN_YAML).write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")
    return run


def content_hash(tailored: dict[str, Any], letter: str | None) -> str:
    """Return the SHA-256 of the canonical tailored.json plus cover_letter.md."""
    canonical = json.dumps(
        tailored, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    payload = canonical + "\n---cover_letter.md---\n" + (letter or "")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_meta(run_dir: Path) -> dict[str, Any]:
    """Return the parsed `run.yaml` of `run_dir`. An empty file gives {}.

    Raises InputError when the file is missing, cannot be read, is larger than
    MAX_RUN_YAML bytes, uses a YAML alias, is not valid YAML, has a value that
    YAML cannot read, or is not a mapping. Each message is one line.
    """
    path = run_dir / RUN_YAML
    # facts new-run writes a small file with no alias. The limit and the alias
    # test stop a hostile run.yaml that makes the YAML load slow.
    meta: Any = (
        read_yaml(
            path,
            "Make the run with facts new-run.",
            max_bytes=MAX_RUN_YAML,
            aliases=False,
        )
        or {}
    )
    if not isinstance(meta, dict):
        raise InputError(f"{path} is not a mapping. Fix the YAML.")
    return meta


# The PDF page size is US Letter by default. The print_settings of run.yaml
# (the Resume Matcher print settings, for example compactMode) replace a key of
# this mapping. They change the layout, not the facts.
DEFAULT_PRINT: dict[str, Any] = {"pageSize": "LETTER"}


def print_settings(run_dir: Path) -> dict[str, Any]:
    """Return the PDF print settings of a run: DEFAULT_PRINT plus run.yaml.

    The `print_settings` mapping of `run.yaml` replaces the keys of
    DEFAULT_PRINT that it has. An absent `run.yaml`, or no `print_settings`,
    gives DEFAULT_PRINT. Raises InputError when `run.yaml` cannot be read (see
    load_meta) or `print_settings` is not a mapping.
    """
    meta = load_meta(run_dir) if is_file(run_dir / RUN_YAML) else {}
    extra: Any = meta.get("print_settings") or {}
    if not isinstance(extra, dict):
        raise InputError(
            f"{run_dir / RUN_YAML}: print_settings is not a mapping. "
            "Write it as keys and values, or remove it."
        )
    return {**DEFAULT_PRINT, **extra}
