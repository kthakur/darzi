"""Rules about verification, open questions and length (R-16, R-17, R-18).

R-16 warns about bullets that are not verified. R-17 blocks facts that an open
question in questions.md names on its `Affects:` line. R-18 warns when a file in
manifest.json has more pages than the selection's max_pages. `facts export` also
uses the reader for the open questions (D-21).
"""

import re
from pathlib import Path
from typing import Any

from darzi.facts.check_structure import Context
from darzi.facts.validate import Finding
from darzi.paths import is_file

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
    return open_questions(path.read_text(encoding="utf-8")) if is_file(path) else {}


def _f(rule: dict[str, Any], where: str, message: str, text: str = "") -> Finding:
    return Finding(rule["id"], rule["severity"], where, message, text)


def r16_unverified(ctx: Context) -> list[Finding]:
    """R-16: warn for each used bullet that is not verified: true."""
    rule = ctx.rules["unverified_bullets"]
    findings: list[Finding] = []
    for p in ctx.pairs:
        if p.tailored is None or p.master is None:
            continue
        bullets = {b["id"]: b for b in p.master.get("bullets") or []}
        for bullet_id in p.bullet_ids:
            bullet = bullets.get(bullet_id)
            if bullet is not None and bullet.get("verified") is not True:
                findings.append(
                    _f(
                        rule,
                        f"{p.location}.description",
                        f"Bullet {bullet_id} is not verified. The owner may confirm "
                        "it, or the selection may leave it out.",
                        " ".join(str(bullet.get("text") or "").split()),
                    )
                )
    return findings


def r17_open_questions(ctx: Context, questions: dict[str, list[str]]) -> list[Finding]:
    """R-17: no entry or bullet that an open question names."""
    rule = ctx.rules["open_questions"]
    findings: list[Finding] = []
    for p in ctx.pairs:
        if p.tailored is None or p.master_id is None:
            continue
        for fact_id in (p.master_id, *p.bullet_ids):
            if fact_id in questions:
                numbers = ", ".join(questions[fact_id])
                findings.append(
                    _f(
                        rule,
                        p.location,
                        f"{fact_id} is named in open question(s) {numbers} in "
                        "questions.md. Leave it out until the owner answers.",
                    )
                )
    return findings


def r18_pages(ctx: Context, manifest: dict[str, Any] | None) -> list[Finding]:
    """R-18: warn when an output file has more pages than max_pages."""
    rule = ctx.rules["page_count"]
    max_pages = ctx.the_map.get("max_pages")
    if not manifest or not isinstance(max_pages, int):
        return []
    findings: list[Finding] = []
    for item in manifest.get("files") or []:
        pages = item.get("pages")
        if isinstance(pages, int) and pages > max_pages:
            findings.append(
                _f(
                    rule,
                    f"manifest.json: {item.get('path')}",
                    f"{pages} pages; the selection allows {max_pages}. Shorten the "
                    "selection (Resume Matcher cannot drop content).",
                )
            )
    return findings
