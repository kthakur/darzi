"""Shared helpers for the tests of `facts check` (R-1 to R-22).

The synthetic base is the export of `tests/data/selections/ux.yaml`. Its
indexes:

- workExperience: 0 acme (3 bullets), 1 globex (2), 2 freelance-design (1),
  3 vandelay (1, condensed);
- personalProjects: 0 plant-app (1 bullet);
- education: 0 example-u-ba, 1 example-state-hci (coursework);
- additional.technicalSkills: Figma, User research, React, Swift.
"""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from darzi.facts.check import RunFiles, check
from darzi.facts.export import export
from darzi.facts.public import public_copy
from darzi.facts.repo import load_master, load_rules
from darzi.facts.selection import load_selection
from darzi.facts.validate import Finding

from .conftest import DATA

RULES: dict[str, Any] = load_rules(DATA)
MASTER: dict[str, Any] = load_master(DATA)
UX = DATA / "selections" / "ux.yaml"
CORPORATE = DATA / "selections" / "corporate-tech.yaml"

ACME, GLOBEX, FREELANCE, VANDELAY = 0, 1, 2, 3  # workExperience indexes


def exported(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (base, base map) for a synthetic selection, as `facts export` makes."""
    return export(
        public_copy(MASTER, RULES),
        load_selection(path),
        f"selections/{path.name}",
        RULES,
    )


BASE, BASE_MAP = exported(UX)


def tailored() -> dict[str, Any]:
    """Return a fresh copy of the synthetic base, for a test to edit."""
    return copy.deepcopy(BASE)


def findings(
    resume: dict[str, Any],
    letter: str | None = None,
    job: str | None = None,
    *,
    base: dict[str, Any] | None = None,
    the_map: dict[str, Any] | None = None,
    master: dict[str, Any] | None = None,
    rules: dict[str, Any] | None = None,
    questions: dict[str, list[str]] | None = None,
    manifest: dict[str, Any] | None = None,
) -> list[Finding]:
    """Run `check()` on a resume with the synthetic base, map, facts and rules."""
    run = RunFiles(
        resume,
        letter,
        BASE if base is None else base,
        BASE_MAP if the_map is None else the_map,
        job,
        manifest,
    )
    return check(
        run,
        RULES if rules is None else rules,
        MASTER if master is None else master,
        questions,
    )


def found(
    resume: dict[str, Any],
    letter: str | None = None,
    job: str | None = None,
    *,
    only: str | None = None,
    **kwargs: Any,
) -> list[tuple[str, str]]:
    """Return (rule, location) of each error, or of each finding of rule `only`."""
    return [
        (f.rule, f.location)
        for f in findings(resume, letter, job, **kwargs)
        if only == f.rule or (only is None and f.severity == "error")
    ]


def master_with(change: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    """Return a copy of the synthetic facts; `change` gets the entries by id."""
    master = copy.deepcopy(MASTER)
    change(
        {e["id"]: e for e in master["roles"] + master["projects"] + master["education"]}
    )
    return master


def bullet(master: dict[str, Any], bullet_id: str) -> dict[str, Any]:
    """Return one bullet of `master` by its id."""
    return next(
        b
        for e in master["roles"] + master["projects"]
        for b in e["bullets"]
        if b["id"] == bullet_id
    )


def write_run(
    folder: Path,
    resume: Any,
    letter: str | None = None,
    *,
    job: str | None = None,
    manifest: dict[str, Any] | None = None,
    the_map: dict[str, Any] | None = None,
) -> Path:
    """Write a synthetic run folder; `resume` is a dict or the raw tailored.json."""
    folder.mkdir(parents=True)
    text = resume if isinstance(resume, str) else json.dumps(resume, indent=2)
    (folder / "tailored.json").write_text(text, encoding="utf-8")
    (folder / "base.json").write_text(json.dumps(BASE, indent=2), encoding="utf-8")
    the_map = BASE_MAP if the_map is None else the_map
    (folder / "base.map.json").write_text(json.dumps(the_map), encoding="utf-8")
    if letter is not None:
        (folder / "cover_letter.md").write_text(letter, encoding="utf-8")
    if job is not None:
        (folder / "job.md").write_text(job, encoding="utf-8")
    if manifest is not None:
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder
