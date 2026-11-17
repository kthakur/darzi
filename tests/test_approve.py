"""`safe()`: the screen shows only visible characters.

Work item W-5 adds the tests of the signed approval to this file.
"""

import pytest

from darzi.runs.approve import safe


@pytest.mark.parametrize(
    "hidden",
    [
        "️",  # variation selector
        "\U000e0100",  # variation selector supplement
        "ㅤ",  # Hangul filler
        "⠀",  # braille blank
        "​",  # zero-width space
        "‮",  # right-to-left override
        "\U000e0041",  # tag character
        "\U0010ffff",  # not assigned
        "\x1b",  # escape
        "\x85",  # C1 control
    ],
)
def test_safe_replaces_hidden_characters(hidden: str) -> None:
    assert safe(f"a{hidden}b") == "a?b"


def test_safe_keeps_normal_text() -> None:
    text = "Café – “quoted” 40% $5 naïve\tok\nnext"
    assert safe(text) == text
