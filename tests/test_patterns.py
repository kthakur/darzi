"""Term and regex matching on word boundaries (D-13)."""

import pytest

from darzi.facts.patterns import find_regex, find_terms, term_regex


@pytest.mark.parametrize(
    ("term", "text", "hit"),
    [
        ("QWEX", "Made a QWEX search tool", True),
        ("QWEX", "Aqwexon Fruit Stand", False),
        ("QWEX", "qwex-based search", True),
        ("QWEX", "QWEXs", False),
        ("headed", "Headed the studio", True),
        ("headed", "a two-headed plan", True),  # a hyphen is a boundary
        ("headed", "forwardheaded", False),
        ("Node.js", "Wrote it in Node.js.", True),
        ("Node.js", "Node.jsx", False),
        ("C#", "Wrote C# services", True),
        ("C#", "C#9", False),
        (".NET", "Built on .NET and SQL", True),
        ("end-to-end", "End-to-end testing", True),
        ("led the design team", "Led the\ndesign  team", True),
        ("I", "I made it", True),
        ("I", "iWidget Labs", False),
        ("I", "UI design", False),
    ],
)
def test_term_on_word_boundaries_ignore_case(term: str, text: str, hit: bool) -> None:
    assert bool(term_regex(term).search(text)) is hit


def test_find_terms_reports_text_and_order() -> None:
    found = find_terms("Leveraged search and drove sales", ["drove", "leveraged"])
    assert [(m.term, m.text) for m in found] == [
        ("leveraged", "Leveraged"),
        ("drove", "drove"),
    ]


def test_find_terms_keeps_the_longer_overlap() -> None:
    found = find_terms("Built Zorblex systems", ["Zorblex", "Zorblex systems"])
    assert [m.text for m in found] == ["Zorblex systems"]


def test_find_regex() -> None:
    found = find_regex("Over 9+ years in UX", [r"\b\d+\s*\+?\s*years?\b"])
    assert [m.text for m in found] == ["9+ years"]


def test_empty_term_is_refused() -> None:
    with pytest.raises(ValueError):
        term_regex("  ")
