"""Rules about verification, open questions and length (R-16, R-17, R-18).

This module has only the reader for the open questions in questions.md now.
`facts export` needs it for D-21. Work item W-3 adds the rules.
"""

import re
from pathlib import Path

QUESTIONS = "questions.md"
OPEN_END_MARKERS = ("## Settled by confirmed facts", "## Resolved")
ITEM = re.compile(r"^- \*\*([0-9]+[a-z]?)\.", re.M)


def open_questions(text: str) -> dict[str, list[str]]:
    """Return {fact id: [question numbers]} from the `Affects:` lines of open questions.

    Open questions are the numbered items above the "Settled" and "Resolved" parts.
    """
    for marker in OPEN_END_MARKERS:
        text = text.split(marker, 1)[0]
    affected: dict[str, list[str]] = {}
    starts = list(ITEM.finditer(text))
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        body = text[m.start() : end]
        for line in re.findall(r"Affects:\s*([^\n]+)", body):
            for fact_id in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", line.lower()):
                affected.setdefault(fact_id, []).append(m.group(1))
    return affected


def load_open_questions(repo: Path) -> dict[str, list[str]]:
    """Read questions.md of `repo`; a missing file means no open questions."""
    path = repo / QUESTIONS
    return open_questions(path.read_text(encoding="utf-8")) if path.is_file() else {}
