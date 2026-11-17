"""The round-trip check: the approved text must be in each output file.

`check_resume` and `check_letter` extract the text of one output file (`pypdf`
for a PDF, `python-docx` for a Word file) and compare it with the approved
résumé JSON or the approved cover letter text. They return a list of findings.
An empty list is a pass. A file that cannot be read gives one finding, never a
pass and never an exception.

Normalization, on both sides: Unicode NFKC, soft hyphens removed, letter case
ignored, white space collapsed. A line break in the file stays a line break, so
that the match can accept a word that the file breaks with a hyphen at a line
end. There is no other tolerance: a space in an item must be a space or a line
break in the file, and each other character must be the same.

Accepted forms:

- A date range: as approved, or as the Resume Matcher template shows it
  (`template_date`, a port of `formatDateRange`).
- A link in the contact data: as approved, or as the template shows it
  (`template_link`).
- In a PDF only, a bullet with HTML tags: without the tags, because the
  template shows the HTML. The Word file shows the bullet as text, with tags.

What must be in the file, in this order: the name; for each visible section in
the `sectionMeta` order, its items: the summary; for each full entry its title
and then its bullets in the approved order; then the title of each condensed
entry (a condensed entry shows one line and no bullets); for each education
entry, the field that the renderer shows first; each skill, language,
certification and award. The check walks this list and finds each item after
the previous one, so the same text two times cannot hide a missing second one.
The other fields of a part (the contact data, the organization, the location,
the dates, the role of a project, the other education fields) must be inside
that part: after its first item and before the first item of the next part.
For a cover letter, each paragraph must be there, in the approved order.

An approved item that is empty after normalization adds no text, so the check
skips it. An item of one, two or three characters (for example the skill "Go")
must stand alone: no letter or digit directly before or after it.

Limits. The check can pass a wrong file: extra text that is not approved;
hidden text (white, very small, off the page); words in a wrong letter case; a
short item found as a whole word in other text of the same part; a field that
the check does not test (the personal title, the links of a project, a custom
section, the section headings). The check can fail a correct file: a template
with two columns, whose text order is not the section order; a font with no
Unicode map; a page header or footer inside an item; a value that the template
shows in a form not listed above; an HTML entity such as `&amp;` in a PDF
bullet; text that the PDF extraction runs together with no space.
"""

import io
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from docx import Document
from pypdf import PdfReader

from darzi.facts.validate import Finding
from darzi.render.word import section_order

RULE = "roundtrip"
Kind = Literal["pdf", "docx"]
# An item of this length or shorter must stand alone (no letter or digit next
# to it), so that it does not match inside a longer word by accident.
SHORT_ITEM = 3
_LINE_BREAKS = "\n\r\x0b\x0c\x85\u2028\u2029"
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_ZIP_HEAD = b"PK\x03\x04"
_PDF_HEAD = b"%PDF-"


@dataclass(frozen=True)
class Item:
    """One approved text item: where it is, its text, and its accepted forms."""

    location: str
    text: str
    forms: tuple[str, ...]


@dataclass
class Part:
    """A part of the file: items in a fixed order, and items anywhere in it."""

    ordered: list[Item] = field(default_factory=list[Item])
    inside: list[Item] = field(default_factory=list[Item])


class _UnreadableError(Exception):
    """The text of the file cannot be extracted."""


def normalize(text: str) -> str:
    """Return `text` in the form that the check compares.

    NFKC, no soft hyphens, letter case folded. Each run of white space becomes
    one line break when it holds a line break, else one space.
    """
    text = unicodedata.normalize("NFKC", text).replace("\u00ad", "").casefold()

    def collapse(match: re.Match[str]) -> str:
        return "\n" if any(c in _LINE_BREAKS for c in match.group()) else " "

    return re.sub(r"\s+", collapse, text).strip()


def template_date(text: str) -> str:
    """Return a date range as the Resume Matcher template shows it.

    A port of `formatDateRange` (apps/frontend/lib/utils.ts at the pinned
    commit): each dash becomes " - ", and a dash goes between two dates that
    have no separator.
    """
    if not text:
        return ""
    formatted = re.sub("[\u2013\u2014]", "-", text)
    segments = formatted.split("-")
    trimmed: list[str] = []
    for i, segment in enumerate(segments):
        if i > 0:
            segment = segment.lstrip()
        if i < len(segments) - 1:
            segment = segment.rstrip()
        trimmed.append(segment)
    formatted = " - ".join(trimmed)
    formatted = re.sub(
        r"(?<![A-Za-z])([A-Za-z]+\.?\s+[0-9]{4})\s+([A-Za-z]+\.?\s+[0-9]{4})",
        r"\1 - \2",
        formatted,
    )
    formatted = re.sub(r"([0-9]{4})\s+([0-9]{4})", r"\1 - \2", formatted)
    return re.sub(
        r"(?<![a-z])([a-z]+\.?\s+[0-9]{4})\s+(Present|Current|Now|Ongoing)",
        r"\1 - \2",
        formatted,
        flags=re.IGNORECASE,
    )


