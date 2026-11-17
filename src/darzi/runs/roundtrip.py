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
  template shows the HTML. Only the tags that the template keeps count as tags
  (`strong`, `em`, `u`, `a`); each other "<...>" is text. The Word file shows
  the bullet as text, with tags.

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
skips it.

The ends of a match. Where an item starts or ends with a letter or a digit, the
file must have no letter or digit directly before or after it, so "Go" does not
match inside "Google". A phone number must not go on with more digits (after
spaces, dots, dashes or brackets). An email address or a link must not go on
with a ".", "-", "_" or "/" and then a letter or digit, so a longer domain or a
longer path does not match; a dot or a slash at its end is accepted. A single
date must not be one end of a range ("Jan 2020" does not match in "Jan 2020 -
Dec 2020"); a date that is approved as a range can be a range.

One place for each item. Each item claims its own span of the extracted text,
and no two items use the same text. So a company does not match inside a
bullet, and a role does not match inside the project name. The walk claims the
spans of the ordered items, each at its first free match after the previous
one. It also claims the line of each section heading, when that line comes
before the first item of the section, so that a skill does not match inside a
heading; a heading that is not there gives no finding. Then the other fields of
each part share out the free text of the part: the check tries the placements
and passes when each field gets its own span. A field with no free span is
missing.

Limits. The check can pass a wrong file: extra text that nobody approved, in a
place where it does not touch an approved item (an added bullet, an added
sentence, an added skill, a changed personal title); hidden text (white, very
small, off the page); words in a wrong letter case; a field that the check does
not test (the personal title, the links of a project, a custom section); a
section heading that is not there. The check can fail a correct file: a
template with two columns, whose text order is not the section order; a font
with no Unicode map; a page header or footer inside an item; a value that the
template shows in a form not listed above; an HTML entity such as `&amp;` in a
PDF bullet; a tag other than the four that the template keeps (for example
`<b>`) in a PDF bullet, because the template removes it and the check expects
it as text; text that the PDF extraction runs together with no space, also
between two fields (for example a title and its dates).
"""

import functools
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
# The kind of an item, for the rules at the ends of a match: plain text, a phone
# number, an email address or a link, a single date, a section heading.
Token = Literal["text", "phone", "address", "date", "heading"]
Span = tuple[int, int]
# The number of matches of one item that `place` tries.
MAX_OPTIONS = 20
_LINE_BREAKS = "\n\r\x0b\x0c\x85\u2028\u2029"
# The tags that the template keeps in a bullet: ALLOWED_TAGS in
# apps/frontend/lib/utils/html-sanitizer.ts. Other "<...>" text stays text.
_TAG = re.compile(r"</?(?:strong|em|u|a)(?:\s[^<>]*)?/?>", re.IGNORECASE)
_ALNUM = r"[^\W_]"
_NOT_AFTER_ALNUM = rf"(?<!{_ALNUM})"
_NOT_BEFORE_ALNUM = rf"(?!{_ALNUM})"
_DASHES = "\\-\u2013\u2014"
_SPACES = ("", " ", "\n")
# Rules at the start and at the end of a match, for each token. A phone number
# must not go on with more digits; an email address or a link must not go on
# with more domain or path; a single date must not be one end of a range; a
# heading must be a full line.
_TOKEN_BEFORE: dict[Token, str] = {
    "text": "",
    "phone": r"(?<!\d[ .()\-])",
    "address": rf"(?<!{_ALNUM}[._\-])",
    "date": "".join(
        rf"(?<!{_ALNUM}{a}[{_DASHES}]{b})" for a in _SPACES for b in _SPACES
    ),
    "heading": r"(?<![^\n])",
}
_TOKEN_AFTER: dict[Token, str] = {
    "text": "",
    "phone": r"(?![ .()\-]*\d)",
    "address": rf"(?![./_\-]{_ALNUM})",
    "date": rf"(?![ \n]?[{_DASHES}][ \n]?{_ALNUM})",
    "heading": r"(?![^\n])",
}
_ZIP_HEAD = b"PK\x03\x04"
_PDF_HEAD = b"%PDF-"


@dataclass(frozen=True)
class Item:
    """One approved text item: where it is, its text, and its accepted forms."""

    location: str
    text: str
    forms: tuple[str, ...]
    token: Token = "text"


@dataclass
class Part:
    """A part of the file: its heading, items in a fixed order, items anywhere."""

    heading: Item | None = None
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
    _add(header.inside, "personalInfo.email", info.get("email"), token="address")
    _add(header.inside, "personalInfo.phone", info.get("phone"), token="phone")
    _add(header.inside, "personalInfo.location", info.get("location"))
    for key in ("website", "linkedin", "github"):
        where = f"personalInfo.{key}"
        _add(header.inside, where, info.get(key), template_link, token="address")
    parts = [header]
    for key, label in section_order(resume):
        section: list[Part] = []
        if key == "summary":
            part = Part()
            _add(part.ordered, "summary", resume.get("summary"))
            section.append(part)
        elif key == "workExperience":
            section = _experience(resume.get("workExperience") or [], kind)
        elif key == "personalProjects":
            section = _projects(resume.get("personalProjects") or [], kind)
        elif key == "education":
            section = _education(resume.get("education") or [], kind)
        elif key == "additional":
            section.append(_additional(resume.get("additional") or {}))
        if section:
            headings: list[Item] = []
            _add(headings, f"sectionMeta.{key}.displayName", label, token="heading")
            section[0].heading = headings[0] if headings else None
        parts += section
    return parts


def _experience(entries: list[dict[str, Any]], kind: Kind) -> list[Part]:
    full: list[Part] = []
    condensed: list[Part] = []
    for i, entry in enumerate(entries):
        where = f"workExperience[{i}]"
        part = Part()
        _add(part.ordered, f"{where}.title", entry.get("title"))
        _add(part.inside, f"{where}.company", entry.get("company"))
        _add_years(part.inside, f"{where}.years", entry.get("years"))
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
        _add_years(part.inside, f"{where}.years", entry.get("years"))
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
        _add_years(part.inside, f"{where}.years", entry.get("years"))
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
    items: list[Item],
    location: str,
    value: Any,
    *forms: Callable[[str], str],
    token: Token = "text",
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
        items.append(Item(location, text, unique, token))


def _add_years(items: list[Item], location: str, value: Any) -> None:
    """Add a date field. A single date must not be a part of a longer range."""
    if value is None:
        return
    is_range = bool(re.search(f"[{_DASHES}]", template_date(str(value))))
    _add(items, location, value, template_date, token="text" if is_range else "date")


@functools.cache
def _pattern(form: str, token: Token) -> re.Pattern[str]:
    """Return the regular expression of one normalized form.

    A space matches a space or a line break. A line break may follow a hyphen of
    the item, and a hyphen with a line break may come between two letters or
    digits (a word broken at a line end). Where the item starts or ends with a
    letter or a digit, the text must have no letter or digit directly before or
    after it. The token adds the rules of `_TOKEN_BEFORE` and `_TOKEN_AFTER`.
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
    if form[0].isalnum():
        body = _NOT_AFTER_ALNUM + body
    if form[-1].isalnum():
        body += _NOT_BEFORE_ALNUM
    return re.compile(_TOKEN_BEFORE[token] + body + _TOKEN_AFTER[token])


def _spans(
    item: Item, text: str, begin: int, end: int, claimed: Sequence[Span]
) -> list[Span]:
    """Return each match of `item` inside text[begin:end] that is still free.

    A free match overlaps no span in `claimed`. The list is in text order.
    """
    found: set[Span] = set()
    for form in item.forms:
        pattern = _pattern(form, item.token)
        pos = begin
        while (match := pattern.search(text, pos)) and match.start() < end:
            span = (match.start(), match.end())
            if span[1] <= end and not any(_overlap(span, c) for c in claimed):
                found.add(span)
            pos = match.start() + 1
    return sorted(found)


def _overlap(a: Span, b: Span) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def place(options: Sequence[Sequence[Span]]) -> list[Span | None]:
    """Give each item one span from its options, with no two spans overlapping.

    `options[i]` holds the free matches of item i. The result gives the span of
    each item, or None for an item that gets no span. The search tries the
    placements in a fixed order and keeps the first one that gives a span to
    the largest number of items. Only the first `MAX_OPTIONS` matches of an
    item take part.
    """
    count = len(options)
    order = sorted(range(count), key=lambda i: len(options[i]))
    best: list[Span | None] = [None] * count
    best_placed = 0
    chosen: list[Span | None] = [None] * count

    def visit(step: int, placed: int) -> None:
        nonlocal best, best_placed
        if placed + count - step <= best_placed:
            return
        if step == count:
            best, best_placed = list(chosen), placed
            return
        index = order[step]
        taken = [c for c in chosen if c is not None]
        for span in options[index][:MAX_OPTIONS]:
            if not any(_overlap(span, t) for t in taken):
                chosen[index] = span
                visit(step + 1, placed + 1)
                chosen[index] = None
                if best_placed == count:
                    return
        visit(step + 1, placed)

    visit(0, 0)
    return best


def _walk(parts: list[Part], text: str, path: Path) -> list[Finding]:
    """Return a finding for each item of `parts` that is not in `text`.

    Each item claims its own span of the text, and no two items share a span or
    a part of one. First the walk goes through the parts in order. A section
    heading claims its line, when the line is there before the first item of the
    section. Each ordered item claims its first free match after the previous
    ordered item. Then, for each part, the items that may be anywhere in it
    share out the free text between the first ordered item of the part and the
    first ordered item of the next part (`place`). The longest item is tried
    first only when two items have the same number of free matches; any
    placement that gives each item its own span is a pass.
    """
    findings: list[Finding] = []
    claimed: list[Span] = []
    cursor = 0
    starts: list[int | None] = []
    begins: list[int] = []
    for part in parts:
        cursor = _claim_heading(part, text, cursor, claimed)
        begins.append(cursor)
        first: int | None = None
        for number, item in enumerate(part.ordered):
            free = _spans(item, text, cursor, len(text), claimed)
            if not free:
                findings.append(_order_finding(item, text, path, claimed))
                continue
            claimed.append(free[0])
            if number == 0:
                first = free[0][0]
            cursor = free[0][1]
        starts.append(first)
    for index, part in enumerate(parts):
        start = starts[index]
        if start is None and part.ordered:
            # The part has a finding already: its items are not in place, so the
            # part has no known place in the file.
            continue
        begin = begins[index] if start is None else start
        later = [s for s in starts[index + 1 :] if s is not None]
        end = max(begin, later[0] if later else len(text))
        items = sorted(part.inside, key=lambda i: -max(len(f) for f in i.forms))
        spans = place([_spans(i, text, begin, end, claimed) for i in items])
        for item, span in zip(items, spans, strict=True):
            if span is not None:
                claimed.append(span)
                continue
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


def _claim_heading(part: Part, text: str, cursor: int, claimed: list[Span]) -> int:
    """Claim the heading line of `part` when it comes before the part's first item.

    Return the new cursor: the end of the heading, or `cursor` when the heading is
    not claimed. A heading that is not there gives no finding.
    """
    if part.heading is None:
        return cursor
    heading = _spans(part.heading, text, cursor, len(text), claimed)
    if not heading:
        return cursor
    if part.ordered:
        first = _spans(part.ordered[0], text, cursor, len(text), claimed)
        if first and first[0][0] < heading[0][0]:
            return cursor
    claimed.append(heading[0])
    return heading[0][1]


def _order_finding(
    item: Item, text: str, path: Path, claimed: Sequence[Span]
) -> Finding:
    if not _spans(item, text, 0, len(text), claimed):
        message = f"This approved text is not in {path.name}."
    else:
        message = (
            f"This approved text is not in the approved order in {path.name}: it "
            "is only before an item that must come after it."
        )
    return Finding(RULE, "error", item.location, message, item.text)
