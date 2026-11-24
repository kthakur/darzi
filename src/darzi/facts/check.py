"""`facts check`: check a tailored resume and cover letter against rules.yaml.

This module has the text rules (R-1 to R-7); check_structure.py has the rules
that compare entries with the base and master.yaml, and check_provenance.py and
check_meta.py have the others. The checker reads the run folder and writes only
`check.json` (through the CLI). Severities come from rules.yaml.
"""

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from darzi.facts.check_meta import r16_unverified, r17_open_questions, r18_pages
from darzi.facts.check_provenance import provenance_rules
from darzi.facts.check_structure import STRUCTURE_RULES, build_context
from darzi.facts.patterns import find_regex, find_terms, term_regex
from darzi.facts.repo import InputError
from darzi.facts.validate import Finding
from darzi.paths import is_dir, is_file

TAILORED = "tailored.json"
COVER_LETTER = "cover_letter.md"
BASE = "base.json"
BASE_MAP = "base.map.json"
JOB = "job.md"
MANIFEST = "manifest.json"
CHECK_REPORT = "check.json"

# Text kinds in a resume or cover letter.
CONTACT = "contact"
HEADLINE = "headline"
SUMMARY = "summary"
ENTRY = "entry"  # titles, company names, project names
BULLET = "bullet"
SKILL = "skill"
EDUCATION = "education"
LETTER = "cover_letter"

PROSE = (HEADLINE, SUMMARY, BULLET, LETTER)
ALL_TEXT = (CONTACT, HEADLINE, SUMMARY, ENTRY, BULLET, SKILL, EDUCATION, LETTER)


@dataclass(frozen=True)
class Text:
    """One text of the resume or cover letter, with its place and kind."""

    location: str
    kind: str
    text: str
    entry: str = ""  # company or project name for bullets and entry fields


# --- Reading the run folder -------------------------------------------------------


@dataclass(frozen=True)
class RunFiles:
    """The inputs of one run folder (docs/SPEC.md section 8)."""

    tailored: dict[str, Any]
    letter: str | None
    base: dict[str, Any]
    the_map: dict[str, Any]
    job: str | None = None
    manifest: dict[str, Any] | None = None


def load_run(run: Path) -> RunFiles:
    """Read tailored.json, base.json, base.map.json and cover_letter.md (optional)."""
    if not is_dir(run):
        raise InputError(f"{run} is not a folder. Pass a run folder.")
    tailored = _resume(run / TAILORED, "Copy base.json to tailored.json first.")
    base = _resume(run / BASE, "Run facts export first.")
    the_map = _json(run / BASE_MAP, "Run facts export first.")
    if not isinstance(the_map, dict) or not isinstance(the_map.get("entries"), list):
        raise InputError(
            f"{run / BASE_MAP} has no entries list. Run facts export again."
        )
    letter_path, job_path = run / COVER_LETTER, run / JOB
    letter = letter_path.read_text(encoding="utf-8") if is_file(letter_path) else None
    job = job_path.read_text(encoding="utf-8") if is_file(job_path) else None
    manifest_path = run / MANIFEST
    manifest = _json(manifest_path, "") if is_file(manifest_path) else None
    return RunFiles(tailored, letter, base, the_map, job, manifest)


