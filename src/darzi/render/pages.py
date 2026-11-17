"""The page count of a Word file, from headless LibreOffice.

The page count that a `.docx` file stores is old: python-docx copies it from
its template. So the count needs a program that lays out the pages. LibreOffice
lays out the file with Carlito, a font with the same widths as Calibri, so its
count agrees with Microsoft Word for the plain Word résumé.

LibreOffice is found only from a fixed list of absolute paths. This module does
no PATH lookup and reads no environment variable. It runs LibreOffice as a
subprocess with an argument list and no shell, with a private profile in a new
temporary folder and a time limit. No value from the résumé goes into an
argument: the arguments are fixed text, the temporary folder and the absolute
path of the Word file.
"""

import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader

from darzi.facts.repo import InputError
from darzi.paths import is_file

# The places where LibreOffice installs its program, in the order of the search.
SOFFICE_PATHS = (
    Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
    Path("/opt/homebrew/bin/soffice"),
    Path("/usr/local/bin/soffice"),
    Path("/usr/bin/soffice"),
    Path("/usr/lib/libreoffice/program/soffice"),
)
INSTALL_HINT = (
    "Install LibreOffice: on macOS `brew install --cask libreoffice`, on Debian "
    "or Ubuntu `sudo apt install libreoffice-writer`."
)
CONVERT_TIMEOUT = 120


def find_soffice() -> Path | None:
    """Return the first LibreOffice program of `SOFFICE_PATHS`, or None."""
    for path in SOFFICE_PATHS:
        if is_file(path):
            return path
    return None


def word_pages(docx: Path) -> int:
    """Return the number of pages of `docx` as LibreOffice lays it out.

    Raises InputError when LibreOffice is absent, does not start, takes more
    than `CONVERT_TIMEOUT` seconds, or does not make a readable PDF.
    """
    soffice = find_soffice()
    if soffice is None:
        raise InputError(
            f"LibreOffice was not found, so the pages of {docx.name} cannot be "
            f"counted. {INSTALL_HINT}"
        )
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        # A private profile, so that an open LibreOffice window does not stop
        # the conversion.
        profile = (folder / "profile").as_uri()
        cmd = [
            str(soffice),
            "--headless",
            f"-env:UserInstallation={profile}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(folder),
            str(docx.absolute()),
        ]
        try:
            done = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CONVERT_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise InputError(
                f"LibreOffice did not convert {docx.name} in {CONVERT_TIMEOUT} s. "
                "Close LibreOffice and try again."
            ) from exc
        except OSError as exc:
            raise InputError(
                f"LibreOffice did not start ({soffice}): {exc.strerror or exc}. "
                f"{INSTALL_HINT}"
            ) from exc
        pdf = folder / f"{docx.stem}.pdf"
        if done.returncode != 0 or not is_file(pdf):
            detail = done.stderr.strip()[-300:] or f"exit code {done.returncode}"
            raise InputError(
                f"LibreOffice could not convert {docx.name} to PDF to count its "
                f"pages: {detail}"
            )
        try:
            return len(PdfReader(pdf).pages)
        except Exception as exc:  # pypdf raises many kinds of errors for a bad file.
            raise InputError(
                f"The PDF that LibreOffice made from {docx.name} cannot be read: {exc}"
            ) from exc
