"""The generic privacy gate (docs/SPEC.md section 10).

It scans `src/`, `tests/`, `docs/` and `scripts/`. It fails on an email address
that is not at example.com or example.org, and on a phone number outside the
555-0100 to 555-0199 range. Inputs that must fail are built at run time, so the
gate does not find its own test data.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("src", "tests", "docs", "scripts")
ALLOWED_DOMAINS = frozenset({"example.com", "example.org"})

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
# 10 to 15 digits. Between two digits: up to two of space ( ) . - characters.
PHONE = re.compile(r"(?<![\w.+])\+?\d(?:[ ().-]{0,2}\d){9,14}(?![\w])")


def findings(text: str) -> list[tuple[int, str]]:
    """Return (line number, kind) for each email or phone that is not allowed."""
    found: list[tuple[int, str]] = []
    for m in EMAIL.finditer(text):
        if m.group(1).lower() not in ALLOWED_DOMAINS:
            found.append((text.count("\n", 0, m.start()) + 1, "email"))
    for m in PHONE.finditer(text):
        last7 = int(re.sub(r"\D", "", m.group(0))[-7:])
        if not 5550100 <= last7 <= 5550199:
            found.append((text.count("\n", 0, m.start()) + 1, "phone"))
    return found


def scanned_files() -> list[Path]:
    files: list[Path] = []
    for top in SCANNED:
        base = ROOT / top
        if base.is_dir():
            files += [
                p
                for p in sorted(base.rglob("*"))
                if p.is_file() and "__pycache__" not in p.parts
            ]
    return files


def test_repo_has_no_real_email_or_phone() -> None:
    problems: list[str] = []
    for path in scanned_files():
        text = path.read_bytes().decode("utf-8", errors="replace")
        for line, kind in findings(text):
            problems.append(f"{path.relative_to(ROOT)}:{line}: {kind}")
    assert not problems, "Private data found:\n" + "\n".join(problems)


def test_gate_scans_files() -> None:
    assert any(p.name == "privacy_scan.py" for p in scanned_files())


@pytest.mark.parametrize(
    "text",
    [
        "Write to test@example.com.",
        "Write to info@example.org.",
        "Call +1 212 555 0100 or (212) 555-0199.",
        "Version 0.3, 2026-11-15, from 2026-01 to 2026-12.",
    ],
)
def test_gate_passes(text: str) -> None:
    assert findings(text) == []


def test_gate_finds_other_email() -> None:
    text = "line one\nWrite to someone" + "@" + "corp" + ".test today."
    assert findings(text) == [(2, "email")]


@pytest.mark.parametrize(
    "number",
    [
        "212 555 " + "0200",
        "(212) 555-" + "0099",
        "+44 20 " + "7946 " + "0958",
        "212" + "867" + "5309",
    ],
)
def test_gate_finds_phone_outside_range(number: str) -> None:
    assert findings(f"Call {number} now.") == [(1, "phone")]
