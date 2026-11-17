"""Tests of the Word files: the layout, and the same bytes for each build."""

from __future__ import annotations

import copy
import datetime
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
from docx import Document
from docx.shared import Mm, Pt

from darzi.render.word import (
    ZIP_ATTRIBUTES,
    ZIP_DATE,
    ZIP_SYSTEM,
    letter_docx,
    resume_docx,
    word_layout,
)

EXPORT = Path(__file__).parent / "expected" / "corporate-tech.export.json"
LETTER = "Dear team,\n\nThe first line\nand the same paragraph.\n\nRegards"


def resume() -> dict[str, Any]:
    data: Any = json.loads(EXPORT.read_text(encoding="utf-8"))
    return data["base"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- Layout (ports of the source tests, with synthetic data) ------------------


def test_word_layout_and_content(tmp_path: Path) -> None:
    data = resume()
    path = tmp_path / "r.docx"
    resume_docx(data, path)
    doc = Document(str(path))
    section = doc.sections[0]
    assert (section.page_width, section.page_height) == (7772400, 10058400)
    assert len(doc.tables) == 0 and len(doc.inline_shapes) == 0
    bullets = [
        p.text
        for p in doc.paragraphs
        if p.style is not None and p.style.name == "List Bullet"
    ]
    assert bullets == [d for e in data["workExperience"] for d in e["description"]]
    labels = {m["displayName"].upper() for m in data["sectionMeta"]}
    headings = [p.text for p in doc.paragraphs if p.text in labels]
    order = sorted(data["sectionMeta"], key=lambda m: m["order"])
    assert headings == [
        m["displayName"].upper() for m in order if m["key"] != "personalInfo"
    ]


def test_layout_follows_print_settings() -> None:
    compact = word_layout({"compactMode": True, "fontSize": 2})
    assert compact.body_pt == 9.0
    assert compact.section_gap_pt == pytest.approx(16 * 0.75 * 0.6)
    assert compact.item_gap_pt == pytest.approx(4 * 0.75 * 0.6)
    assert compact.line_spacing == pytest.approx(1.35 * 0.92)
    assert compact.margins_mm == (10, 10, 10, 10)


def test_layout_defaults() -> None:
    default = word_layout({})
    assert default.body_pt == 10.5
    assert default.section_gap_pt == 12.0 and default.item_gap_pt == 3.0
    assert default.line_spacing == pytest.approx(1.35)
    custom = word_layout({"marginTop": 15, "marginLeft": 20, "lineHeight": 1})
    assert custom.margins_mm == (15, 10, 20, 10)
    assert custom.line_spacing == pytest.approx(1.15)


def test_condensed_roles_are_one_line_with_no_bullets(tmp_path: Path) -> None:
    data = resume()
    old = data["workExperience"][-1]
    old["condensed"] = True
    path = tmp_path / "r.docx"
    resume_docx(data, path)
    texts = [p.text for p in Document(str(path)).paragraphs]
    assert not set(old["description"]) & set(texts)
    line = f"{old['title']}, {old['company']}, {old['years']}"
    assert texts.index("Earlier experience") < texts.index(line)
    kept = data["workExperience"][0]["description"][0]
    assert texts.index(kept) < texts.index("Earlier experience")


def test_word_resume_uses_print_settings(tmp_path: Path) -> None:
    path = tmp_path / "r.docx"
    resume_docx(resume(), path, {"compactMode": True, "fontSize": 2})
    doc = Document(str(path))
    assert doc.styles["Normal"].font.size == Pt(9)  # pyright: ignore[reportAttributeAccessIssue]
    section = doc.sections[0]
    for margin in (section.left_margin, section.top_margin):
        assert margin is not None
        assert abs(margin - Mm(10)) < Mm(0.1)


def test_letter_docx(tmp_path: Path) -> None:
    path = tmp_path / "l.docx"
    letter_docx(LETTER, path)
    assert [p.text for p in Document(str(path)).paragraphs] == [
        "Dear team,",
        "The first line and the same paragraph.",
        "Regards",
    ]


# --- The same bytes for each build -------------------------------------------


def _clock(year: int) -> Any:
    real = time.localtime

    def localtime(seconds: float | None = None) -> time.struct_time:
        now = real(seconds)
        return time.struct_time((year, *now[1:]))

    return localtime


def test_two_builds_have_the_same_sha256(tmp_path: Path) -> None:
    resume_docx(resume(), tmp_path / "a.docx")
    resume_docx(resume(), tmp_path / "b.docx")
    assert sha256(tmp_path / "a.docx") == sha256(tmp_path / "b.docx")
    letter_docx(LETTER, tmp_path / "a-letter.docx")
    letter_docx(LETTER, tmp_path / "b-letter.docx")
    assert sha256(tmp_path / "a-letter.docx") == sha256(tmp_path / "b-letter.docx")


def test_the_sha256_does_not_change_with_the_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_docx(resume(), tmp_path / "a.docx")
    letter_docx(LETTER, tmp_path / "a-letter.docx")
    monkeypatch.setattr(time, "localtime", _clock(2031))
    resume_docx(resume(), tmp_path / "b.docx")
    letter_docx(LETTER, tmp_path / "b-letter.docx")
    assert sha256(tmp_path / "a.docx") == sha256(tmp_path / "b.docx")
    assert sha256(tmp_path / "a-letter.docx") == sha256(tmp_path / "b-letter.docx")


def test_control_the_clock_patch_changes_a_plain_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: list[bytes] = []
    for year in (2029, 2031):
        monkeypatch.setattr(time, "localtime", _clock(year))
        buffer = io.BytesIO()
        Document().save(buffer)
        saved.append(buffer.getvalue())
    assert saved[0] != saved[1]
    dates = [zipfile.ZipFile(io.BytesIO(b)).infolist()[0].date_time[0] for b in saved]
    assert dates == [2029, 2031]


def test_each_zip_entry_has_the_fixed_fields(tmp_path: Path) -> None:
    path = tmp_path / "r.docx"
    resume_docx(resume(), path)
    buffer = io.BytesIO()
    Document().save(buffer)
    plain = [e.filename for e in zipfile.ZipFile(buffer).infolist()]
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        assert archive.comment == b""
        assert archive.testzip() is None
    assert [e.filename for e in entries] == plain  # the same entry order
    for entry in entries:
        assert entry.compress_type == zipfile.ZIP_STORED
        assert entry.compress_size == entry.file_size
        assert entry.date_time == ZIP_DATE == (1980, 1, 1, 0, 0, 0)
        assert entry.create_system == ZIP_SYSTEM == 3
        assert entry.external_attr == ZIP_ATTRIBUTES
        assert entry.extra == b"" and entry.comment == b""


def test_the_document_properties_hold_no_name_and_no_build_time(
    tmp_path: Path,
) -> None:
    path = tmp_path / "r.docx"
    data = resume()
    resume_docx(data, path)
    with zipfile.ZipFile(path) as archive:
        props = archive.read("docProps/core.xml") + archive.read("docProps/app.xml")
    text = props.decode("utf-8")
    assert data["personalInfo"]["name"] not in text
    assert str(datetime.date.today().year) not in text
    assert "<dc:creator>python-docx</dc:creator>" in text


def test_a_change_of_one_bullet_changes_the_sha256(tmp_path: Path) -> None:
    data = resume()
    other = copy.deepcopy(data)
    other["workExperience"][0]["description"][0] += " Again."
    resume_docx(data, tmp_path / "a.docx")
    resume_docx(other, tmp_path / "b.docx")
    assert sha256(tmp_path / "a.docx") != sha256(tmp_path / "b.docx")
