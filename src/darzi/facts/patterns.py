"""Text matching for rules (D-13): word boundaries, case ignored.

A plain substring check finds "QWEX" inside "Aqwexon". Here a term matches only
when no letter, digit or underscore touches either end, so terms that start or
end with a symbol ("C#", ".NET", "Node.js", "+1 ...") also work.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Match:
    """One term found in a text."""

    term: str
    start: int
    end: int
    text: str


def term_regex(term: str) -> re.Pattern[str]:
    """Return a case-insensitive regex for `term` on word boundaries.

    Spaces in the term match any run of whitespace, so a line break inside a
    phrase still matches.
    """
    words = term.split()
    if not words:
        raise ValueError("A term must contain text.")
    body = r"\s+".join(re.escape(w) for w in words)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def find_terms(text: str, terms: Iterable[str]) -> list[Match]:
    """Return every match of any term in `text`, in text order.

    When two matches overlap ("QWEX" inside "QWEX systems"), only the longer one stays.
    """
    found: list[Match] = []
    for term in terms:
        for m in term_regex(term).finditer(text):
            found.append(Match(term, m.start(), m.end(), m.group(0)))
    return _longest(found)


def _longest(found: list[Match]) -> list[Match]:
    """Drop each match that overlaps an earlier or longer one."""
    kept: list[Match] = []
    for m in sorted(found, key=lambda m: (m.start, -m.end)):
        if kept and m.start < kept[-1].end:
            continue
        kept.append(m)
    return kept


def find_regex(text: str, patterns: Iterable[str]) -> list[Match]:
    """Return every match of any regex in `text` (case ignored), in text order.

    When two matches overlap, only the longer one stays.
    """
    found: list[Match] = []
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            found.append(Match(pattern, m.start(), m.end(), m.group(0)))
    return _longest(found)
