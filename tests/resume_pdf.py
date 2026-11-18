"""The text lines of a resume as the Resume Matcher PDF template shows them.

`pdf_lines()` gives the lines for `tests/pdf_fakes.py`. The round-trip tests
and the finalize tests use it to make a fake PDF from approved content.
"""

from __future__ import annotations

from typing import Any

from darzi.render.word import section_order
from darzi.runs.roundtrip import template_date, template_link

Resume = dict[str, Any]


def pdf_lines(resume: Resume) -> list[str]:
    """Return the text lines of the resume as the Resume Matcher template shows it."""
    info = resume.get("personalInfo") or {}
    lines = [str(info["name"]).upper(), str(info.get("title", "")).upper()]
    contact = [info.get(k) for k in ("email", "phone", "location")]
    contact += [
        template_link(info[k]) for k in ("website", "linkedin", "github") if info.get(k)
    ]
    lines.append(", ".join(str(c) for c in contact if c))
    for key, label in section_order(resume):
        lines.append(label.upper())
        if key == "summary":
            lines.append(resume["summary"])
        elif key == "workExperience":
            entries = resume["workExperience"]
            for e in (e for e in entries if not e.get("condensed")):
                lines += [e["title"], template_date(e["years"]), e["company"]]
                lines += [e["location"]] + [
                    f"\u2022 {strip(d)}" for d in e["description"]
                ]
            old = [e for e in entries if e.get("condensed")]
            if old:
                lines.append("Earlier experience")
            for e in old:
                lines.append(
                    f"{e['title']}, {e['company']}, {template_date(e['years'])}"
                )
        elif key == "personalProjects":
            for e in resume["personalProjects"]:
                lines += [e["name"], template_date(e["years"]), e["role"]]
                lines += [f"\u2022 {strip(d)}" for d in e["description"]]
        elif key == "education":
            for e in resume["education"]:
                lines += [e["institution"], template_date(e["years"]), e["degree"]]
                lines += [e["description"]] if e.get("description") else []
        elif key == "additional":
            extra = resume["additional"]
            for name, field in (
                ("Technical Skills:", "technicalSkills"),
                ("Languages:", "languages"),
                ("Certifications:", "certificationsTraining"),
                ("Awards:", "awards"),
            ):
                if extra.get(field):
                    lines.append(f"{name} {', '.join(extra[field])}")
    return lines


def strip(text: str) -> str:
    """Remove the <strong> tags, as the PDF template does."""
    return text.replace("<strong>", "").replace("</strong>", "")
