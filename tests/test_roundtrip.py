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
    MAX_OPTIONS,
    check_letter,
    check_resume,
    extract_text,
    letter_paragraphs,
    normalize,
    place,
    resume_parts,
    template_date,
    template_link,
    walk,
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


# --- One place for each item, and the ends of a match --------------------------

Change = Callable[[Resume], None]


def _put(path: str, value: Any) -> Change:
    """Return a function that sets the value at `path` (dots and indexes)."""

    def put(resume: Resume) -> None:
        keys: list[Any] = [int(k) if k.isdigit() else k for k in path.split(".")]
        target: Any = resume
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value

    return put


BULLET = "workExperience.0.description.0"
ACME_BULLET = _put(BULLET, "Ran 20 user interviews for Acme on the settings pages.")
YEAR = [
    _put("workExperience.0.years", "2021"),
    _put(BULLET, "Ran 20 user interviews in 2021."),
]


def _skill(approved: str, shown: str | None) -> tuple[list[Change], list[Change], str]:
    """Return a case: a first skill `approved`, shown as `shown` (None: absent)."""

    def show(resume: Resume) -> None:
        skills = resume["additional"]["technicalSkills"]
        if shown is None:
            skills.pop(0)
        else:
            skills[0] = shown

    def add(resume: Resume) -> None:
        resume["additional"]["technicalSkills"].insert(0, approved)

    return [add], [show], "additional.technicalSkills[0]"


GO_SKILL: list[Change] = [
    lambda r: r["additional"]["technicalSkills"].insert(0, "Go"),
    lambda r: r["sectionMeta"][-1].update(displayName="Go-To Skills"),
]

# Each case: the changes to the approved résumé, the changes to the file only,
# and the item that must have a finding. Each file holds the approved text of
# that item only inside a different item or inside a longer value.
WRONG: dict[str, tuple[list[Change], list[Change], str]] = {
    "company named in a bullet": (
        [ACME_BULLET],
        [_put("workExperience.0.company", "")],
        "workExperience[0].company",
    ),
    "company inside the location": (
        [_put("workExperience.0.company", "Springfield")],
        [_put("workExperience.0.company", "")],
        "workExperience[0].company",
    ),
    "company at the end of a longer word": (
        [],
        [_put("workExperience.0.company", "Megaacme")],
        "workExperience[0].company",
    ),
    "year in a bullet": (
        YEAR,
        [_put("workExperience.0.years", "")],
        "workExperience[0].years",
    ),
    "title inside a longer word": (
        [
            _put("workExperience.0.title", "Strategist"),
            _put("workExperience.0.company", "Strategists Guild"),
        ],
        [_put("workExperience.0.title", "")],
        "workExperience[0].title",
    ),
    "role inside the project name": (
        [_put("personalProjects.0.name", "Lead Designer Toolkit")],
        [_put("personalProjects.0.role", "")],
        "personalProjects[0].role",
    ),
    "skill inside a section heading": (
        GO_SKILL,
        [lambda r: r["additional"]["technicalSkills"].pop(0)],
        "additional.technicalSkills[0]",
    ),
    "phone with one more digit": (
        [],
        [_put("personalInfo.phone", "555-01001")],
        "personalInfo.phone",
    ),
    "phone with more digits after a dash": (
        [],
        [_put("personalInfo.phone", "555-0100-7")],
        "personalInfo.phone",
    ),
    "phone with more digits in front": (
        [],
        [_put("personalInfo.phone", "1-555-0100")],
        "personalInfo.phone",
    ),
    "email with a longer domain": (
        [],
        # Two strings, so that the privacy gate sees no address at a longer domain.
        [_put("personalInfo.email", "test.person@example.com" + ".example.org")],
        "personalInfo.email",
    ),
    "email with a dash and more text after the domain": (
        [],
        [_put("personalInfo.email", "test.person@example.com-mail")],
        "personalInfo.email",
    ),
    "email with more text in front": (
        [],
        [_put("personalInfo.email", "x.test.person@example.com")],
        "personalInfo.email",
    ),
    "link with a longer last part": (
        [],
        [_put("personalInfo.github", "github.com/test-person-2")],
        "personalInfo.github",
    ),
    "link with one more path part": (
        [],
        [_put("personalInfo.github", "github.com/test-person/old")],
        "personalInfo.github",
    ),
    "month shown as the start of a range": (
        [_put("personalProjects.0.years", "Jan 2020")],
        [_put("personalProjects.0.years", "Jan 2020 - Dec 2020")],
        "personalProjects[0].years",
    ),
    "month shown as the end of a range": (
        [_put("personalProjects.0.years", "Jan 2020")],
        [_put("personalProjects.0.years", "Dec 2019 - Jan 2020")],
        "personalProjects[0].years",
    ),
    # A sign that joins more text to the approved value makes a longer token.
    "one-letter skill shown with a plus sign": _skill("C", "C+"),
    "one-letter skill shown with a hash": _skill("C", "C#"),
    "one-letter skill shown with two plus signs": _skill("C", "C++"),
    "skill with a leading dot shown with letters in front": _skill(".NET", "ASP.NET"),
    "skill with slashes shown with one more part": _skill("CI/CD", "CI/CD/CT"),
    "short skill shown with a hyphen and more letters": _skill("Go", "Go-kit"),
    "skill with two plus signs not shown": _skill("C++", None),
    "skill with a leading dot not shown": _skill(".NET", None),
    "company shown with a hyphen and a second word": (
        [],
        [_put("workExperience.0.company", "Acme-Widgets")],
        "workExperience[0].company",
    ),
    "year shown with a month in front": (
        [_put("workExperience.0.years", "2021")],
        [_put("workExperience.0.years", "Mar 2021")],
        "workExperience[0].years",
    ),
    "year shown with a slash and two more digits": (
        [_put("workExperience.0.years", "2021")],
        [_put("workExperience.0.years", "2021/22")],
        "workExperience[0].years",
    ),
    "month shown as the start of a range with a range word": (
        [_put("personalProjects.0.years", "Jan 2020")],
        [_put("personalProjects.0.years", "Jan 2020 to Dec 2020")],
        "personalProjects[0].years",
    ),
    "month shown as the end of a range with a range word": (
        [_put("personalProjects.0.years", "Jan 2020")],
        [_put("personalProjects.0.years", "Dec 2019 until Jan 2020")],
        "personalProjects[0].years",
    ),
    "email with a part and a plus sign in front": (
        [],
        [_put("personalInfo.email", "x+" + "test.person@example.com")],
        "personalInfo.email",
    ),
    "link with a query after it": (
        [],
        [_put("personalInfo.github", "github.com/test-person?tab=repos")],
        "personalInfo.github",
    ),
    "phone with a plus sign and a country code in front": (
        [],
        # Two strings, so that the privacy gate sees no other phone number.
        [_put("personalInfo.phone", "+1 " + "555-0100")],
        "personalInfo.phone",
    ),
    # The Word file has the heading "Certifications and training".
    "certification that is a word of its heading": (
        [_put("additional.certificationsTraining", ["Training", "Usability Analyst"])],
        [_put("additional.certificationsTraining", ["Usability Analyst"])],
        "additional.certificationsTraining[0]",
    ),
}


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", sorted(WRONG))
def test_an_item_only_inside_other_text_fails(
    kind: str, case: str, tmp_path: Path
) -> None:
    approved_changes, shown_changes, location = WRONG[case]
    approved = rich()
    for change in approved_changes:
        change(approved)
    shown = copy.deepcopy(approved)
    for change in shown_changes:
        change(shown)
    assert location in locations(check_resume(approved, write(kind, shown, tmp_path)))


