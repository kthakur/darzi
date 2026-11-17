"""Structure rules for `facts check`.

R-8 titles, R-9 employer of record, R-10 dates and not-working periods, R-11
exclude_from, R-12 same shape as the base, R-14 never a company or a salaried job,
R-15 unpaid projects, R-19 skills.

Each tailored entry is paired with its base entry by section and Resume Matcher id
(Resume Matcher keeps ids when it reorders or drops entries). base.map.json then
names the master.yaml id of each base entry and its bullets.
"""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from darzi.facts.patterns import find_terms, term_regex
from darzi.facts.validate import Finding

SECTIONS = ("workExperience", "personalProjects", "education")
MONTHS: list[str] = list("jan feb mar apr may jun jul aug sep oct nov dec".split())
PAID_WORDS = [
    "client",
    "clients",
    "contract",
    "contractor",
    "paid",
    "freelance",
    "consultant",
    "consulting",
    "employee",
    "salaried",
]


@dataclass(frozen=True)
class Pair:
    """A tailored entry, its base entry (if any) and its master entry (if any)."""

    section: str
    rm_id: Any
    location: str
    tailored: dict[str, Any] | None
    base: dict[str, Any] | None
    master_id: str | None
    master: dict[str, Any] | None
    master_kind: str  # "roles", "projects", "education" or ""
    bullet_ids: tuple[str, ...]


@dataclass(frozen=True)
class Context:
    """Everything the structure rules read."""

    resume: dict[str, Any]
    base: dict[str, Any]
    the_map: dict[str, Any]
    rules: dict[str, Any]
    master: dict[str, Any]
    pairs: tuple[Pair, ...]


def build_context(
    resume: dict[str, Any],
    base: dict[str, Any],
    the_map: dict[str, Any],
    rules: dict[str, Any],
    master: dict[str, Any],
) -> Context:
    """Pair every tailored and base entry and attach its master entry."""
    by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    for kind in ("roles", "projects", "education"):
        for entry in master.get(kind) or []:
            by_id[entry["id"]] = (kind, entry)
    mapped = {(e["section"], e["rm_id"]): e for e in the_map.get("entries") or []}
    pairs: list[Pair] = []
    for section in SECTIONS:
        tailored = {
            e.get("id"): (i, e) for i, e in enumerate(resume.get(section) or [])
        }
        based = {e.get("id"): e for e in base.get(section) or []}
        for rm_id in list(based) + [k for k in tailored if k not in based]:
            index, entry = tailored.get(rm_id, (None, None))
            link = mapped.get((section, rm_id)) or {}
            master_id = link.get("id")
            kind, master_entry = by_id.get(master_id or "", ("", None))
            where = (
                f"{section}[{index}]" if index is not None else f"{section}[id={rm_id}]"
            )
            pairs.append(
                Pair(
                    section,
                    rm_id,
                    where,
                    entry,
                    based.get(rm_id),
                    master_id,
                    master_entry,
                    kind,
                    tuple(link.get("bullets") or []),
                )
            )
    return Context(resume, base, the_map, rules, master, tuple(pairs))


def _f(rule: dict[str, Any], where: str, message: str, text: str = "") -> Finding:
    return Finding(rule["id"], rule["severity"], where, message, text)


def _present(ctx: Context) -> Iterator[Pair]:
    """Yield pairs whose tailored entry exists and maps to a master entry."""
    for p in ctx.pairs:
        if p.tailored is not None and p.master is not None:
            yield p


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).lower()


# --- R-8 titles ---------------------------------------------------------------------


