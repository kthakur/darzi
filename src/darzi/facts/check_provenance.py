"""Provenance rules for `facts check`.

R-20: each tailored bullet pairs with one source bullet of its entry, by word
similarity. R-13: numbers and capitalized names come from the source. R-21:
technology terms come from the source. R-22: the cover letter names only public
facts (or words from the job post).
"""

import difflib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from darzi.facts.check_structure import Context, Pair
from darzi.facts.patterns import find_terms, term_regex
from darzi.facts.validate import Finding

WORD = re.compile(r"[a-z0-9]+(?:[.'#+-][a-z0-9]+)*")
NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*")
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9&+#'.-]*")
SENTENCE_START = re.compile(r"(?:^|[.!?:;]\s+|\n\s*|^[\"'\u201c(]+)$")
NON_TECH_SKILL_TAGS = {"design", "research", "process"}


def similarity(a: str, b: str) -> float:
    """Return the word-sequence similarity of two texts (0 to 1)."""
    return difflib.SequenceMatcher(
        None, WORD.findall(a.lower()), WORD.findall(b.lower())
    ).ratio()


@dataclass(frozen=True)
class Claim:
    """A number, name or technology term in a text that a source must contain."""

    kind: str  # "number", "name" or "term"
    text: str


def claims(text: str, allowed: set[str], tech_terms: list[str]) -> list[Claim]:
    """Return the numbers, capitalized names and technology terms in `text`."""
    found = [Claim("number", m.group(0)) for m in NUMBER.finditer(text)]
    for m in TOKEN.finditer(text):
        word = re.sub(r"(?:'s|[.'-])+$", "", m.group(0))
        if not word or not any(c.isupper() for c in word) or word in allowed:
            continue
        initial_cap_only = word[0].isupper() and not any(c.isupper() for c in word[1:])
        if initial_cap_only and SENTENCE_START.search(text[: m.start()]):
            continue
        if len(word) == 1:
            continue
        found.append(Claim("name", word))
    terms = [Claim("term", m.text) for m in find_terms(text, tech_terms)]
    # A name inside a technology term is reported once, as the term.
    term_words = {w.lower() for c in terms for w in TOKEN.findall(c.text)}
    found = [c for c in found if c.kind != "name" or c.text.lower() not in term_words]
    return found + terms


def unsupported(found: list[Claim], source: str) -> list[Claim]:
    """Return the claims that `source` does not contain (word boundaries, any case)."""
    seen: set[tuple[str, str]] = set()
    missing: list[Claim] = []
    for claim in found:
        key = (claim.kind, claim.text.lower())
        if key in seen:
            continue
        seen.add(key)
        if not term_regex(claim.text).search(source):
            missing.append(claim)
    return missing


def tech_terms(rules: dict[str, Any], master: dict[str, Any]) -> list[str]:
    """Return rules.tech_terms plus the technical master skill names (R-21)."""
    terms = list(rules["tech_terms"]["terms"])
    for skill in master.get("skills") or []:
        tags = set(skill.get("tags") or [])
        if tags - NON_TECH_SKILL_TAGS:
            terms.append(str(skill["name"]))
    return terms


def public_corpus(master: dict[str, Any]) -> str:
    """Return all public fact text of master.yaml (no notes, sources or contacts)."""
    parts: list[str] = []
    profile: Any = master.get("profile") or {}
    parts += [
        str(profile.get(k) or "") for k in ("name", "location", "computing_background")
    ]
    parts += [str(h.get("text") or "") for h in profile.get("headlines") or []]
    parts += [
        f"{link.get('label')} {link.get('url')}" for link in profile.get("links") or []
    ]
    for entry in (master.get("roles") or []) + (master.get("projects") or []):
        if "all" in (entry.get("exclude_from") or []):
            continue
        parts.append(_entry_text(entry))
        parts += [
            str(b.get("text") or "")
            for b in entry.get("bullets") or []
            if "all" not in (b.get("exclude_from") or [])
        ]
    for edu in master.get("education") or []:
        parts += [
            str(edu.get(k) or "")
            for k in (
                "institution",
                "display_institution",
                "credential",
                "start",
                "end",
            )
        ]
    parts += [str(s.get("name") or "") for s in master.get("skills") or []]
    return "\n".join(parts)


def _entry_text(entry: dict[str, Any]) -> str:
    fields = [
        str(entry.get(k) or "")
        for k in ("org", "display_org", "title", "location", "start", "end")
    ]
    fields += [
        str(t) for t in (entry.get("alt_titles") or []) + (entry.get("alt_names") or [])
    ]
    fields += [
        f"{p.get('title')} {p.get('start')} {p.get('end')}"
        for p in entry.get("positions") or []
    ]
    return " | ".join(fields)


def _f(rule: dict[str, Any], where: str, message: str, text: str) -> Finding:
    return Finding(rule["id"], rule["severity"], where, message, text)


def _claim_findings(
    rule: dict[str, Any], where: str, text: str, missing: list[Claim], source: str
) -> Iterator[Finding]:
    for claim in missing:
        yield _f(
            rule,
            where,
            f"{claim.kind.capitalize()} {claim.text!r} is not in {source}. "
            "Use only what the fact says.",
            text,
        )


