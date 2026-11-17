"""Tests of the round-trip check, on Word files and on minimal PDF files."""

from __future__ import annotations

import copy
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from docx import Document
from pypdf import PdfWriter

from darzi.render.word import letter_docx, resume_docx, section_order
from darzi.runs import roundtrip
from darzi.runs.roundtrip import (
    check_letter,
    check_resume,
    letter_paragraphs,
    normalize,
    template_date,
    template_link,
)

from .pdf_fakes import pdf_bytes

EXPORT = Path(__file__).parent / "expected" / "corporate-tech.export.json"
KINDS = ("docx", "pdf")
LETTER = (
    "Dear hiring team,\n\nI design research-led products\nfor Acme.\n\n"
    "I ran 20 user interviews last year.\n\nKind regards,\nTest Person"
)

Resume = dict[str, Any]


def base() -> Resume:
    data: Any = json.loads(EXPORT.read_text(encoding="utf-8"))
    return data["base"]


def rich() -> Resume:
    """The synthetic résumé with each field that the check tests."""
    resume = base()
    info = resume["personalInfo"]
    info["linkedin"] = "www.linkedin.com/in/test-person"
    info["github"] = "github.com/test-person"
    resume["workExperience"].append(
        {
            "id": 3,
            "title": "Junior Designer",
            "company": "Initech",
            "location": "Ogdenville, ST",
            "years": "2015 2018",
            "description": ["Made print layouts for the store."],
            "condensed": True,
        }
    )
    resume["personalProjects"] = [
        {
            "id": 1,
            "name": "Pattern Library",
            "role": "Lead designer",
            "years": "Jan 2020\u2013Dec 2020",
            "description": ["Built a <strong>shared</strong> component set."],
        }
    ]
    resume["education"][0]["description"] = "Thesis on wayfinding signs."
    resume["additional"]["languages"] = ["Spanish"]
    resume["additional"]["certificationsTraining"] = ["Certified Usability Analyst"]
    resume["additional"]["awards"] = ["Design Award 2022"]
    resume["sectionMeta"].insert(
        3,
        {
            "id": "personalProjects",
            "key": "personalProjects",
            "displayName": "Projects",
            "sectionType": "itemList",
            "isDefault": True,
            "isVisible": True,
            "order": 3,
        },
    )
    for order, meta in enumerate(resume["sectionMeta"]):
        meta["order"] = order
    return resume


def pdf_lines(resume: Resume) -> list[str]:
    """Return the text lines of the résumé as the Resume Matcher template shows it."""
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
    return text.replace("<strong>", "").replace("</strong>", "")


def write(kind: str, resume: Resume, folder: Path, name: str = "resume") -> Path:
    path = folder / f"{name}.{kind}"
    if kind == "docx":
        resume_docx(resume, path)
    else:
        path.write_bytes(pdf_bytes(pdf_lines(resume)))
    return path


def locations(findings: list[roundtrip.Finding]) -> list[str]:
    return [f.location for f in findings]


# --- The three "done when" cases ---------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("make", [base, rich])
def test_a_correct_file_passes(
    kind: str, make: Callable[[], Resume], tmp_path: Path
) -> None:
    resume = make()
    assert check_resume(resume, write(kind, resume, tmp_path)) == []


@pytest.mark.parametrize("kind", KINDS)
def test_one_bullet_removed_fails(kind: str, tmp_path: Path) -> None:
    approved = rich()
    shown = copy.deepcopy(approved)
    del shown["workExperience"][0]["description"][1]
    findings = check_resume(approved, write(kind, shown, tmp_path))
    assert locations(findings) == ["workExperience[0].description[1]"]
    assert "is not in resume" in findings[0].message


@pytest.mark.parametrize("kind", KINDS)
def test_two_sections_that_change_places_fail(kind: str, tmp_path: Path) -> None:
    approved = rich()
    shown = copy.deepcopy(approved)
    meta = {m["key"]: m for m in shown["sectionMeta"]}
    meta["workExperience"]["order"], meta["education"]["order"] = (
        meta["education"]["order"],
        meta["workExperience"]["order"],
    )
    findings = check_resume(approved, write(kind, shown, tmp_path))
    assert findings
    assert all("approved order" in f.message for f in findings)