# Each case: the changes to the approved résumé (the file shows it as approved),
# then the changes to the file only, in a form that the check accepts.
RIGHT: dict[str, tuple[list[Change], list[Change]]] = {
    "company also named in a bullet": ([ACME_BULLET], []),
    "company also inside the location": (
        [_put("workExperience.0.company", "Springfield")],
        [],
    ),
    "year also in a bullet": (YEAR, []),
    "role also inside the project name": (
        [_put("personalProjects.0.name", "Lead Designer Toolkit")],
        [],
    ),
    "skill also inside a section heading": (GO_SKILL, []),
    "a date range approved as a range": (
        [_put("personalProjects.0.years", "Jan 2020 - Dec 2020")],
        [],
    ),
    "email before a sentence dot": (
        [],
        [_put("personalInfo.email", "test.person@example.com.")],
    ),
    "link with a final slash": (
        [],
        [_put("personalInfo.github", "github.com/test-person/")],
    ),
    "link with https in front": (
        [],
        [_put("personalInfo.github", "https://github.com/test-person")],
    ),
    "link with www in front": (
        [],
        [_put("personalInfo.github", "www.github.com/test-person")],
    ),
    "skills with signs shown exactly": (
        [
            _put(
                "additional.technicalSkills", ["C++", "C#", ".NET", "CI/CD", "R&D", "C"]
            )
        ],
        [],
    ),
    "two skills shown with a slash and spaces": (
        [_put("additional.technicalSkills", ["Figma", "C", "C++"])],
        [_put("additional.technicalSkills", ["Figma", "C / C++"])],
    ),
    "company before a sentence dot": ([], [_put("workExperience.0.company", "Acme.")]),
    "company in brackets": ([], [_put("workExperience.0.company", "(Acme)")]),
    "company in quotes": ([], [_put("workExperience.0.company", "'Acme'")]),
    "company before a comma": ([], [_put("workExperience.0.company", "Acme,")]),
    # A heading must be a full line: the word of the next heading in a condensed
    # entry is not that heading.
    "company holds the word of the next heading": (
        [_put("workExperience.2.company", "Initech Projects")],
        [],
    ),
    "education text starts with the word of the next heading": (
        [_put("education.0.description", "Skills workshop for new staff.")],
        [],
    ),
    "company holds the word of the next heading at a line end": (
        [
            _put("workExperience.2.company", "Initech Projects"),
            _put("workExperience.2.years", ""),
        ],
        [],
    ),
}


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", sorted(RIGHT))
def test_an_item_in_its_own_place_passes(kind: str, case: str, tmp_path: Path) -> None:
    approved_changes, shown_changes = RIGHT[case]
    approved = rich()
    for change in approved_changes:
        change(approved)
    shown = copy.deepcopy(approved)
    for change in shown_changes:
        change(shown)
    assert check_resume(approved, write(kind, shown, tmp_path)) == []


