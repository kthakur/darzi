"""Minimal PDF files for the tests, with no PDF library.

`pdf_bytes` writes one text line for each given line, in one standard font
(Helvetica, WinAnsi encoding), with a correct cross-reference table. pypdf
reads the text back with one line break between two lines.
"""

from collections.abc import Sequence

LINES_PER_PAGE = 50


def _escape(line: str) -> bytes:
    data = line.encode("cp1252")
    return data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def pdf_bytes(lines: Sequence[str], lines_per_page: int = LINES_PER_PAGE) -> bytes:
    """Return a PDF file that shows `lines`, `lines_per_page` lines on a page."""
    pages = [
        list(lines[i : i + lines_per_page])
        for i in range(0, len(lines), lines_per_page)
    ] or [[]]
    font = 3
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # the page tree, below
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>",
    ]
    kids: list[str] = []
    for page_lines in pages:
        number = len(objects) + 1
        kids.append(f"{number} 0 R")
        stream = b"BT /F1 10 Tf 50 760 Td "
        for line in page_lines:
            stream += b"(" + _escape(line) + b") Tj 0 -14 Td "
        stream += b"ET"
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> "
            f"/Contents {number + 1} 0 R >>".encode()
        )
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )
    objects[1] = (
        f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    )
    out = b"%PDF-1.4\n"
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return out