# --- Bullets: R-20, R-13, R-21 ------------------------------------------------------


def _pair_bullets(p: Pair) -> Iterator[tuple[int, str, str | None, float]]:
    """Yield (index, tailored text, source text or None, similarity) per bullet."""
    assert p.tailored is not None and p.master is not None
    by_id = {b["id"]: str(b.get("text") or "") for b in p.master.get("bullets") or []}
    sources = [by_id[i] for i in p.bullet_ids if i in by_id]
    shown = [str(t) for t in p.tailored.get("description") or []]
    scores = sorted(
        (
            (similarity(t, s), ti, si)
            for ti, t in enumerate(shown)
            for si, s in enumerate(sources)
        ),
        reverse=True,
    )
    chosen: dict[int, tuple[int, float]] = {}
    used: set[int] = set()
    for score, ti, si in scores:
        if ti not in chosen and si not in used:
            chosen[ti] = (si, score)
            used.add(si)
    for ti, text in enumerate(shown):
        si, score = chosen.get(ti, (-1, 0.0))
        yield ti, text, sources[si] if si >= 0 else None, score


def bullet_rules(ctx: Context) -> list[Finding]:
    """R-20, R-13 and R-21 for every bullet of every paired entry."""
    prov, numbers, terms_rule = (
        ctx.rules[k] for k in ("provenance", "numbers_and_names", "tech_terms")
    )
    threshold = float(prov.get("similarity_threshold", 0.5))
    allowed = set(numbers.get("allowed_words") or [])
    terms = tech_terms(ctx.rules, ctx.master)
    findings: list[Finding] = []
    for p in ctx.pairs:
        if p.tailored is None or p.master is None or p.section == "education":
            continue
        for ti, text, source, score in _pair_bullets(p):
            where = f"{p.location}.description[{ti}]"
            if source is None:
                findings.append(
                    _f(
                        prov,
                        where,
                        "Bullet has no source fact in base.map.json. Remove it.",
                        text,
                    )
                )
            elif score < threshold:
                findings.append(
                    _f(
                        prov,
                        where,
                        f"Bullet drifts from its source fact "
                        f"(similarity {score:.2f} < {threshold}). Source: {source!r}",
                        text,
                    )
                )
            context = f"{source or ''}\n{_entry_text(p.master)}"
            found = claims(text, allowed, terms)
            missing = unsupported(found, context)
            findings += _claim_findings(
                numbers,
                where,
                text,
                [c for c in missing if c.kind != "term"],
                "the source fact",
            )
            findings += _claim_findings(
                terms_rule,
                where,
                text,
                [c for c in missing if c.kind == "term"],
                "the source fact",
            )
    return findings


# --- Summary and headline: R-13, R-21 -----------------------------------------------


def summary_rules(ctx: Context) -> list[Finding]:
    """R-13 and R-21 for the summary and headline, against all public facts."""
    numbers, terms_rule = ctx.rules["numbers_and_names"], ctx.rules["tech_terms"]
    allowed = set(numbers.get("allowed_words") or [])
    corpus = public_corpus(ctx.master)
    terms = tech_terms(ctx.rules, ctx.master)
    findings: list[Finding] = []
    info: Any = ctx.resume.get("personalInfo") or {}
    for where, text in (
        ("personalInfo.title", info.get("title")),
        ("summary", ctx.resume.get("summary")),
    ):
        if not text:
            continue
        missing = unsupported(claims(str(text), allowed, terms), corpus)
        findings += _claim_findings(
            numbers,
            where,
            str(text),
            [c for c in missing if c.kind != "term"],
            "master.yaml",
        )
        findings += _claim_findings(
            terms_rule,
            where,
            str(text),
            [c for c in missing if c.kind == "term"],
            "master.yaml",
        )
    return findings


# --- Cover letter: R-22 -------------------------------------------------------------


def letter_rules(ctx: Context, letter: str | None, job: str | None) -> list[Finding]:
    """R-22: the cover letter names only public facts, or words from the job post."""
    if not letter:
        return []
    rule = ctx.rules["cover_letter_claims"]
    allowed = set(rule.get("allowed_words") or []) | set(
        ctx.rules["numbers_and_names"].get("allowed_words") or []
    )
    # The public contact pair may sign the letter; R-1 checks every other contact.
    contact = ctx.rules["public_contact"]
    corpus = "\n".join(
        [public_corpus(ctx.master), job or "", contact["email"], contact["phone"]]
    )
    terms = tech_terms(ctx.rules, ctx.master)
    findings: list[Finding] = []
    for n, line in enumerate(letter.splitlines(), start=1):
        if not line.strip():
            continue
        missing = unsupported(claims(line, allowed, terms), corpus)
        findings += _claim_findings(
            rule,
            f"cover_letter.md:{n}",
            line,
            missing,
            "the public facts or the job post",
        )
    return findings


def provenance_rules(
    ctx: Context, letter: str | None, job: str | None
) -> list[Finding]:
    """Return the findings of R-13, R-20, R-21 and R-22."""
    return bullet_rules(ctx) + summary_rules(ctx) + letter_rules(ctx, letter, job)
