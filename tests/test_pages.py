"""Tests of the Word page count with headless LibreOffice."""

from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document

from darzi.facts.repo import InputError
from darzi.render import pages
from darzi.render.pages import find_soffice, word_pages


def _fake(folder: Path, script: str) -> Path:
    """Write a fake LibreOffice program and make it the only one in the list."""
    fake = folder / "soffice"
    fake.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    fake.chmod(0o755)
    return fake


def test_refuses_without_libreoffice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pages, "SOFFICE_PATHS", (tmp_path / "none" / "soffice",))
    assert find_soffice() is None
    with pytest.raises(InputError, match="brew install --cask libreoffice"):
        word_pages(tmp_path / "r.docx")


def test_the_list_holds_absolute_paths_only() -> None:
    assert len(pages.SOFFICE_PATHS) == 5
    assert all(path.is_absolute() for path in pages.SOFFICE_PATHS)


def test_refuses_when_the_conversion_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = tmp_path / "args.txt"
    fake = _fake(
        tmp_path,
        f'for a in "$@"; do echo "$a" >> "{args}"; done\necho broken >&2\nexit 1\n',
    )
    monkeypatch.setattr(pages, "SOFFICE_PATHS", (fake,))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(InputError, match="could not convert r.docx.*broken"):
        word_pages(Path("r.docx"))
    given = args.read_text(encoding="utf-8").splitlines()
    assert given[:2] == ["--headless", given[1]]
    assert given[1].startswith("-env:UserInstallation=file:///")
    assert given[2:5] == ["--convert-to", "pdf", "--outdir"]
    assert Path(given[5]).is_absolute() and not given[5].startswith(str(tmp_path))
    assert given[6] == str(tmp_path / "r.docx")  # absolute, never an option


def test_refuses_when_libreoffice_does_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake(tmp_path, "exit 0\n")
    fake.chmod(0o644)  # a file that cannot run
    monkeypatch.setattr(pages, "SOFFICE_PATHS", (fake,))
    with pytest.raises(InputError, match="LibreOffice did not start"):
        word_pages(tmp_path / "r.docx")


def test_refuses_on_a_time_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake(tmp_path, "exec sleep 5\n")
    monkeypatch.setattr(pages, "SOFFICE_PATHS", (fake,))
    monkeypatch.setattr(pages, "CONVERT_TIMEOUT", 0.2)
    with pytest.raises(InputError, match="did not convert r.docx in 0.2 s"):
        word_pages(tmp_path / "r.docx")


def test_refuses_a_pdf_that_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fake writes a damaged PDF into the output folder (argument 6).
    fake = _fake(tmp_path, 'echo "%PDF-1.4 damaged" > "$6/r.pdf"\nexit 0\n')
    monkeypatch.setattr(pages, "SOFFICE_PATHS", (fake,))
    with pytest.raises(InputError, match="cannot be read"):
        word_pages(tmp_path / "r.docx")


@pytest.mark.libreoffice
@pytest.mark.skipif(find_soffice() is None, reason="LibreOffice is not installed")
def test_counts_pages_with_libreoffice(tmp_path: Path) -> None:
    doc = Document()
    doc.add_paragraph("Page one.")
    doc.add_page_break()
    doc.add_paragraph("Page two.")
    path = tmp_path / "two.docx"
    doc.save(str(path))
    count = word_pages(path)
    print("LibreOffice page count:", count)
    assert count == 2