def r8_titles(ctx: Context) -> list[Finding]:
    """R-8: a title is the master title, an alt_title or a position title."""
    rule = ctx.rules["titles"]
    findings: list[Finding] = []
    for p in _present(ctx):
        assert p.tailored is not None and p.master is not None
        if p.section == "education":
            credential = str(p.master.get("credential") or "")
            allowed = {
                _norm(credential),
                _norm(re.sub(r"\s*\(no degree\)$", "", credential)),
            }
            degree = p.tailored.get("degree")
            if _norm(degree) not in allowed:
                findings.append(
                    _f(
                        rule,
                        f"{p.location}.degree",
                        f"Credential differs from master.yaml: {credential!r}.",
                        str(degree),
                    )
                )
            continue
        allowed = {_norm(p.master.get("title"))}
        allowed |= {_norm(t) for t in p.master.get("alt_titles") or []}
        allowed |= {_norm(t) for t in p.master.get("alt_names") or []}
        allowed |= {_norm(pos.get("title")) for pos in p.master.get("positions") or []}
        fields = ("title",) if p.section == "workExperience" else ("name", "role")
        shown = [p.tailored.get(f) for f in fields]
        if not any(_norm(v) in allowed for v in shown):
            findings.append(
                _f(
                    rule,
                    f"{p.location}.{fields[0]}",
                    f"Title is not the master title {p.master.get('title')!r} "
                    "or one of its alt_titles. Use the master title.",
                    " | ".join(str(v) for v in shown if v),
                )
            )
    return findings


# --- R-9 employer of record -------------------------------------------------------


def r9_employer(ctx: Context) -> list[Finding]:
    """R-9: the real org name (or its display name), and who employed or placed."""
    rule = ctx.rules["employer_of_record"]
    findings: list[Finding] = []
    for p in _present(ctx):
        assert p.tailored is not None and p.master is not None
        if p.section == "education":
            names = {
                _norm(p.master.get("institution")),
                _norm(p.master.get("display_institution")),
            }
            shown = p.tailored.get("institution")
            if _norm(shown) not in names:
                findings.append(
                    _f(
                        rule,
                        f"{p.location}.institution",
                        "Institution differs from master.yaml. Use its "
                        "institution or display_institution.",
                        str(shown),
                    )
                )
            continue
        names = {_norm(p.master.get("org")), _norm(p.master.get("display_org"))} - {""}
        fields = ("company",) if p.section == "workExperience" else ("name", "role")
        shown = [str(p.tailored.get(f) or "") for f in fields]
        if not any(_norm(v) in names for v in shown):
            findings.append(
                _f(
                    rule,
                    f"{p.location}.{fields[0]}",
                    f"Org differs from master.yaml: use {p.master.get('org')!r} "
                    f"or its display_org.",
                    " | ".join(shown),
                )
            )
            continue
        required = rule["required_any"].get(p.master_id or "")
        if required and not any(find_terms(v, required) for v in shown):
            findings.append(
                _f(
                    rule,
                    f"{p.location}.{fields[0]}",
                    f"Must show who employed or placed the owner: one of {required}.",
                    " | ".join(shown),
                )
            )
    return findings


# --- R-10 dates ---------------------------------------------------------------------


def _parse_years(text: str) -> list[str] | None:
    """Parse 'Mar 2014 - Sep 2016', '2003 - 2005', 'Jun 2022' or '... - Present'."""
    tokens: list[str] = []
    for part in re.split(r"\s+(?:-|\u2013|\u2014|to)\s+", text.strip()):
        part = part.strip()
        m = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{4})", part)
        if m and m.group(1).lower() in MONTHS:
            tokens.append(f"{m.group(2)}-{MONTHS.index(m.group(1).lower()) + 1:02d}")
        elif re.fullmatch(r"\d{4}", part):
            tokens.append(part)
        elif part.lower() in ("present", "now", "current"):
            tokens.append("present")
        else:
            return None
    return tokens if 1 <= len(tokens) <= 2 else None


def _same(shown: str, fact: Any) -> bool:
    """Return True if a shown date matches a fact at the shown precision."""
    fact = str(fact or "")
    if shown == "present" or fact == "present":
        return shown == fact
    return fact.startswith(shown) if len(shown) == 4 else shown == fact


def _dates_ok(tokens: list[str], start: Any, end: Any) -> bool:
    if len(tokens) == 2:
        return _same(tokens[0], start) and _same(tokens[1], end)
    single = [v for v in (start, end) if v not in (None, "")]
    if not single:
        return False
    if len(single) == 2 and str(single[0]) != str(single[1]):
        return False
    return _same(tokens[0], single[-1])


