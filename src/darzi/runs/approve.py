"""`facts approve`: the owner approves a selection or a run.

This module has only `safe()` now. The CLI report uses it, so that a finding
cannot put terminal codes or invisible characters on the screen. Work item W-5
adds the signed approval (docs/SPEC.md section 7).
"""

import re
import unicodedata

CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Allow-list: letters, numbers, punctuation, symbols, normal spaces and combining
# accents. Everything else (format, separators, private use, unassigned) is shown as
# "?". Some allowed-category characters still render as blank; they are listed too.
VISIBLE = ("L", "N", "P", "S")
BLANK_LOOKING = {"ᅟ", "ᅠ", "ㅤ", "ﾠ", "⠀"}


def _shown(c: str) -> bool:
    if c in "\n\t":
        return True
    if c in BLANK_LOOKING:
        return False
    code = ord(c)
    if 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF:  # variation selectors
        return False
    category = unicodedata.category(c)
    return category == "Zs" or category in ("Mn", "Mc") or category.startswith(VISIBLE)


def safe(text: str) -> str:
    """Show only visible characters; replace every other character with '?'.

    Covers terminal escape codes, bidirectional overrides, zero-width and other
    invisible characters, which could hide or reorder text on the review screen.
    """
    text = CONTROL.sub("?", text)
    return "".join(c if _shown(c) else "?" for c in text)