# --- Order -------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_bullets_of_one_entry_in_a_wrong_order_fail(kind: str, tmp_path: Path) -> None:
    approved = rich()
    shown = copy.deepcopy(approved)
    shown["workExperience"][0]["description"].reverse()
    findings = check_resume(approved, write(kind, shown, tmp_path))
    assert locations(findings) == ["workExperience[0].description[1]"]
    assert "approved order" in findings[0].message


@pytest.mark.parametrize("kind", KINDS)
def test_a_bullet_moved_into_another_entry_fails(kind: str, tmp_path: Path) -> None:
    approved = rich()
    shown = copy.deepcopy(approved)
    moved = shown["workExperience"][0]["description"].pop()
    shown["workExperience"][1]["description"].insert(0, moved)
    findings = check_resume(approved, write(kind, shown, tmp_path))
    # The walk finds the moved bullet in the next entry, so the title of that
    # entry is the item that is not in the approved order.
    assert locations(findings) == ["workExperience[1].title"]
    assert "approved order" in findings[0].message


@pytest.mark.parametrize("kind", KINDS)
def test_the_same_text_two_times_does_not_hide_a_missing_one(
    kind: str, tmp_path: Path
) -> None:
    approved = rich()
    same = "Wrote the research plan with the product team."
    approved["workExperience"][0]["description"].append(same)
    approved["workExperience"][1]["description"].append(same)
    shown = copy.deepcopy(approved)
    shown["workExperience"][1]["description"].pop()
    assert check_resume(approved, write(kind, approved, tmp_path, "a")) == []
    findings = check_resume(approved, write(kind, shown, tmp_path, "b"))
    assert locations(findings) == ["workExperience[1].description[1]"]


@pytest.mark.parametrize("kind", KINDS)
def test_the_same_company_two_times_does_not_hide_a_missing_one(
    kind: str, tmp_path: Path
) -> None:
    approved = rich()
    approved["workExperience"][1]["company"] = "Acme"
    shown = copy.deepcopy(approved)
    shown["workExperience"][1]["company"] = "Umbrella"
    findings = check_resume(approved, write(kind, shown, tmp_path))
    assert locations(findings) == ["workExperience[1].company"]


# --- Each item must be there -------------------------------------------------


def _drop(path: str) -> Callable[[Resume], None]:
    """Return a function that removes the value at `path` (dots and indexes)."""

    def drop(resume: Resume) -> None:
        keys: list[Any] = [int(k) if k.isdigit() else k for k in path.split(".")]
        target: Any = resume
        for key in keys[:-1]:
            target = target[key]
        if isinstance(target, list):
            del target[keys[-1]]
        else:
            target[keys[-1]] = ""

    return drop


MISSING = {
    "personalInfo.name": "personalInfo.name",
    "personalInfo.email": "personalInfo.email",
    "personalInfo.phone": "personalInfo.phone",
    "personalInfo.website": "personalInfo.website",
    "personalInfo.linkedin": "personalInfo.linkedin",
    "summary": "summary",
    "workExperience.0.title": "workExperience[0].title",
    "workExperience.0.company": "workExperience[0].company",
    "workExperience.0.years": "workExperience[0].years",
    "workExperience.2.title": "workExperience[2].title",
    "personalProjects.0.name": "personalProjects[0].name",
    "education.0.degree": "education[0].degree",
    "education.0.institution": "education[0].institution",
    "education.0.years": "education[0].years",
    "additional.technicalSkills.1": "additional.technicalSkills[1]",
    # The fields that the owner added to the section 9 list.
    "personalInfo.location": "personalInfo.location",
    "workExperience.0.location": "workExperience[0].location",
    "additional.languages.0": "additional.languages[0]",
    "additional.certificationsTraining.0": "additional.certificationsTraining[0]",
    "additional.awards.0": "additional.awards[0]",
    "education.0.description": "education[0].description",
    "personalProjects.0.role": "personalProjects[0].role",
}


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("path", sorted(MISSING))
def test_a_missing_item_fails(kind: str, path: str, tmp_path: Path) -> None:
    approved = rich()
    shown = copy.deepcopy(approved)
    _drop(path)(shown)
    findings = check_resume(approved, write(kind, shown, tmp_path))
    assert MISSING[path] in locations(findings)


@pytest.mark.parametrize("kind", KINDS)
def test_a_condensed_entry_passes_without_its_bullets(
    kind: str, tmp_path: Path
) -> None:
    approved = rich()
    approved["workExperience"][1]["condensed"] = True
    path = write(kind, approved, tmp_path)
    text = normalize(roundtrip.extract_text(path))
    assert normalize(approved["workExperience"][1]["description"][0]) not in text
    assert check_resume(approved, path) == []