def template_link(text: str) -> str:
    """Return a contact link as the Resume Matcher template shows it.

    The template removes a first "www." from a link that does not start with
    "http" or "//". It shows each other link as it is.
    """
    if text.startswith(("http", "//")):
        return text
    return re.sub(r"^www\.", "", text)


def letter_paragraphs(letter: str) -> list[str]:
    """Return the paragraphs of a cover letter, split as the Word file splits them."""
    paragraphs: list[str] = []
    for block in letter.split("\n\n"):
        text = " ".join(line.strip() for line in block.strip().splitlines())
        if text:
            paragraphs.append(text.lstrip("# "))
    return paragraphs


def check_resume(resume: Mapping[str, Any], path: Path) -> list[Finding]:
    """Return the findings of the round-trip check of the résumé file `path`."""
    try:
        text = extract_text(path)
    except _UnreadableError as exc:
        return [_unreadable(path, str(exc))]
    return _walk(resume_parts(dict(resume), _kind(path)), normalize(text), path)


def check_letter(letter: str, path: Path) -> list[Finding]:
    """Return the findings of the round-trip check of the cover letter `path`."""
    try:
        text = extract_text(path)
    except _UnreadableError as exc:
        return [_unreadable(path, str(exc))]
    part = Part()
    for i, paragraph in enumerate(letter_paragraphs(letter)):
        _add(part.ordered, f"letter.paragraph[{i}]", paragraph)
    return _walk([part], normalize(text), path)