def _bounds(value: Any, low: bool) -> str:
    """Month bounds for a date: 'present' is open; a year is its first or last month."""
    v = str(value)
    if v == "present":
        return "9999-12"
    if len(v) == 4:
        return f"{v}-01" if low else f"{v}-12"
    return v


def r10_dates(ctx: Context) -> list[Finding]:
    """R-10: dates equal master.yaml; no role inside a not-working period."""
    rule = ctx.rules["not_working_periods"]
    findings: list[Finding] = []
    for p in _present(ctx):
        assert p.tailored is not None and p.master is not None
        shown = str(p.tailored.get("years") or "")
        tokens = _parse_years(shown) if shown else None
        spans = [(p.master.get("start"), p.master.get("end"))]
        spans += [
            (q.get("start"), q.get("end")) for q in p.master.get("positions") or []
        ]
        if shown and (
            tokens is None or not any(_dates_ok(tokens, s, e) for s, e in spans)
        ):
            findings.append(
                _f(
                    rule,
                    f"{p.location}.years",
                    f"Dates differ from master.yaml "
                    f"({p.master.get('start')} \u2013 {p.master.get('end')}).",
                    shown,
                )
            )
        if p.master_kind in rule.get("applies_to", ["roles"]):
            start, end = (
                p.master.get("start"),
                p.master.get("end") or p.master.get("start"),
            )
            if not start:
                continue
            # The narrowest certain span: a year-only start is its last month, a
            # year-only end its first month.
            lo, hi = _bounds(start, low=False), _bounds(end, low=True)
            for period in rule["periods"]:
                if lo <= _bounds(period["to"], low=False) and hi >= _bounds(
                    period["from"], low=True
                ):
                    findings.append(
                        _f(
                            rule,
                            p.location,
                            f"{p.master_id} ({start} \u2013 {end}) overlaps a "
                            f"not-working period ({period['source']}).",
                            shown,
                        )
                    )
    for p in ctx.pairs:
        if p.tailored is not None and p.base is None:
            continue  # R-12 reports added entries
        if p.tailored is not None and p.master is None:
            findings.append(
                _f(
                    rule,
                    p.location,
                    "Entry has no master.yaml id in base.map.json.",
                    "",
                )
            )
    return findings


# --- R-11 exclude_from -------------------------------------------------------------


def r11_exclude(ctx: Context) -> list[Finding]:
    """R-11: nothing excluded for this job type (or for all) appears."""
    rule = ctx.rules["exclude_from"]
    job_type = str(ctx.the_map.get("type") or "")
    blocked = {"all", job_type} - {""}
    findings: list[Finding] = []
    for p in _present(ctx):
        assert p.master is not None
        if blocked & set(p.master.get("exclude_from") or []):
            findings.append(
                _f(
                    rule,
                    p.location,
                    f"{p.master_id} is excluded from {job_type or 'all'} resumes.",
                )
            )
        bullets = {b["id"]: b for b in p.master.get("bullets") or []}
        for bullet_id in p.bullet_ids:
            if blocked & set((bullets.get(bullet_id) or {}).get("exclude_from") or []):
                findings.append(
                    _f(
                        rule,
                        p.location,
                        f"Bullet {bullet_id} is excluded from {job_type or 'all'} "
                        "resumes.",
                    )
                )
    return findings


# --- R-12 same shape ----------------------------------------------------------------


def r12_shape(ctx: Context) -> list[Finding]:
    """R-12: same entries and same bullet count as the base; no added rows."""
    rule = ctx.rules["same_shape"]
    findings: list[Finding] = []
    for p in ctx.pairs:
        if p.base is None and p.tailored is not None:
            findings.append(
                _f(
                    rule,
                    p.location,
                    "Entry is not in the base. Remove it.",
                    _label(p.tailored),
                )
            )
        elif p.base is not None and p.tailored is None:
            findings.append(
                _f(
                    rule,
                    p.location,
                    "Base entry is missing. Put it back.",
                    _label(p.base),
                )
            )
        elif p.base is not None and p.tailored is not None:
            want = len(p.base.get("description") or [])
            got = len(p.tailored.get("description") or [])
            if want != got and p.section != "education":
                findings.append(
                    _f(
                        rule,
                        f"{p.location}.description",
                        f"{got} bullets; the base has {want}. Reword and reorder "
                        "only; never add or drop a bullet.",
                        _label(p.tailored),
                    )
                )
    return findings