# --- The match rules ---------------------------------------------------------


def test_normalize() -> None:
    assert normalize("\ufb01gma  Pro\u00adto\u00a0type\n\n x") == "figma proto type\nx"
    assert normalize("STRASSE") == normalize("Stra\u00dfe")
    assert normalize("\uff26igma") == "figma"  # a full-width letter (NFKC)


def test_a_hyphen_word_broken_after_its_hyphen_passes(tmp_path: Path) -> None:
    paragraphs = letter_paragraphs(LETTER)
    assert paragraphs[1].startswith("I design research-led ")
    lines = [paragraphs[0], "I design research-", paragraphs[1][18:], *paragraphs[2:]]
    path = tmp_path / "letter.pdf"
    path.write_bytes(pdf_bytes(lines))
    assert check_letter(LETTER, path) == []


def test_another_letter_case_passes(tmp_path: Path) -> None:
    approved = rich()
    path = tmp_path / "r.pdf"
    path.write_bytes(pdf_bytes([line.swapcase() for line in pdf_lines(approved)]))
    assert check_resume(approved, path) == []


def test_a_hyphen_at_a_line_end_passes(tmp_path: Path) -> None:
    approved = rich()
    lines = pdf_lines(approved)
    bullet = "\u2022 " + approved["workExperience"][0]["description"][0]
    at = lines.index(bullet)
    lines[at : at + 1] = [
        "\u2022 Ran 20 user inter-",
        "views to shape the account",
        "set-",
        "tings pages.",
    ]
    path = tmp_path / "r.pdf"
    path.write_bytes(pdf_bytes(lines))
    assert check_resume(approved, path) == []


def test_a_word_split_with_no_hyphen_fails(tmp_path: Path) -> None:
    approved = rich()
    lines = pdf_lines(approved)
    bullet = "\u2022 " + approved["workExperience"][0]["description"][0]
    at = lines.index(bullet)
    lines[at : at + 1] = [
        "\u2022 Ran 20 user inter",
        "views to shape the account settings pages.",
    ]
    path = tmp_path / "r.pdf"
    path.write_bytes(pdf_bytes(lines))
    assert locations(check_resume(approved, path)) == [
        "workExperience[0].description[0]"
    ]


def test_template_date() -> None:
    assert template_date("Mar 2021 - Present") == "Mar 2021 - Present"
    assert template_date("Mar 2021\u2013Present") == "Mar 2021 - Present"
    assert template_date("2015 2018") == "2015 - 2018"
    assert template_date("Jun 2025 Aug 2025") == "Jun 2025 - Aug 2025"
    assert template_date("Jun 2025 present") == "Jun 2025 - present"
    assert template_date("") == ""


def test_template_link() -> None:
    assert template_link("www.example.com/a") == "example.com/a"
    assert template_link("https://www.example.com") == "https://www.example.com"
    assert template_link("//www.example.com") == "//www.example.com"


def test_a_date_in_another_form_fails(tmp_path: Path) -> None:
    approved = rich()
    shown = copy.deepcopy(approved)
    shown["workExperience"][0]["years"] = "03/2021 - Present"
    findings = check_resume(approved, write("pdf", shown, tmp_path))
    assert locations(findings) == ["workExperience[0].years"]


def test_an_html_bullet_passes_without_its_tags_in_a_pdf(tmp_path: Path) -> None:
    approved = rich()
    bullet = approved["personalProjects"][0]["description"][0]
    assert "<strong>" in bullet
    assert check_resume(approved, write("pdf", approved, tmp_path)) == []
    # The Word file shows the tags as text; a PDF that shows them fails.
    assert check_resume(approved, write("docx", approved, tmp_path)) == []
    path = tmp_path / "tags.pdf"
    lines = pdf_lines(approved)
    lines[lines.index("\u2022 " + strip(bullet))] = "\u2022 " + bullet
    path.write_bytes(pdf_bytes(lines))
    assert locations(check_resume(approved, path)) == [
        "personalProjects[0].description[0]"
    ]


