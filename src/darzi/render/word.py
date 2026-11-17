"""The Word files: the résumé and the cover letter.

The layout is plain and has one column: US Letter, one standard font, real
bullet lists, and no tables, text boxes or images. The content comes only from
the approved résumé JSON, in its section order. The résumé layout uses the same
print settings as the Resume Matcher PDF, so both files fit the same page limit.

A `.docx` file is a zip file, and python-docx writes the time of the clock into
each zip entry. So after the save, `fixed_zip` writes the zip again with fixed
entry fields: stored entries with no compression, the same entry order, one
fixed date, fixed system and attribute fields, no extra field and no comment.
Then two builds of the same content give the same bytes. The document
properties come from the python-docx template. They hold no name from the
résumé and no build time.
"""

import io
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docx import Document
from docx.document import Document as DocumentType
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Length, Mm, Pt

FONT = "Calibri"
DEFAULT_ORDER = [
    ("summary", "Summary"),
    ("workExperience", "Experience"),
    ("personalProjects", "Projects"),
    ("education", "Education"),
    ("additional", "Skills"),
]

# The print settings of Resume Matcher and their CSS values, from
# apps/frontend/lib/types/template-settings.ts at the pinned commit. When the pin
# moves, compare these values again.
PX_TO_PT = 0.75
FONT_SIZE_PX = {1: 11, 2: 12, 3: 14, 4: 15, 5: 16}
SECTION_GAP_PX = {1: 6, 2: 10, 3: 16, 4: 20, 5: 24}
ITEM_GAP_PX = {1: 2, 2: 4, 3: 8, 4: 12, 5: 16}
LINE_HEIGHT = {1: 1.15, 2: 1.25, 3: 1.35, 4: 1.45, 5: 1.55}
COMPACT_GAP = 0.6
COMPACT_LINE = 0.92
DEFAULT_LEVELS = {"fontSize": 3, "sectionSpacing": 3, "itemSpacing": 2, "lineHeight": 3}
DEFAULT_MARGIN_MM = 10
HEADING_SCALE = 1.05
NAME_SCALE = 1.5
# The subheading above the condensed roles, as in earlier-experience.tsx.
EARLIER_EXPERIENCE = "Earlier experience"
# The lists of the additional section after the skills, each with its heading.
# The round-trip check claims these heading lines.
ADDITIONAL_HEADINGS: tuple[tuple[str, str], ...] = (
    ("languages", "Languages"),
    ("certificationsTraining", "Certifications and training"),
    ("awards", "Awards"),
)

# The cover letter has one fixed layout, because it has no page limit.
LETTER_BODY_PT = 10.5
LETTER_MARGIN = Inches(0.7)


@dataclass(frozen=True)
class WordLayout:
    """The sizes of the Word résumé in points, and its margins in mm.

    The margins are in the order top, bottom, left, right.
    """

    body_pt: float
    section_gap_pt: float
    item_gap_pt: float
    line_spacing: float
    margins_mm: tuple[int, int, int, int]


def word_layout(settings: Mapping[str, Any]) -> WordLayout:
    """Return the Word résumé layout for the Resume Matcher print settings."""
    level = {
        key: int(settings.get(key, value)) for key, value in DEFAULT_LEVELS.items()
    }
    compact = bool(settings.get("compactMode"))
    gap = COMPACT_GAP if compact else 1.0
    top, bottom, left, right = (
        int(settings.get(f"margin{side}", DEFAULT_MARGIN_MM))
        for side in ("Top", "Bottom", "Left", "Right")
    )
    return WordLayout(
        body_pt=FONT_SIZE_PX[level["fontSize"]] * PX_TO_PT,
        section_gap_pt=SECTION_GAP_PX[level["sectionSpacing"]] * PX_TO_PT * gap,
        item_gap_pt=ITEM_GAP_PX[level["itemSpacing"]] * PX_TO_PT * gap,
        line_spacing=LINE_HEIGHT[level["lineHeight"]]
        * (COMPACT_LINE if compact else 1),
        margins_mm=(top, bottom, left, right),
    )


