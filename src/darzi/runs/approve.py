"""`facts approve`: the owner approves a selection or a run (docs/SPEC.md section 7).

The review part is a port: `selection_text()`, `run_text()`, `unreviewable()`
and `safe()` give the same checks and the same review text as the source. The
approval part is new: `record_and_sign()` writes a JSON record and signs it with
the owner's SSH key, and `run_is_approved()` asks `verify_approval()` of
`darzi.runs.signing`, the one function that decides "approved".

`facts approve` refuses when stdin is not a terminal. That test is not the real
gate: the real gate is the passphrase of the owner's key.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

from darzi.config import Approval
from darzi.facts.check import RunFiles
from darzi.facts.repo import InputError
from darzi.paths import is_file
from darzi.runs.runs import APPROVAL, content_hash
from darzi.runs.signing import (
    Verdict,
    key_path,
    record_bytes,
    run_target,
    settings_problem,
    sig_path,
    sign_record,
    verify_approval,
)

Ask = Callable[[str], str]


def require_terminal(is_tty: bool) -> None:
    """Raise InputError when stdin is not a terminal."""
    if not is_tty:
        raise InputError(
            "stdin is not a terminal. Only the owner approves, "
            "and the owner runs this command at a terminal."
        )


def confirm(ask: Ask, what: str) -> bool:
    """Ask the owner; return True only for the answer "y" or "yes"."""
    answer = ask(f"Approve {what}? Type y to approve. Any other answer stops: ")
    return answer.strip().lower() in ("y", "yes")


# --- Selection ----------------------------------------------------------------


def selection_text(selection: dict[str, Any], master: dict[str, Any]) -> str:
    """Return the review text of a selection: each fact that it uses."""
    entries = {e["id"]: e for e in master["roles"] + master["projects"]}
    lines = [
        f"Selection: {selection['type']} (tier {selection['tier']}), max "
        f"{selection['max_pages']} pages",
        f"Headline: {selection['headline']}",
        "",
    ]
    for section in ("projects", "work"):
        lines.append(section.upper())
        for pick in selection[section]:
            entry = entries[pick["id"]]
            org = entry.get("display_org") or entry["org"]
            flag = " (condensed)" if pick.get("condensed") else ""
            lines.append(
                f"- {entry['title']} | {org} | {entry['start']} \u2013 "
                f"{entry['end']}{flag}"
            )
            bullets = {b["id"]: b for b in entry["bullets"]}
            for bullet_id in pick["bullets"]:
                bullet = bullets[bullet_id]
                mark = "" if bullet.get("verified") is True else "  [not verified]"
                lines.append(f"    * {' '.join(bullet['text'].split())}{mark}")
        lines.append("")
    education = {e["id"]: e for e in master["education"]}
    lines.append("EDUCATION")
    for edu_id in selection["education"]:
        edu = education[edu_id]
        school = edu.get("display_institution") or edu["institution"]
        lines.append(f"- {edu['credential']} | {school}")
    lines += ["", "SKILLS", ", ".join(selection["skills"])]
    return safe("\n".join(lines))


# --- Run ----------------------------------------------------------------------

# The keys that the review text shows. A run with any other key is refused, so
# the hash never covers text that the owner did not see. descriptionStyles and
# sectionMeta are layout data of Resume Matcher; the review shows the display
# names of sectionMeta.
TOP_KEYS = {
    "personalInfo",
    "summary",
    "workExperience",
    "personalProjects",
    "education",
    "additional",
    "sectionMeta",
    "customSections",
}
INFO_KEYS = (
    "name",
    "title",
    "email",
    "phone",
    "location",
    "website",
    "linkedin",
    "github",
)
ENTRY_KEYS = (
    "title",
    "name",
    "company",
    "role",
    "location",
    "years",
    "website",
    "github",
    "institution",
    "degree",
)
LAYOUT_KEYS = {"id", "description", "descriptionStyles", "condensed"}
ADDITIONAL_KEYS = ("technicalSkills", "languages", "certificationsTraining", "awards")
META_KEYS = {
    "id",
    "key",
    "displayName",
    "sectionType",
    "isDefault",
    "isVisible",
    "order",
}
SECTION_TYPES = {"personalInfo", "text", "itemList", "stringList"}
# The description styles of the Resume Matcher schema.
DESCRIPTION_STYLES = {"bullet", "plain"}
# The mark for a line break inside a one-line field.
LINE_BREAK = " \u23ce "
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Allow-list: letters, numbers, punctuation, symbols, normal spaces and combining
# accents. Everything else (format, separators, private use, unassigned) is shown as
# "?". Some allowed-category characters still render as blank; they are listed too.
VISIBLE = ("L", "N", "P", "S")
BLANK_LOOKING = {"\u115f", "\u1160", "\u3164", "\uffa0", "\u2800"}


def _shown(c: str) -> bool:
    if c in "\n\t":
        return True
    if c in BLANK_LOOKING:
        return False
    code = ord(c)
    if 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF:  # variation selectors
        return False
    category = unicodedata.category(c)
    return category == "Zs" or category in ("Mn", "Mc") or category.startswith(VISIBLE)


def safe(text: str) -> str:
    """Show only visible characters; replace every other character with '?'.

    Covers terminal escape codes, bidirectional overrides, zero-width and other
    invisible characters, which could hide or reorder text on the review screen.
    """
    text = CONTROL.sub("?", text)
    return "".join(c if _shown(c) else "?" for c in text)


def one_line(value: Any) -> str:
    """Return a one-line field with each line break shown as a visible mark."""
    return re.sub(r"\s*\n\s*", LINE_BREAK, str(value))


def unreviewable(tailored: dict[str, Any]) -> list[str]:
    """Return each part of tailored.json that the review text does not show."""
    problems = [f"top-level key {k!r}" for k in tailored if k not in TOP_KEYS]
    info: Any = tailored.get("personalInfo") or {}
    problems += [f"personalInfo.{k}" for k in info if k not in INFO_KEYS]
    for section in ("workExperience", "personalProjects", "education"):
        for i, entry in enumerate(tailored.get(section) or []):
            problems += [
                f"{section}[{i}].{k}"
                for k in entry
                if k not in ENTRY_KEYS and k not in LAYOUT_KEYS
            ]
    additional: Any = tailored.get("additional") or {}
    problems += [f"additional.{k}" for k in additional if k not in ADDITIONAL_KEYS]
    if tailored.get("customSections"):
        problems.append("customSections (not empty)")
    problems += _wrong_types(tailored)
    for i, meta in enumerate(tailored.get("sectionMeta") or []):
        problems += [f"sectionMeta[{i}].{k}" for k in meta if k not in META_KEYS]
        if meta.get("sectionType") not in SECTION_TYPES:
            problems.append(f"sectionMeta[{i}].sectionType {meta.get('sectionType')!r}")
        if meta.get("isVisible") is not True:
            problems.append(f"sectionMeta[{i}] is hidden (isVisible is not true)")
    for section in ("workExperience", "personalProjects", "education"):
        for i, entry in enumerate(tailored.get(section) or []):
            for style in entry.get("descriptionStyles") or []:
                if style not in DESCRIPTION_STYLES:
                    problems.append(f"{section}[{i}].descriptionStyles {style!r}")
    return problems


def _text_or_none(value: Any) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _wrong_types(tailored: dict[str, Any]) -> list[str]:
    """Return each shown field that is not text, and each list not of text."""
    problems: list[str] = []
    if not isinstance(tailored.get("summary"), str | None):
        problems.append("summary is not text")
    info: Any = tailored.get("personalInfo") or {}
    problems += [
        f"personalInfo.{k} is not text"
        for k in INFO_KEYS
        if not _text_or_none(info.get(k))
    ]
    for section in ("workExperience", "personalProjects", "education"):
        for i, entry in enumerate(tailored.get(section) or []):
            problems += [
                f"{section}[{i}].{k} is not text"
                for k in ENTRY_KEYS
                if not _text_or_none(entry.get(k))
            ]
            description: Any = entry.get("description")
            if description is None or isinstance(description, str):
                continue
            if not isinstance(description, list) or not all(
                isinstance(d, str) for d in description
            ):
                problems.append(f"{section}[{i}].description is not a list of text")
    additional: Any = tailored.get("additional") or {}
    for key in ADDITIONAL_KEYS:
        items: Any = additional.get(key)
        if items is not None and (
            not isinstance(items, list) or not all(isinstance(x, str) for x in items)
        ):
            problems.append(f"additional.{key} is not a list of text")
    return problems


def run_text(run: RunFiles) -> str:
    """Return the review text of a run: each shown field and the cover letter."""
    r = run.tailored
    info: Any = r.get("personalInfo") or {}
    lines = ["CONTACT"] + [
        f"  {k}: {one_line(info[k])}" for k in INFO_KEYS if info.get(k)
    ]
    names = [
        m.get("displayName") for m in r.get("sectionMeta") or [] if m.get("displayName")
    ]
    if names:
        lines.append("SECTIONS: " + ", ".join(one_line(n) for n in names))
    lines += ["", "SUMMARY", str(r.get("summary") or ""), ""]
    for section, label in (
        ("personalProjects", "PROJECTS"),
        ("workExperience", "EXPERIENCE"),
        ("education", "EDUCATION"),
    ):
        lines.append(label)
        for e in r.get(section) or []:
            head = [one_line(e[k]) for k in ENTRY_KEYS if e.get(k)]
            lines.append("- " + " | ".join(head))
            description: Any = e.get("description") or []
            if isinstance(description, str):
                description = [description]
            lines += [f"    * {one_line(d)}" for d in description]
        lines.append("")
    additional: Any = r.get("additional") or {}
    for key in ADDITIONAL_KEYS:
        if additional.get(key):
            lines += [key.upper(), ", ".join(one_line(x) for x in additional[key]), ""]
    if run.letter:
        lines += ["COVER LETTER", run.letter.rstrip()]
    return safe("\n".join(lines))


def run_is_approved(
    repo: Path, settings: Approval, run_dir: Path, run: RunFiles
) -> Verdict:
    """Return the verdict of `verify_approval()` for the run folder `run_dir`."""
    return verify_approval(
        repo,
        settings,
        "run",
        run_target(run_dir),
        run_dir / APPROVAL,
        content_hash(run.tailored, run.letter),
        target_path=run_dir,
    )


# --- The signed record --------------------------------------------------------


def sign_problem(repo: Path, settings: Approval) -> str:
    """Return why `record_and_sign()` cannot sign now, or "" when it can."""
    problem = settings_problem(repo, settings)
    if problem:
        return problem
    key = key_path(settings)
    if key is None:
        return "facts.toml has no usable [approval] key"
    try:
        if not is_file(key):
            return f"the [approval] key file {key} does not exist"
    except OSError as exc:
        return f"the [approval] key file {key} cannot be read: {exc.strerror}"
    return ""


def _remove(*paths: Path) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def _clean_up(*paths: Path) -> None:
    """Remove `paths` after a failed step. A file that stays is not approved."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def record_and_sign(
    repo: Path,
    settings: Approval,
    kind: str,
    target: str,
    record: Path,
    digest: str,
    day: dt.date,
    *,
    target_path: Path,
) -> Verdict:
    """Write a new approval record, sign it, and verify the new pair.

    The old record and the old signature are removed first: ssh-keygen keeps an
    old signature file when it cannot ask to replace it. After the sign call the
    record is read again and must have the bytes that this function wrote, and
    `verify_approval()` must approve the pair. If a step fails, the record and
    the signature are removed, and InputError is raised.
    """
    problem = sign_problem(repo, settings)
    key = key_path(settings)
    if problem or key is None or settings.signer is None or settings.namespace is None:
        raise InputError(f"cannot sign: {problem or 'the settings are not complete'}.")
    sig = sig_path(record)
    data = record_bytes(kind, target, day, digest, settings.signer)
    try:
        _remove(record, sig)
        with record.open("xb") as f:
            f.write(data)
    except OSError as exc:
        _clean_up(record, sig)
        raise InputError(
            f"cannot write {record}: {exc.strerror}. Check the permissions."
        ) from exc
    if not sign_record(key, settings.namespace, record):
        _clean_up(record, sig)
        raise InputError("ssh-keygen did not sign the record. Nothing is approved.")
    try:
        written = record.read_bytes()
    except OSError:
        written = b""
    verdict = verify_approval(
        repo, settings, kind, target, record, digest, target_path=target_path
    )
    if written != data or not verdict.approved:
        _clean_up(record, sig)
        reason = verdict.reason if written == data else "the record changed"
        raise InputError(
            f"the new signature does not verify ({reason}). Nothing is approved."
        )
    return verdict
