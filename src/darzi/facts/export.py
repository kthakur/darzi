"""`facts export`: a selection -> Resume Matcher base resume and its map.

It builds only from the public copy (PV-2), uses display names, copies every
text exactly, and refuses output that the privacy scan flags (PV-3). The CLI
writes base.json and base.map.json into the run folder (D-32).
"""

import re
from typing import Any

from darzi.facts.public import privacy_findings
from darzi.facts.repo import InputError

MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
SECTION_TYPES = {
    "personalInfo": "personalInfo",
    "summary": "text",
    "additional": "stringList",
}
MAP_SCHEMA_VERSION = 1


def fmt_date(value: Any) -> str:
    """Format YYYY-MM as 'Mon YYYY', keep YYYY, and 'present' as 'Present'."""
    text = str(value or "")
    if text == "present":
        return "Present"
    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if match:
        return f"{MONTHS[int(match.group(2)) - 1]} {match.group(1)}"
    return text


def fmt_years(entry: dict[str, Any]) -> str:
    """Format the date range of an entry ('Mar 2012 - Nov 2014', 'Aug 2019')."""
    start, end = fmt_date(entry.get("start")), fmt_date(entry.get("end"))
    if start and end and start != end:
        return f"{start} - {end}"
    return start or end


def _text(bullet: dict[str, Any]) -> str:
    return " ".join(str(bullet.get("text") or "").split())


def _bullets(entry: dict[str, Any], ids: list[str]) -> list[str]:
    by_id = {b["id"]: b for b in entry.get("bullets") or []}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise InputError(
            f"{entry['id']}: bullets {missing} are not in the public copy "
            "(removed, hidden or wrong ID). Fix the selection."
        )
    return [_text(by_id[i]) for i in ids]


def build_base(
    public: dict[str, Any], selection: dict[str, Any], selection_path: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (base resume, base map) for a selection, from the public copy."""
    entries = {e["id"]: e for e in public["roles"] + public["projects"]}
    education = {e["id"]: e for e in public["education"]}
    profile = public["profile"]
    the_map: list[dict[str, Any]] = []

    work: list[dict[str, Any]] = []
    for i, pick in enumerate(selection["work"], start=1):
        entry = _entry(entries, pick["id"])
        work.append(
            {
                "id": i,
                "title": entry["title"],
                "company": entry["org"],
                "location": entry.get("location") or None,
                "years": fmt_years(entry),
                "description": _bullets(entry, pick["bullets"]),
                "condensed": bool(pick.get("condensed")),
            }
        )
        the_map.append(
            {
                "section": "workExperience",
                "rm_id": i,
                "id": entry["id"],
                "bullets": list(pick["bullets"]),
            }
        )

    projects: list[dict[str, Any]] = []
    for i, pick in enumerate(selection["projects"], start=1):
        entry = _entry(entries, pick["id"])
        independent = str(entry["org"]).startswith("Independent")
        name = entry["title"] if independent else entry["org"]
        role = entry["org"] if independent else entry["title"]
        projects.append(
            {
                "id": i,
                "name": name,
                "role": role,
                "years": fmt_years(entry),
                "description": _bullets(entry, pick["bullets"]),
            }
        )
        the_map.append(
            {
                "section": "personalProjects",
                "rm_id": i,
                "id": entry["id"],
                "bullets": list(pick["bullets"]),
            }
        )

    schools: list[dict[str, Any]] = []
    for i, edu_id in enumerate(selection["education"], start=1):
        if edu_id not in education:
            raise InputError(f"education {edu_id} is not in the public copy.")
        item = education[edu_id]
        start, end = str(item.get("start") or ""), str(item.get("end") or "")[:4]
        schools.append(
            {
                "id": i,
                "institution": item["institution"],
                "degree": str(item["credential"]).replace(" (no degree)", ""),
                "years": f"{start} - {end}" if start and start != end else end,
            }
        )
        the_map.append({"section": "education", "rm_id": i, "id": edu_id})

    links = profile.get("links") or []
    base: dict[str, Any] = {
        "personalInfo": {
            "name": profile["name"],
            "title": selection["headline"],
            "email": profile["email"],
            "phone": profile["phone"],
            "location": profile.get("location"),
            "website": links[0]["url"] if links else None,
        },
        "summary": selection["headline"] + ".",
        "workExperience": work,
        "personalProjects": projects,
        "education": schools,
        "additional": {"technicalSkills": list(selection["skills"])},
        "sectionMeta": [
            {
                "id": key,
                "key": key,
                "displayName": name,
                "sectionType": SECTION_TYPES.get(key, "itemList"),
                "isDefault": True,
                "isVisible": True,
                "order": i,
            }
            for i, (key, name) in enumerate(selection["section_order"])
        ],
        "customSections": {},
    }
    base_map = {
        "schema_version": MAP_SCHEMA_VERSION,
        "type": selection["type"],
        "selection": selection_path,
        "entries": the_map,
        "skills": list(selection["skills"]),
        "max_pages": selection["max_pages"],
        "summary_basis": list(selection.get("summary_basis") or []),
    }
    return base, base_map


def _entry(entries: dict[str, dict[str, Any]], entry_id: str) -> dict[str, Any]:
    if entry_id not in entries:
        raise InputError(f"{entry_id} is not in the public copy. Fix the selection.")
    return entries[entry_id]


def export(
    public: dict[str, Any],
    selection: dict[str, Any],
    selection_path: str,
    rules: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the base and refuse it if the privacy scan finds anything (PV-3)."""
    base, base_map = build_base(public, selection, selection_path)
    leaks = privacy_findings(base, rules)
    if leaks:
        lines = "; ".join(f"{f.location}: {f.message}" for f in leaks)
        raise InputError(f"the export holds private data: {lines}")
    return base, base_map