def resume_docx(
    resume: dict[str, Any], path: Path, settings: Mapping[str, Any] | None = None
) -> None:
    """Write the résumé to `path` as a plain Word file.

    The layout follows the print settings. The zip has fixed entry fields, so
    the same content always gives the same bytes.
    """
    layout = word_layout(settings or {})
    doc = _resume_document(layout)
    _header(doc, resume.get("personalInfo") or {}, layout)
    for key, label in _order(resume):
        _section(doc, resume, key, label, layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    _save(doc, path)


def letter_docx(letter: str, path: Path) -> None:
    """Write the cover letter to `path` as a plain Word file.

    Each block of text between two empty lines becomes one paragraph. The zip
    has fixed entry fields, as in `resume_docx`.
    """
    margins = (LETTER_MARGIN,) * 4
    doc = _new_document(
        LETTER_BODY_PT, (margins[0], margins[1], margins[2], margins[3])
    )
    doc.styles["Normal"].paragraph_format.space_after = Pt(2)  # pyright: ignore[reportAttributeAccessIssue]
    for block in letter.split("\n\n"):
        text = " ".join(line.strip() for line in block.strip().splitlines())
        if text:
            doc.add_paragraph(text.lstrip("# "))
    path.parent.mkdir(parents=True, exist_ok=True)
    _save(doc, path)


def _new_document(
    body_pt: float, margins: tuple[Length, Length, Length, Length]
) -> DocumentType:
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)  # US Letter
    sides = ("top_margin", "bottom_margin", "left_margin", "right_margin")
    for side, margin in zip(sides, margins, strict=True):
        setattr(section, side, margin)
    style = doc.styles["Normal"]
    style.font.name = FONT  # pyright: ignore[reportAttributeAccessIssue]
    style.font.size = Pt(body_pt)  # pyright: ignore[reportAttributeAccessIssue]
    return doc


def _resume_document(layout: WordLayout) -> DocumentType:
    top, bottom, left, right = (Mm(mm) for mm in layout.margins_mm)
    doc = _new_document(layout.body_pt, (top, bottom, left, right))
    for name in ("Normal", "List Bullet"):
        fmt = doc.styles[name].paragraph_format  # pyright: ignore[reportAttributeAccessIssue]
        fmt.space_before = Pt(0)
        fmt.space_after = Pt(0)
        fmt.line_spacing = layout.line_spacing
    return doc


def _header(doc: DocumentType, info: dict[str, Any], layout: WordLayout) -> None:
    name = doc.add_paragraph()
    name.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = name.add_run(str(info.get("name") or ""))
    run.bold = True
    run.font.size = Pt(layout.body_pt * NAME_SCALE)
    if info.get("title"):
        doc.add_paragraph(str(info["title"]))
    contact = [
        info.get(k)
        for k in ("email", "phone", "location", "website", "linkedin", "github")
    ]
    doc.add_paragraph(" | ".join(str(c) for c in contact if c))


def _section(
    doc: DocumentType,
    resume: dict[str, Any],
    key: str,
    label: str,
    layout: WordLayout,
) -> None:
    if key == "summary" and resume.get("summary"):
        _heading(doc, label, layout)
        doc.add_paragraph(str(resume["summary"]))
    elif key == "workExperience" and resume.get("workExperience"):
        _heading(doc, label, layout)
        _experience(doc, resume["workExperience"], layout)
    elif key == "personalProjects" and resume.get("personalProjects"):
        _heading(doc, label, layout)
        for e in resume["personalProjects"]:
            head = [e.get("name"), e.get("role")]
            _entry(doc, head, str(e.get("years") or ""), layout)
            _bullets(doc, e.get("description") or [])
    elif key == "education" and resume.get("education"):
        _heading(doc, label, layout)
        for e in resume["education"]:
            head = [e.get("degree"), e.get("institution")]
            _entry(doc, head, str(e.get("years") or ""), layout)
            if e.get("description"):
                doc.add_paragraph(str(e["description"]))
    elif key == "additional":
        _additional(doc, resume.get("additional") or {}, label, layout)