def _json(path: Path, fix: str) -> Any:
    if not is_file(path):
        raise InputError(f"{path} does not exist. {fix}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InputError(f"{path} is not valid JSON: {exc}.") from exc


def _resume(path: Path, fix: str) -> dict[str, Any]:
    data = _json(path, fix)
    if isinstance(data, dict) and "resume_data" in data:
        data = data["resume_data"]
    if not isinstance(data, dict) or "personalInfo" not in data:
        raise InputError(f"{path} is not a Resume Matcher resume (no personalInfo).")
    return data


# --- The keys that the rules read ---------------------------------------------------

# Each rules.yaml entry that `check` reads, with the keys that it reads without a
# default. Each entry also needs `id` and `severity`.
RULE_KEYS: dict[str, tuple[str, ...]] = {
    "public_contact": ("email", "phone"),
    "private_contact_patterns": ("terms", "regex"),
    "years_patterns": ("regex",),
    "banned_words": ("terms",),
    "banned_phrases": ("terms",),
    "pronouns": ("terms", "allowed_in"),
    "education_forbidden": ("terms", "coursework_label", "coursework_ids"),
    "credential_wording": ("items",),
    "removed_items": ("terms",),
    "doubtful_items": ("terms",),
    "inflation_phrases": ("terms",),
    "titles": (),
    "employer_of_record": ("required_any",),
    "not_working_periods": ("periods",),
    "exclude_from": (),
    "same_shape": (),
    "numbers_and_names": (),
    "never_as_company": ("terms", "freelance_only"),
    "unpaid_projects": (),
    "unverified_bullets": (),
    "open_questions": (),
    "page_count": (),
    "skills": (),
    "provenance": (),
    "tech_terms": ("terms",),
    "cover_letter_claims": (),
}
# Keys inside a value of an entry: (entry, value, keys, the value is a list).
NESTED_KEYS: tuple[tuple[str, str, tuple[str, ...], bool], ...] = (
    ("private_contact_patterns", "regex", ("any_email", "any_phone"), False),
    ("credential_wording", "items", ("term", "forbidden", "allowed"), True),
    ("not_working_periods", "periods", ("from", "to", "source"), True),
)
MAP_ENTRY_KEYS = ("section", "rm_id")


def _texts(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)  # pyright: ignore[reportUnknownVariableType]


def _text_map(value: Any) -> bool:
    return isinstance(value, dict) and all(isinstance(v, str) for v in value.values())  # pyright: ignore[reportUnknownVariableType]


@dataclass(frozen=True)
class Kind:
    """The kind of a rules.yaml value: its name, the fix and its test."""

    name: str
    fix: str
    test: Callable[[Any], bool]


# The kind of each key of the key test, as rules.schema.json gives it.
KINDS: dict[str, Kind] = {
    "id": Kind("text", "text", lambda v: isinstance(v, str)),
    "severity": Kind("text", "text", lambda v: isinstance(v, str)),
    "terms": Kind("a list of text", "a list", _texts),
    "regex": Kind(
        "a list of text or a mapping of text",
        "a list or a mapping",
        lambda v: _texts(v) or _text_map(v),
    ),
    "required_any": Kind(
        "a mapping of lists of text",
        "a mapping",
        lambda v: isinstance(v, dict) and all(_texts(x) for x in v.values()),  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    ),
    "periods": Kind(
        "a list of mappings",
        "a list",
        lambda v: isinstance(v, list) and all(isinstance(x, dict) for x in v),  # pyright: ignore[reportUnknownVariableType]
    ),
}
RULES_FIX = "Add it to rules.yaml."
MAP_FIX = "Run facts export again."

# The rules.yaml keys that `facts check SELECTION` reads (check_selection): each
# entry must be a mapping. `id` and `severity` are not read.
SELECTION_KEYS: dict[str, tuple[str, ...]] = {
    "removed_items": (),
    "doubtful_items": (),
}
# The rules.yaml keys that `facts export`, `facts public` and `facts query` read
# without a default (public_copy, privacy_findings, check_selection). The scan
# reads `id` and `severity` of the two entries that give findings.
PUBLIC_KEYS: dict[str, tuple[str, ...]] = {
    "public_contact": ("email", "phone"),
    "private_contact_patterns": ("id", "severity", "terms", "regex"),
    "privacy_patterns": ("id", "severity", "regex", "terms"),
    "removed_items": ("terms",),
    "doubtful_items": ("terms",),
}
PUBLIC_NESTED_KEYS: tuple[tuple[str, str, tuple[str, ...], bool], ...] = (
    ("private_contact_patterns", "regex", ("any_email", "any_phone"), False),
)


def _require_rule_keys(
    rules: Any,
    table: dict[str, tuple[str, ...]],
    nested: tuple[tuple[str, str, tuple[str, ...], bool], ...],
    common: tuple[str, ...] = (),
) -> None:
    """Raise InputError when rules.yaml lacks a key of `table` or `nested`.

    Each entry of `table` must be a mapping with the keys `common` and its own
    keys. A key of KINDS must have the kind that rules.schema.json gives it.
    The message names the key and the fix.
    """
    with_keys = f" with {' and '.join(common)}" if common else ""
    if not isinstance(rules, dict):
        raise InputError(
            "rules.yaml has no rule entries. Add the rule entries to rules.yaml, "
            f"each one a mapping{with_keys}."
        )
    for name, keys in table.items():
        if name not in rules:
            raise InputError(f"rules.yaml has no entry {name}. {RULES_FIX}")
        entry = rules[name]
        if not isinstance(entry, dict):
            raise InputError(
                f"rules.yaml: {name} is not a mapping. Write it as a mapping"
                f"{with_keys}."
            )
        for key in (*common, *keys):
            if key not in entry:
                raise InputError(f"rules.yaml: {name} has no key {key}. {RULES_FIX}")
            if key in KINDS and not KINDS[key].test(entry[key]):
                kind = KINDS[key]
                raise InputError(
                    f"rules.yaml: {name}.{key} is not {kind.name}. Write it as "
                    f"{kind.fix}, then run the command again."
                )
    for name, value, keys, is_list in nested:
        found: Any = rules[name][value]
        items: list[tuple[str, Any]] = (
            [(f"{name}.{value}[{i}]", v) for i, v in enumerate(found or [])]
            if is_list
            else [(f"{name}.{value}", found)]
        )
        for where, item in items:
            for key in keys:
                if not isinstance(item, dict) or key not in item:
                    raise InputError(
                        f"rules.yaml: {where} has no key {key}. {RULES_FIX}"
                    )


def require_selection_keys(rules: Any) -> None:
    """Raise InputError when rules.yaml lacks a key that `facts check SELECTION` reads.

    Without this test, a missing key stops the command with a traceback.
    """
    _require_rule_keys(rules, SELECTION_KEYS, ())


def require_public_keys(rules: Any) -> None:
    """Raise InputError when rules.yaml lacks a key that the public copy reads.

    `facts export`, `facts public` and `facts query` call it. Without this test,
    a missing key stops the command with a traceback.
    """
    _require_rule_keys(rules, PUBLIC_KEYS, PUBLIC_NESTED_KEYS)
    # privacy_findings() reads privacy_patterns.regex as a mapping of name to
    # pattern, as the source does. The schema and KINDS also permit a list.
    if isinstance(rules["privacy_patterns"]["regex"], list):
        raise InputError(
            "rules.yaml: privacy_patterns.regex is a list. Write it as a mapping "
            "of name to pattern, then run the command again."
        )


def require_keys(rules: Any, the_map: dict[str, Any]) -> None:
    """Raise InputError when rules.yaml or base.map.json lacks a key that check reads.

    The rules read these keys without a default. Without this test, a missing key
    stops `facts check` with a traceback. The message names the key and the fix.
    """
    _require_rule_keys(rules, RULE_KEYS, NESTED_KEYS, ("id", "severity"))
    for i, entry in enumerate(the_map.get("entries") or []):
        for key in MAP_ENTRY_KEYS:
            if not isinstance(entry, dict) or key not in entry:
                raise InputError(
                    f"{BASE_MAP}: entries[{i}] has no key {key}. {MAP_FIX}"
                )


def texts(resume: dict[str, Any], letter: str | None) -> Iterator[Text]:
    """Yield every text of the resume and the cover letter."""
    info: Any = resume.get("personalInfo") or {}
    for key in ("email", "phone"):
        if info.get(key):
            yield Text(f"personalInfo.{key}", CONTACT, str(info[key]))
    for key in ("name", "location", "website", "linkedin", "github"):
        if info.get(key):
            yield Text(f"personalInfo.{key}", ENTRY, str(info[key]))
    if info.get("title"):
        yield Text("personalInfo.title", HEADLINE, str(info["title"]))
    if resume.get("summary"):
        yield Text("summary", SUMMARY, str(resume["summary"]))
    yield from _entries(resume, "workExperience", ("title", "company", "location"))
    yield from _entries(resume, "personalProjects", ("name", "role"))
    for i, edu in enumerate(resume.get("education") or []):
        for key in ("institution", "degree", "description"):
            if edu.get(key):
                yield Text(f"education[{i}].{key}", EDUCATION, str(edu[key]))
    additional: Any = resume.get("additional") or {}
    for key, items in additional.items():
        for i, item in enumerate(items or []):
            yield Text(f"additional.{key}[{i}]", SKILL, str(item))
    if letter:
        for n, line in enumerate(letter.splitlines(), start=1):
            if line.strip():
                yield Text(f"{COVER_LETTER}:{n}", LETTER, line)


def _entries(
    resume: dict[str, Any], section: str, fields: tuple[str, ...]
) -> Iterator[Text]:
    for i, entry in enumerate(resume.get(section) or []):
        name = str(entry.get("company") or entry.get("name") or "")
        for key in fields:
            if entry.get(key):
                yield Text(f"{section}[{i}].{key}", ENTRY, str(entry[key]), name)
        for j, line in enumerate(entry.get("description") or []):
            yield Text(f"{section}[{i}].description[{j}]", BULLET, str(line), name)


# --- Rules --------------------------------------------------------------------------

Rule = Callable[[list[Text], dict[str, Any], dict[str, Any]], list[Finding]]


def _finding(entry: dict[str, Any], item: Text, message: str, hit: str) -> Finding:
    return Finding(entry["id"], entry["severity"], item.location, message, hit)


def _term_rule(key: str, kinds: tuple[str, ...], message: str) -> Rule:
    """Make a rule that flags any `rules[key].terms` match in texts of `kinds`."""

    def run(
        items: list[Text], rules: dict[str, Any], _: dict[str, Any]
    ) -> list[Finding]:
        entry = rules[key]
        return [
            _finding(entry, item, message.format(term=m.text), item.text)
            for item in items
            if item.kind in kinds
            for m in find_terms(item.text, entry["terms"])
        ]

    return run


def r1_contact(
    items: list[Text], rules: dict[str, Any], _: dict[str, Any]
) -> list[Finding]:
    """R-1: only the public email and phone, anywhere."""
    public = rules["public_contact"]
    private = rules["private_contact_patterns"]
    email, phone = public["email"].lower(), _digits(public["phone"])
    findings: list[Finding] = []
    for item in items:
        for m in find_terms(item.text, private["terms"]):
            findings.append(
                _finding(
                    private, item, f"Private contact {m.text!r}. Remove it.", item.text
                )
            )
        for m in find_regex(item.text, [private["regex"]["any_email"]]):
            if m.text.lower() != email and not _is_private(m.text, private):
                findings.append(
                    _finding(
                        public,
                        item,
                        f"Email {m.text!r} is not the public email {public['email']}.",
                        item.text,
                    )
                )
        for m in find_regex(item.text, [private["regex"]["any_phone"]]):
            if _digits(m.text)[-10:] != phone[-10:] and not _is_private(
                m.text, private
            ):
                findings.append(
                    _finding(
                        public,
                        item,
                        f"Phone {m.text!r} is not the public phone {public['phone']}.",
                        item.text,
                    )
                )
    by_loc = {i.location: i for i in items}
    if "personalInfo.email" not in by_loc:
        findings.append(
            Finding(
                public["id"],
                public["severity"],
                "personalInfo.email",
                f"No email. Use {public['email']}.",
            )
        )
    if "personalInfo.phone" not in by_loc:
        findings.append(
            Finding(
                public["id"],
                public["severity"],
                "personalInfo.phone",
                f"No phone. Use {public['phone']}.",
            )
        )
    return findings


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _is_private(text: str, private: dict[str, Any]) -> bool:
    return bool(find_terms(text, private["terms"]))


def r2_years(
    items: list[Text], rules: dict[str, Any], _: dict[str, Any]
) -> list[Finding]:
    """R-2: no count of years of experience."""
    entry = rules["years_patterns"]
    return [
        _finding(
            entry,
            item,
            f"Years count {m.text!r}. Remove it; role dates show time.",
            item.text,
        )
        for item in items
        if item.kind in PROSE
        for m in find_regex(item.text, entry["regex"])
    ]


r3_banned_words = _term_rule(
    "banned_words",
    PROSE,
    "Banned word {term!r}. Use a plain verb that says what was done.",
)
r3_banned_phrases = _term_rule(
    "banned_phrases", PROSE, "Banned phrase {term!r}. Say what was built, plainly."
)


def r4_pronouns(
    items: list[Text], rules: dict[str, Any], _: dict[str, Any]
) -> list[Finding]:
    """R-4: no pronouns in bullets, summary or headline (a cover letter may)."""
    entry = rules["pronouns"]
    kinds = tuple(
        k for k in (HEADLINE, SUMMARY, BULLET) if k not in entry["allowed_in"]
    )
    return [
        _finding(
            entry, item, f"Pronoun {m.text!r}. Start with the verb instead.", item.text
        )
        for item in items
        if item.kind in kinds
        for m in find_terms(item.text, entry["terms"])
    ]


def r5_education(
    items: list[Text], rules: dict[str, Any], master: dict[str, Any]
) -> list[Finding]:
    """R-5: no completed master's degree; coursework labelled; credential wording."""
    forbidden = rules["education_forbidden"]
    findings = [
        _finding(
            forbidden,
            item,
            f"{m.text!r} claims a degree that was not completed. "
            f"Write {forbidden['coursework_label']!r}.",
            item.text,
        )
        for item in items
        if item.kind in (*PROSE, EDUCATION, SKILL)
        for m in find_terms(item.text, forbidden["terms"])
    ]
    findings += _coursework_labels(items, forbidden, master)
    credentials = rules["credential_wording"]
    for item in items:
        if item.kind not in (*PROSE, EDUCATION, SKILL, ENTRY):
            continue
        for cred in credentials["items"]:
            wrong = find_terms(item.text, cred["forbidden"])
            for m in wrong:
                findings.append(
                    _finding(
                        credentials,
                        item,
                        f"{m.text!r}: there is no {cred['term']} certificate. "
                        + (
                            f"Write {cred['allowed'][0]!r}."
                            if cred["allowed"]
                            else "Remove it."
                        ),
                        item.text,
                    )
                )
            if (
                not wrong
                and cred["allowed"]
                and term_regex(cred["term"]).search(item.text)
            ):
                rest = item.text
                for phrase in cred["allowed"]:
                    rest = term_regex(phrase).sub(" ", rest)
                if term_regex(cred["term"]).search(rest):
                    findings.append(
                        _finding(
                            credentials,
                            item,
                            f"Write {cred['term']} as {cred['allowed'][0]!r}.",
                            item.text,
                        )
                    )
    return findings


def _coursework_labels(
    items: list[Text], forbidden: dict[str, Any], master: dict[str, Any]
) -> list[Finding]:
    names: set[str] = set()
    for edu in master.get("education") or []:
        if edu.get("id") in forbidden["coursework_ids"]:
            names.update(
                n.lower()
                for n in (edu.get("institution"), edu.get("display_institution"))
                if n
            )
    findings: list[Finding] = []
    entries: dict[str, list[Text]] = {}
    for item in items:
        if item.kind == EDUCATION:
            entries.setdefault(item.location.rsplit(".", 1)[0], []).append(item)
    for fields in entries.values():
        inst = next((f for f in fields if f.location.endswith(".institution")), None)
        if inst is None or inst.text.strip().lower() not in names:
            continue
        joined = " ".join(f.text for f in fields)
        if not term_regex(forbidden["coursework_label"]).search(joined):
            findings.append(
                _finding(
                    forbidden,
                    inst,
                    f"Coursework at {inst.text}: write "
                    f"{forbidden['coursework_label']!r}.",
                    joined,
                )
            )
    return findings


def r6_removed(
    items: list[Text], rules: dict[str, Any], _: dict[str, Any]
) -> list[Finding]:
    """R-6: no removed or doubtful items."""
    findings: list[Finding] = []
    for key, what in (("removed_items", "removed"), ("doubtful_items", "in doubt")):
        entry = rules[key]
        for item in items:
            if item.kind == CONTACT:
                continue
            for m in find_terms(item.text, entry["terms"]):
                findings.append(
                    _finding(
                        entry,
                        item,
                        f"{m.text!r} is {what} (rules.yaml {key}). Remove it.",
                        item.text,
                    )
                )
    return findings


def r7_inflation(
    items: list[Text], rules: dict[str, Any], master: dict[str, Any]
) -> list[Finding]:
    """R-7: no team-lead claims unless a source bullet of the same entry says so."""
    entry = rules["inflation_phrases"]
    findings: list[Finding] = []
    for item in items:
        if item.kind not in (SUMMARY, BULLET, LETTER, HEADLINE):
            continue
        hits = find_terms(item.text, entry["terms"])
        hits += [
            r
            for r in find_regex(item.text, entry.get("regex") or [])
            if not any(t.start < r.end and r.start < t.end for t in hits)
        ]
        for m in hits:
            if item.kind == BULLET and _source_says(master, item.entry, m.text):
                continue
            findings.append(
                _finding(
                    entry,
                    item,
                    f"{m.text!r} claims a lead role no source states. Remove it.",
                    item.text,
                )
            )
    return findings


def _source_says(master: dict[str, Any], entry_name: str, phrase: str) -> bool:
    """Return True if a source bullet of this entry contains `phrase`.

    The entry is matched by its org, display_org or title. Client entries
    ("(client, through X)") count for entry X, because a resume shows them under X
    (R-9).
    """
    name = entry_name.strip().lower()
    if not name:
        return False
    for entry in (master.get("roles") or []) + (master.get("projects") or []):
        names = {
            str(entry.get(k) or "").strip().lower()
            for k in ("org", "display_org", "title")
        }
        agent = re.search(r"\bthrough (\w+)", str(entry.get("org") or ""))
        under_agent = bool(agent and name.startswith(agent.group(1).lower()))
        if name in names or under_agent:
            for bullet in entry.get("bullets") or []:
                if term_regex(phrase).search(str(bullet.get("text") or "")):
                    return True
    return False


RULES: tuple[Rule, ...] = (
    r1_contact,
    r2_years,
    r3_banned_words,
    r3_banned_phrases,
    r4_pronouns,
    r5_education,
    r6_removed,
    r7_inflation,
)


def check(
    run: RunFiles,
    rules: dict[str, Any],
    master: dict[str, Any],
    questions: dict[str, list[str]] | None = None,
) -> list[Finding]:
    """Return all findings for a run.

    `questions` maps a fact id to the open questions that name it (R-17); the CLI
    reads it from questions.md. Raises InputError when rules.yaml or base.map.json
    lacks a key that the rules read (see `require_keys`).
    """
    require_keys(rules, run.the_map)
    items = list(texts(run.tailored, run.letter))
    findings = [f for rule in RULES for f in rule(items, rules, master)]
    ctx = build_context(run.tailored, run.base, run.the_map, rules, master)
    findings += [f for rule in STRUCTURE_RULES for f in rule(ctx)]
    findings += provenance_rules(ctx, run.letter, run.job)
    findings += r16_unverified(ctx)
    findings += r17_open_questions(ctx, questions or {})
    findings += r18_pages(ctx, run.manifest)
    return sorted(findings, key=lambda f: (_rule_number(f.rule), f.location))


def _rule_number(rule: str) -> int:
    return int(rule.split("-")[1])
