"""`safe()`: the screen shows only visible characters.

Work item W-5 adds the tests of the signed approval to this file.
"""

import pytest

from darzi.runs.approve import safe


@pytest.mark.parametrize(
    "hidden",
    [
        "\ufe0f",  # variation selector
        "\U000e0100",  # variation selector supplement
        "\u3164",  # Hangul filler
        "\u2800",  # braille blank
        "\u200b",  # zero-width space
        "\u202e",  # right-to-left override
        "\U000e0041",  # tag character
        "\U0010ffff",  # not assigned
        "\x1b",  # escape
        "\x85",  # C1 control
    ],
)
def test_safe_replaces_hidden_characters(hidden: str) -> None:
    assert safe(f"a{hidden}b") == "a?b"


def test_safe_keeps_normal_text() -> None:
    # Visible non-ASCII: e acute, en dash, curly quotes, i diaeresis.
    text = "Caf\u00e9 \u2013 \u201cquoted\u201d 40% $5 na\u00efve\tok\nnext"
    assert safe(text) == text


def test_safe_keeps_combining_accent() -> None:
    # "e" followed by U+0301 (combining acute accent) is visible text.
    text = "Cafe\u0301 menu"
    assert safe(text) == text