def _experience(
    doc: DocumentType, entries: list[dict[str, Any]], layout: WordLayout
) -> None:
    """Write the full roles with bullets, then one line for each condensed role."""
    for e in (e for e in entries if not e.get("condensed")):
        head = [e.get("title"), e.get("company"), e.get("location")]
        _entry(doc, head, str(e.get("years") or ""), layout)
        _bullets(doc, e.get("description") or [])
    condensed = [_condensed_line(e) for e in entries if e.get("condensed")]
    condensed = [line for line in condensed if line]
    if not condensed:
        return
    _entry(doc, [EARLIER_EXPERIENCE], "", layout)
    for line in condensed:
        doc.add_paragraph(line)


def _condensed_line(entry: dict[str, Any]) -> str:
    """Return the one line of a condensed role: title, company and years."""
    parts = (entry.get("title"), entry.get("company"), entry.get("years"))
    return ", ".join(str(p).strip() for p in parts if p and str(p).strip())


def _additional(
    doc: DocumentType, additional: dict[str, Any], label: str, layout: WordLayout
) -> None:
    skills = additional.get("technicalSkills") or []
    if skills:
        _heading(doc, label, layout)
        doc.add_paragraph(", ".join(str(s) for s in skills))
    for extra, title in ADDITIONAL_HEADINGS:
        if additional.get(extra):
            _heading(doc, title, layout)
            doc.add_paragraph(", ".join(str(s) for s in additional[extra]))


def _order(resume: dict[str, Any]) -> list[tuple[str, str]]:
    meta = sorted(resume.get("sectionMeta") or [], key=lambda m: m.get("order", 0))
    order = [
        (m["key"], m.get("displayName") or m["key"])
        for m in meta
        if m.get("key") != "personalInfo" and m.get("isVisible", True)
    ]
    return order or DEFAULT_ORDER


def _heading(doc: DocumentType, text: str, layout: WordLayout) -> None:
    para = doc.add_paragraph()
    para.paragraph_format.space_before = Pt(layout.section_gap_pt)
    run = para.add_run(text.upper())
    run.bold = True
    run.font.size = Pt(layout.body_pt * HEADING_SCALE)


def _entry(
    doc: DocumentType, head: Sequence[object], when: str, layout: WordLayout
) -> None:
    para = doc.add_paragraph()
    para.paragraph_format.space_before = Pt(layout.item_gap_pt)
    run = para.add_run(" | ".join(str(h) for h in head if h))
    run.bold = True
    if when:
        para.add_run(f" | {when}")


def _bullets(doc: DocumentType, lines: list[str]) -> None:
    for line in lines:
        doc.add_paragraph(str(line), style="List Bullet")


def section_order(resume: dict[str, Any]) -> list[tuple[str, str]]:
    """Return the visible sections of `resume` as (key, label), in their order.

    The round-trip check uses this order, so it expects the order of the Word
    file.
    """
    return _order(resume)


# The fixed fields of each zip entry.
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
ZIP_SYSTEM = 3  # Unix
ZIP_ATTRIBUTES = 0o644 << 16  # a normal file: rw-r--r--


def fixed_zip(data: bytes) -> bytes:
    """Return the zip file `data` again, with fixed fields in each entry.

    The entries keep their names, their order and their content. Each entry is
    stored with no compression, with the date `ZIP_DATE` (1 January 1980), the Unix
    system value, one attribute value, no extra field and no comment. The zip
    has no comment.
    """
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(data)) as source,
        zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as target,
    ):
        for entry in source.infolist():
            fixed = zipfile.ZipInfo(entry.filename, date_time=ZIP_DATE)
            fixed.compress_type = zipfile.ZIP_STORED
            fixed.create_system = ZIP_SYSTEM
            fixed.external_attr = ZIP_ATTRIBUTES
            fixed.extra = b""
            fixed.comment = b""
            target.writestr(fixed, source.read(entry))
        target.comment = b""
    return out.getvalue()


def _save(doc: DocumentType, path: Path) -> None:
    buffer = io.BytesIO()
    doc.save(buffer)
    path.write_bytes(fixed_zip(buffer.getvalue()))