def test_a_short_item_must_stand_alone(tmp_path: Path) -> None:
    approved = rich()
    approved["additional"]["technicalSkills"].append("Go")
    shown = copy.deepcopy(approved)
    shown["additional"]["technicalSkills"][-1] = "Google Docs"
    findings = check_resume(approved, write("pdf", shown, tmp_path))
    assert locations(findings) == ["additional.technicalSkills[3]"]
    assert check_resume(approved, write("pdf", approved, tmp_path, "ok")) == []


def test_an_empty_item_is_skipped(tmp_path: Path) -> None:
    approved = rich()
    approved["additional"]["technicalSkills"].append("  ")
    assert check_resume(approved, write("pdf", approved, tmp_path)) == []


def test_the_order_rule_agrees_with_the_word_file(tmp_path: Path) -> None:
    resume = rich()
    meta = {m["key"]: m for m in resume["sectionMeta"]}
    meta["education"]["order"], meta["summary"]["order"] = 1, 4
    path = write("docx", resume, tmp_path)
    labels = {label.upper() for _key, label in section_order(resume)}
    headings = [p.text for p in Document(str(path)).paragraphs if p.text in labels]
    assert headings == [label.upper() for _key, label in section_order(resume)]
    assert check_resume(resume, path) == []


# --- Files that cannot be read -----------------------------------------------


def _encrypted_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    writer.encrypt("secret", algorithm="RC4-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


UNREADABLE: dict[str, tuple[str, Callable[[], bytes], str]] = {
    "empty pdf": ("r.pdf", lambda: b"", "is empty"),
    "empty docx": ("r.docx", lambda: b"", "is empty"),
    "damaged pdf": ("r.pdf", lambda: b"%PDF-1.4\n\x00\x01 not a pdf", "PDF"),
    "damaged docx": ("r.docx", lambda: b"PK\x03\x04 not a zip", "Word file"),
    "encrypted pdf": ("r.pdf", _encrypted_pdf, "is encrypted"),
    "pdf with no text": ("r.pdf", _blank_pdf, "no text"),
    "word file named pdf": ("r.pdf", lambda: b"PK\x03\x04", "is not a PDF"),
    "pdf named docx": ("r.docx", lambda: pdf_bytes(["x"]), "is not a Word"),
    "text file": ("r.txt", lambda: b"Test Person", "is not a .pdf or .docx"),
    "pdf of spaces": ("r.pdf", lambda: pdf_bytes(["   "]), "no text"),
}


@pytest.mark.parametrize("case", sorted(UNREADABLE))
def test_a_file_that_cannot_be_read_fails(case: str, tmp_path: Path) -> None:
    name, make, reason = UNREADABLE[case]
    path = tmp_path / name
    path.write_bytes(make())
    for findings in (check_resume(rich(), path), check_letter(LETTER, path)):
        assert len(findings) == 1
        assert findings[0].location == name
        assert reason in findings[0].message


def test_an_absent_file_fails(tmp_path: Path) -> None:
    findings = check_resume(rich(), tmp_path / "none.pdf")
    assert len(findings) == 1 and "cannot be read" in findings[0].message


# --- Cover letter ------------------------------------------------------------


def _letter_file(kind: str, paragraphs: list[str], folder: Path) -> Path:
    path = folder / f"letter.{kind}"
    if kind == "docx":
        letter_docx("\n\n".join(paragraphs), path)
    else:
        path.write_bytes(pdf_bytes(paragraphs))
    return path


def test_letter_paragraphs_agree_with_the_word_file(tmp_path: Path) -> None:
    path = tmp_path / "l.docx"
    letter_docx(LETTER, path)
    shown = [p.text for p in Document(str(path)).paragraphs]
    assert letter_paragraphs(LETTER) == shown


@pytest.mark.parametrize("kind", KINDS)
def test_the_cover_letter(kind: str, tmp_path: Path) -> None:
    paragraphs = letter_paragraphs(LETTER)
    assert check_letter(LETTER, _letter_file(kind, paragraphs, tmp_path)) == []
    removed = paragraphs[:2] + paragraphs[3:]
    findings = check_letter(LETTER, _letter_file(kind, removed, tmp_path))
    assert locations(findings) == ["letter.paragraph[2]"]
    swapped = [paragraphs[0], paragraphs[2], paragraphs[1], paragraphs[3]]
    findings = check_letter(LETTER, _letter_file(kind, swapped, tmp_path))
    assert locations(findings) == ["letter.paragraph[2]"]
    assert "approved order" in findings[0].message