def extract_text(path: Path) -> str:
    """Return the text of the PDF or Word file `path`.

    Raises an internal error with a short reason when the file is empty,
    damaged, encrypted, of a wrong type, or has no text.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise _UnreadableError(f"cannot be read: {exc.strerror or exc}") from exc
    if not data:
        raise _UnreadableError("is empty")
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        if not data.startswith(_PDF_HEAD):
            raise _UnreadableError("is not a PDF file")
        text = _pdf_text(data)
    elif suffix == ".docx":
        if not data.startswith(_ZIP_HEAD):
            raise _UnreadableError("is not a Word file")
        text = _docx_text(data)
    else:
        raise _UnreadableError("is not a .pdf or .docx file")
    if not normalize(text):
        raise _UnreadableError("has no text that can be extracted")
    return text


def _pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise _UnreadableError("is encrypted")
        return "\n".join(page.extract_text() for page in reader.pages)
    except _UnreadableError:
        raise
    except Exception as exc:  # pypdf raises many kinds of errors for a bad file.
        raise _UnreadableError(f"cannot be read as a PDF: {exc}") from exc


def _docx_text(data: bytes) -> str:
    try:
        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)
    except Exception as exc:  # python-docx raises many kinds of errors too.
        raise _UnreadableError(f"cannot be read as a Word file: {exc}") from exc


def _kind(path: Path) -> Kind:
    return "pdf" if path.suffix.lower() == ".pdf" else "docx"


def _unreadable(path: Path, reason: str) -> Finding:
    return Finding(
        RULE,
        "error",
        path.name,
        f"The file {reason}, so the round-trip check cannot pass. Make the file again.",
    )


def resume_parts(resume: dict[str, Any], kind: Kind) -> list[Part]:
    """Return the parts of `resume` that a `kind` file must show, in order."""
    info: dict[str, Any] = resume.get("personalInfo") or {}
    header = Part()
    _add(header.ordered, "personalInfo.name", info.get("name"))
    for key in ("email", "phone", "location"):
        _add(header.inside, f"personalInfo.{key}", info.get(key))
    for key in ("website", "linkedin", "github"):
        _add(header.inside, f"personalInfo.{key}", info.get(key), template_link)
    parts = [header]
    for key, _label in section_order(resume):
        if key == "summary":
            part = Part()
            _add(part.ordered, "summary", resume.get("summary"))
            parts.append(part)
        elif key == "workExperience":
            parts += _experience(resume.get("workExperience") or [], kind)
        elif key == "personalProjects":
            parts += _projects(resume.get("personalProjects") or [], kind)
        elif key == "education":
            parts += _education(resume.get("education") or [], kind)
        elif key == "additional":
            parts.append(_additional(resume.get("additional") or {}))
    return parts


def _experience(entries: list[dict[str, Any]], kind: Kind) -> list[Part]:
    full: list[Part] = []
    condensed: list[Part] = []
    for i, entry in enumerate(entries):
        where = f"workExperience[{i}]"
        part = Part()
        _add(part.ordered, f"{where}.title", entry.get("title"))
        _add(part.inside, f"{where}.company", entry.get("company"))
        _add(part.inside, f"{where}.years", entry.get("years"), template_date)
        if entry.get("condensed"):
            condensed.append(part)
            continue
        _add(part.inside, f"{where}.location", entry.get("location"))
        _bullets(part, where, entry.get("description") or [], kind)
        full.append(part)
    return full + condensed


def _projects(entries: list[dict[str, Any]], kind: Kind) -> list[Part]:
    parts: list[Part] = []
    for i, entry in enumerate(entries):
        where = f"personalProjects[{i}]"
        part = Part()
        _add(part.ordered, f"{where}.name", entry.get("name"))
        _add(part.inside, f"{where}.role", entry.get("role"))
        _add(part.inside, f"{where}.years", entry.get("years"), template_date)
        _bullets(part, where, entry.get("description") or [], kind)
        parts.append(part)
    return parts


def _education(entries: list[dict[str, Any]], kind: Kind) -> list[Part]:
    # The Word file shows the degree first; the template shows the institution
    # first. The first field is the ordered item; the others must be inside.
    first, second = ("degree", "institution")
    if kind == "pdf":
        first, second = second, first
    parts: list[Part] = []
    for i, entry in enumerate(entries):
        where = f"education[{i}]"
        part = Part()
        _add(part.ordered, f"{where}.{first}", entry.get(first))
        _add(part.inside, f"{where}.{second}", entry.get(second))
        _add(part.inside, f"{where}.years", entry.get("years"), template_date)
        _add(part.inside, f"{where}.description", entry.get("description"))
        parts.append(part)
    return parts


def _additional(additional: dict[str, Any]) -> Part:
    part = Part()
    for key in ("technicalSkills", "languages", "certificationsTraining", "awards"):
        for i, value in enumerate(additional.get(key) or []):
            _add(part.ordered, f"additional.{key}[{i}]", value)
    return part


def _bullets(part: Part, where: str, lines: Sequence[Any], kind: Kind) -> None:
    for j, line in enumerate(lines):
        text = str(line)
        if kind == "pdf":
            _add(part.ordered, f"{where}.description[{j}]", text, _strip_tags)
        else:
            _add(part.ordered, f"{where}.description[{j}]", text)


def _strip_tags(text: str) -> str:
    return _TAG.sub("", text)


def _add(
    items: list[Item], location: str, value: Any, *forms: Callable[[str], str]
) -> None:
    """Add `value` to `items` with its accepted forms; skip an empty value.

    With a form function, the value is accepted as given or in that form. The
    tag form (a PDF bullet) replaces the value, so the tags must not be there.
    """
    if value is None:
        return
    text = str(value)
    accepted = [normalize(f(text)) for f in forms]
    if not forms or forms[0] is not _strip_tags:
        accepted.insert(0, normalize(text))
    unique = tuple(dict.fromkeys(a for a in accepted if a))
    if unique:
        items.append(Item(location, text, unique))


def _pattern(form: str) -> re.Pattern[str]:
    """Return the regular expression of one normalized form.

    A space matches a space or a line break. A line break may follow a hyphen of
    the item, and a hyphen with a line break may come between two letters or
    digits (a word broken at a line end). A short item must stand alone.
    """
    parts: list[str] = []
    for i, char in enumerate(form):
        after = form[i + 1] if i + 1 < len(form) else ""
        if char == " ":
            parts.append("[ \n]")
            continue
        parts.append(re.escape(char))
        if char == "-" and after and after != " ":
            parts.append("\n?")
        elif char.isalnum() and after.isalnum():
            parts.append("(?:-\n)?")
    body = "".join(parts)
    if len(form) <= SHORT_ITEM:
        if form[0].isalnum():
            body = r"(?<![^\W_])" + body
        if form[-1].isalnum():
            body += r"(?![^\W_])"
    return re.compile(body)


def _find(item: Item, text: str, start: int, end: int) -> tuple[int, int] | None:
    """Return the first match of `item` in text[start:end], or None."""
    best: tuple[int, int] | None = None
    for form in item.forms:
        match = _pattern(form).search(text, start, end)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), match.end())
    return best


def _walk(parts: list[Part], text: str, path: Path) -> list[Finding]:
    findings: list[Finding] = []
    cursor = 0
    starts: list[int | None] = []
    begins: list[int] = []
    for part in parts:
        begins.append(cursor)
        first: int | None = None
        for number, item in enumerate(part.ordered):
            found = _find(item, text, cursor, len(text))
            if found is None:
                findings.append(_order_finding(item, text, path))
                continue
            if number == 0:
                first = found[0]
            cursor = found[1]
        starts.append(first)
    for index, part in enumerate(parts):
        start = starts[index]
        if start is None and part.ordered:
            # The part has a finding already: its items are not in place, so the
            # part has no known place in the file.
            continue
        begin = begins[index] if start is None else start
        later = [s for s in starts[index + 1 :] if s is not None]
        end = later[0] if later else len(text)
        for item in part.inside:
            if _find(item, text, begin, max(begin, end)) is None:
                findings.append(
                    Finding(
                        RULE,
                        "error",
                        item.location,
                        f"This approved text is not in its part of {path.name}.",
                        item.text,
                    )
                )
    return findings


def _order_finding(item: Item, text: str, path: Path) -> Finding:
    if _find(item, text, 0, len(text)) is None:
        message = f"This approved text is not in {path.name}."
    else:
        message = (
            f"This approved text is not in the approved order in {path.name}: it "
            "is only before an item that must come after it."
        )
    return Finding(RULE, "error", item.location, message, item.text)
