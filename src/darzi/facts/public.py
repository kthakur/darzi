"""The public copy of master.yaml and the privacy scan (PV-1 to PV-4).

`public_copy` drops every private part (PV-1). `privacy_findings` scans any data
(the public copy, an export) for private patterns (PV-3). Only the public copy may
go to Resume Matcher or to an AI model (PV-4).
"""

import copy
import re
from collections.abc import Iterator
from typing import Any

from darzi.facts.patterns import find_regex, find_terms
from darzi.facts.validate import Finding

PUBLIC_SCHEMA_VERSION = 1
DROP_TOP = ("source_files",)
DROP_PROFILE = (
    "notes",
    "residence",
    "experience_claims",
    "experience_claims_policy",
    "availability_notes",
    "contact_sources",
    "contact_verified",
)
ENTRY_SECTIONS = ("roles", "projects")


def public_copy(master: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    """Return master.yaml without its private parts (PV-1).

    Dropped: all `notes`, `source_files`, the private profile fields, every email
    and phone except the public pair, items with `exclude_from: [all]`, removed and
    doubtful items. An entry with a display name shows it as its `org` (or
    `institution`): the long `org` is a description for the owner, not a public name.
    `profile.computing_background` stays (cover letters may use it).
    """
    data = copy.deepcopy(master)
    for key in DROP_TOP:
        data.pop(key, None)
    profile: dict[str, Any] = data.get("profile") or {}
    for key in DROP_PROFILE:
        profile.pop(key, None)
    public = rules["public_contact"]
    profile["email"], profile["phone"] = public["email"], public["phone"]

    removed_terms = list(rules["removed_items"]["terms"]) + list(
        rules["doubtful_items"]["terms"]
    )
    doubtful_bullets = set(rules["doubtful_items"].get("bullet_ids") or [])
    for section in ENTRY_SECTIONS:
        kept: list[dict[str, Any]] = []
        for entry in data.get(section) or []:
            if _excluded(entry):
                continue
            entry.pop("notes", None)
            if entry.get("display_org"):
                entry["org"] = entry["display_org"]
            entry["bullets"] = [
                b
                for b in entry.get("bullets") or []
                if not _excluded(b)
                and b.get("id") not in doubtful_bullets
                and not find_terms(str(b.get("text") or ""), removed_terms)
            ]
            kept.append(entry)
        data[section] = kept
    education: list[dict[str, Any]] = []
    for edu in data.get("education") or []:
        if _excluded(edu):
            continue
        edu.pop("notes", None)
        if edu.get("display_institution"):
            edu["institution"] = edu["display_institution"]
        education.append(edu)
    data["education"] = education
    removed_skills = set(rules["removed_items"].get("skills") or [])
    data["skills"] = [
        s
        for s in data.get("skills") or []
        if s.get("name") not in removed_skills
        and not find_terms(str(s.get("name") or ""), removed_terms)
    ]
    return {"schema_version": PUBLIC_SCHEMA_VERSION, **data}


def _excluded(item: dict[str, Any]) -> bool:
    return "all" in (item.get("exclude_from") or [])


def strings(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Yield (path, text) for every string in `node`; list items show their id."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from strings(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            label: Any = i
            if isinstance(value, dict):
                label = value.get("id") or value.get("name") or i
            yield from strings(value, f"{path}[{label}]")
    elif isinstance(node, str):
        yield path, node


def privacy_findings(data: Any, rules: dict[str, Any]) -> list[Finding]:
    """Return every private pattern found in `data` (PV-3; R-1 contact rules)."""
    public = rules["public_contact"]
    contact = rules["private_contact_patterns"]
    privacy = rules["privacy_patterns"]
    allowed = set(privacy.get("allowed_bullet_ids") or [])
    email, phone = public["email"].lower(), re.sub(r"\D", "", public["phone"])
    findings: list[Finding] = []

    def add(rule: dict[str, Any], where: str, what: str, text: str) -> None:
        findings.append(
            Finding(
                rule["id"],
                rule["severity"],
                where,
                f"Private data: {what}. Remove it before it leaves the Mac.",
                text,
            )
        )

    for where, text in strings(data):
        for m in find_terms(text, contact["terms"]):
            add(contact, where, f"private contact {m.text!r}", text)
        for m in find_regex(text, [contact["regex"]["any_email"]]):
            if m.text.lower() != email and not find_terms(m.text, contact["terms"]):
                add(contact, where, f"email {m.text!r}", text)
        for m in find_regex(text, [contact["regex"]["any_phone"]]):
            if re.sub(r"\D", "", m.text)[-10:] != phone[-10:] and not find_terms(
                m.text, contact["terms"]
            ):
                add(contact, where, f"phone number {m.text!r}", text)
        for name, pattern in privacy["regex"].items():
            for m in re.finditer(pattern, text):
                add(privacy, where, f"{name.replace('_', ' ')} {m.group(0)!r}", text)
        if not any(f"[{bullet_id}]" in where for bullet_id in allowed):
            for m in find_terms(text, privacy["terms"]):
                add(privacy, where, f"family word {m.text!r}", text)
    return findings


def query(
    public: dict[str, Any], tags: list[str], text: str | None = None
) -> list[dict[str, Any]]:
    """Return public bullets that have every tag in `tags` and contain `text`."""
    wanted = {t.strip() for t in tags if t.strip()}
    needle = (text or "").lower()
    out: list[dict[str, Any]] = []
    for section in ENTRY_SECTIONS:
        for entry in public.get(section) or []:
            for bullet in entry.get("bullets") or []:
                body = " ".join(str(bullet.get("text") or "").split())
                if wanted - set(bullet.get("tags") or []):
                    continue
                if needle and needle not in body.lower():
                    continue
                out.append(
                    {
                        "id": bullet["id"],
                        "entry": entry["id"],
                        "org": entry.get("org"),
                        "tags": bullet.get("tags") or [],
                        "verified": bullet.get("verified") is True,
                        "text": body,
                    }
                )
    return out