def _label(entry: dict[str, Any]) -> str:
    return str(
        entry.get("company") or entry.get("name") or entry.get("institution") or ""
    )


# --- R-14 never a company, never salaried -----------------------------------------


def r14_company(ctx: Context) -> list[Finding]:
    """R-14: no never_as_company name appears; freelance groups say "freelance"."""
    rule = ctx.rules["never_as_company"]
    findings: list[Finding] = []
    for where, text in _all_strings(ctx.resume, ""):
        for m in find_terms(text, rule["terms"]):
            findings.append(
                _f(rule, where, f"{m.text!r} was never a company. Remove it.", text)
            )
    for p in _present(ctx):
        assert p.tailored is not None
        if p.master_id in rule["freelance_only"]:
            shown = " ".join(
                str(p.tailored.get(f) or "")
                for f in ("company", "title", "name", "role")
            )
            if not term_regex("freelance").search(shown):
                findings.append(
                    _f(
                        rule,
                        p.location,
                        f"{p.master_id} was informal freelance work, not a salaried "
                        'job. Show "freelance" in the org or title.',
                        shown,
                    )
                )
    return findings


def _all_strings(node: Any, where: str) -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("sectionMeta", "descriptionStyles"):
                continue
            yield from _all_strings(value, f"{where}.{key}" if where else str(key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _all_strings(value, f"{where}[{i}]")
    elif isinstance(node, str):
        yield where, node


# --- R-15 unpaid projects -----------------------------------------------------------


def r15_unpaid(ctx: Context) -> list[Finding]:
    """R-15: unpaid projects stay under projects and never read as paid work."""
    rule = ctx.rules["unpaid_projects"]
    findings: list[Finding] = []
    for p in _present(ctx):
        assert p.tailored is not None and p.master is not None
        unpaid = p.master_kind == "projects" and p.master.get("type") == "independent"
        if not unpaid:
            continue
        if p.section != "personalProjects":
            findings.append(
                _f(
                    rule,
                    p.location,
                    f"{p.master_id} is an unpaid project. Show it under projects, "
                    "not under work experience.",
                    _label(p.tailored),
                )
            )
        shown = " ".join(
            str(p.tailored.get(f) or "") for f in ("company", "title", "name", "role")
        )
        for m in find_terms(shown, PAID_WORDS):
            findings.append(
                _f(
                    rule,
                    p.location,
                    f"{m.text!r} makes the unpaid project {p.master_id} read as "
                    "paid work. Remove it.",
                    shown,
                )
            )
    return findings


# --- R-19 skills --------------------------------------------------------------------


def r19_skills(ctx: Context) -> list[Finding]:
    """R-19: every skill is an exact master skill name, not removed or doubtful."""
    rule = ctx.rules["skills"]
    names = {str(s.get("name")) for s in ctx.master.get("skills") or []}
    removed = set(ctx.rules["removed_items"].get("skills") or [])
    findings: list[Finding] = []
    skills = (ctx.resume.get("additional") or {}).get("technicalSkills") or []
    for i, skill in enumerate(skills):
        where = f"additional.technicalSkills[{i}]"
        if skill in removed:
            findings.append(
                _f(rule, where, "Skill was removed by the owner. Remove it.", skill)
            )
        elif skill not in names:
            findings.append(
                _f(
                    rule,
                    where,
                    "Skill is not an exact master.yaml skill name. Use the "
                    "master name or remove it.",
                    str(skill),
                )
            )
    return findings


STRUCTURE_RULES: tuple[Callable[[Context], list[Finding]], ...] = (
    r8_titles,
    r9_employer,
    r10_dates,
    r11_exclude,
    r12_shape,
    r14_company,
    r15_unpaid,
    r19_skills,
)
