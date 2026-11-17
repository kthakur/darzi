"""Selection files: load, hash and check them (D-20, D-21).

A selection picks facts by ID and sets the layout for one job type. It holds no
fact text. The owner approves each version (`facts approve`); the approval holds
the SHA-256 of the selection without its `approval` field. The signed approval
test comes with work item W-5 (docs/SPEC.md section 7).
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from darzi.facts.repo import InputError
from darzi.facts.validate import Finding


def load_selection(path: Path) -> dict[str, Any]:
    """Return a parsed selection file."""
    if not path.is_file():
        raise InputError(f"{path} does not exist. Pass a selections/<type>.yaml file.")
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise InputError(f"{path} is not valid YAML: {exc}. Fix the YAML.") from exc
    if not isinstance(data, dict):
        raise InputError(f"{path} is not a selection (expected a mapping).")
    return data


def selection_hash(selection: dict[str, Any]) -> str:
    """Return the SHA-256 of the selection content, without its `approval` field."""
    content = {k: v for k, v in selection.items() if k != "approval"}
    canonical = json.dumps(
        content, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _f(where: str, message: str, rule: str = "D-21") -> Finding:
    return Finding(rule, "error", f"selection: {where}", message)


def check_selection(
    selection: dict[str, Any],
    schema: dict[str, Any],
    master: dict[str, Any],
    rules: dict[str, Any],
    questions: dict[str, list[str]],
) -> list[Finding]:
    """Return D-20 (format) and D-21 (facts) findings for a selection."""
    findings = [
        _f(
            ".".join(str(p) for p in e.absolute_path) or "(top level)",
            f"{e.message}. Fix it to match the selection schema.",
            "D-20",
        )
        for e in Draft202012Validator(schema).iter_errors(selection)
    ]
    if findings:
        return findings
    job_type = selection["type"]
    blocked = {"all", job_type}
    removed = set(rules["removed_items"].get("skills") or [])
    doubtful = set(rules["doubtful_items"].get("bullet_ids") or [])
    entries = {e["id"]: e for e in master["roles"] + master["projects"]}

    headlines = {h["text"] for h in master["profile"].get("headlines") or []}
    if selection["headline"] not in headlines:
        findings.append(
            _f(
                "headline",
                "Headline is not a profile.headlines text. "
                "Use one of them word for word.",
            )
        )

    used_bullets: set[str] = set()
    for section in ("work", "projects"):
        for i, pick in enumerate(selection[section]):
            where = f"{section}[{i}] {pick['id']}"
            entry = entries.get(pick["id"])
            if entry is None:
                findings.append(
                    _f(where, "No role or project has this ID in master.yaml.")
                )
                continue
            if blocked & set(entry.get("exclude_from") or []):
                findings.append(
                    _f(where, f"Entry is excluded from {job_type} resumes.")
                )
            if pick["id"] in questions:
                findings.append(
                    _f(
                        where,
                        f"Entry is named in open question(s) "
                        f"{', '.join(questions[pick['id']])}. Wait for the owner.",
                    )
                )
            own = {b["id"]: b for b in entry.get("bullets") or []}
            for bullet_id in pick["bullets"]:
                bullet = own.get(bullet_id)
                if bullet is None:
                    findings.append(
                        _f(where, f"Bullet {bullet_id} does not belong to this entry.")
                    )
                    continue
                used_bullets.add(bullet_id)
                if blocked & set(bullet.get("exclude_from") or []):
                    findings.append(
                        _f(
                            where,
                            f"Bullet {bullet_id} is excluded from {job_type} resumes.",
                        )
                    )
                if bullet_id in doubtful:
                    findings.append(
                        _f(where, f"Bullet {bullet_id} is in doubt (rules.yaml).")
                    )
                if bullet_id in questions:
                    findings.append(
                        _f(
                            where,
                            f"Bullet {bullet_id} is named in open "
                            f"question(s) {', '.join(questions[bullet_id])}.",
                        )
                    )
            if len(set(pick["bullets"])) != len(pick["bullets"]):
                findings.append(_f(where, "A bullet is listed twice."))

    for bullet_id in selection.get("summary_basis") or []:
        if bullet_id not in used_bullets:
            findings.append(
                _f(
                    "summary_basis",
                    f"{bullet_id} is not a bullet of this "
                    "selection. The summary may only use selected facts.",
                )
            )

    education = {e["id"]: e for e in master["education"]}
    for edu_id in selection["education"]:
        if edu_id not in education:
            findings.append(
                _f(f"education {edu_id}", "No education entry has this ID.")
            )
        elif blocked & set(education[edu_id].get("exclude_from") or []):
            findings.append(
                _f(f"education {edu_id}", f"Excluded from {job_type} resumes.")
            )

    names = {s["name"] for s in master["skills"]}
    for skill in selection["skills"]:
        if skill in removed:
            findings.append(_f(f"skills {skill!r}", "The owner removed this skill."))
        elif skill not in names:
            findings.append(
                _f(f"skills {skill!r}", "Not an exact master.yaml skill name.")
            )

    ids = [p["id"] for p in selection["work"] + selection["projects"]]
    for dup in {i for i in ids if ids.count(i) > 1}:
        findings.append(_f(dup, "Entry is listed twice."))
    return findings