@pytest.mark.parametrize("kind", KINDS)
def test_one_value_in_the_file_cannot_serve_two_parts(
    kind: str, tmp_path: Path
) -> None:
    approved = rich()
    first = "degree" if kind == "docx" else "institution"
    second = "institution" if kind == "docx" else "degree"
    entry = copy.deepcopy(approved["education"][0])
    entry[first] = ""
    approved["education"].append(entry)
    assert check_resume(approved, write(kind, approved, tmp_path, "ok")) == []
    # The first entry loses its second field and its years. The second entry,
    # with no ordered item, shows the same values one time only.
    shown = copy.deepcopy(approved)
    shown["education"][0][second] = ""
    shown["education"][0]["years"] = ""
    findings = check_resume(approved, write(kind, shown, tmp_path))
    assert f"education[1].{second}" in locations(findings)
    assert "education[1].years" in locations(findings)


def test_place() -> None:
    # The first match of the first item blocks the only free match of the second
    # item, so the search must try the second match of the first item.
    options = [[(0, 10), (20, 30)], [(5, 8), (40, 50)], [(40, 50), (45, 46)]]
    assert place(options) == [(20, 30), (5, 8), (40, 50)]
    assert place([[(0, 5)], [(1, 3)]]) == [(0, 5), None]
    assert place([]) == []


def test_place_tries_only_the_first_matches_of_an_item() -> None:
    # The first MAX_OPTIONS matches of the first item overlap the only match of
    # the second item. The free match after them is not tried: the result is a
    # finding (a correct file can fail), never a pass and never a long search.
    many = [(i, i + 1) for i in range(MAX_OPTIONS)] + [(500, 501)]
    assert place([many, [(0, 100)]]) == [None, (0, 100)]
    assert place([many[-MAX_OPTIONS:], [(0, 100)]]) == [(500, 501), (0, 100)]


# A word that ends in a combining mark (Devanagari), and the letters of a
# longer word of the same script.
MARK_WORD = "\u0939\u093f\u0902\u0926\u0940"
MORE_LETTERS = "\u092d\u093e\u0937\u093e"


@pytest.mark.parametrize(
    ("approved", "shown"),
    [
        (MARK_WORD, MARK_WORD + MORE_LETTERS),
        # The approved word stops before the last mark of the shown word.
        (MARK_WORD[:-1], MARK_WORD),
        ("Go", "Go\u0332od"),
    ],
)
def test_a_combining_mark_is_a_part_of_its_word(
    approved: str, shown: str, tmp_path: Path
) -> None:
    # The minimal PDF has no font for these characters, so only a Word file.
    resume = rich()
    resume["additional"]["languages"] = [approved]
    assert check_resume(resume, write("docx", resume, tmp_path, "ok")) == []
    wrong = copy.deepcopy(resume)
    wrong["additional"]["languages"] = [shown]
    findings = check_resume(resume, write("docx", wrong, tmp_path))
    assert "additional.languages[0]" in locations(findings)


def test_each_line_of_the_word_file_is_claimed(tmp_path: Path) -> None:
    # Each text of the Word file is an approved item or a claimed heading line;
    # only the separators stay free. A new fixed line in the Word writer that
    # the check does not claim makes this test fail.
    resume = rich()
    del resume["personalInfo"]["title"]  # The check does not test this field.
    path = write("docx", resume, tmp_path)
    text = normalize(extract_text(path))
    findings, claimed = walk(resume_parts(resume, "docx"), text, path)
    assert findings == []
    rest = list(text)
    for start, end in claimed:
        rest[start:end] = [" "] * (end - start)
    assert set(rest) <= set(" ,|\n"), "".join(rest)


def test_only_the_template_tags_are_removed_from_a_pdf_bullet(
    tmp_path: Path,
) -> None:
    approved = rich()
    bullet = "Proved x<y and z>w."
    approved["personalProjects"][0]["description"] = [bullet]
    lines = pdf_lines(approved)
    at = lines.index("\u2022 " + bullet)
    path = tmp_path / "ok.pdf"
    path.write_bytes(pdf_bytes(lines))
    assert check_resume(approved, path) == []
    # The words between the two signs are approved text, not a tag.
    lines[at] = "\u2022 Proved xw."
    path = tmp_path / "wrong.pdf"
    path.write_bytes(pdf_bytes(lines))
    assert locations(check_resume(approved, path)) == [
        "personalProjects[0].description[0]"
    ]


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
