"""Fit analysis: check `requirements.json` and rate the fit (docs/SPEC.md section 14).

The agent reads the job post and writes `requirements.json`. This module checks
that the input is real (each quote is in `job.md`, each id is in the public
copy of the facts or in `answers.yaml`) and does the arithmetic. It writes
`fit.json` only when each check passes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from darzi.config import FitSettings, exact, load_config
from darzi.facts.check import require_public_keys
from darzi.facts.public import ENTRY_SECTIONS, privacy_findings, public_copy
from darzi.facts.repo import InputError, load_master, load_rules
from darzi.facts.validate import Finding
from darzi.paths import exists, is_dir, write_atomic
from darzi.runs.form import load_answers
from darzi.runs.lock import locked
from darzi.runs.runs import load_meta
from darzi.runs.signing import in_data_root, run_target
from darzi.schemas import ANSWERS, REQUIREMENTS, load_schema

JOB = "job.md"
REQUIREMENTS_JSON = "requirements.json"
FIT_JSON = "fit.json"
ANSWERS_YAML = "answers.yaml"
# Each of these names in the run folder must not be a symbolic link.
NO_LINKS = (JOB, REQUIREMENTS_JSON, FIT_JSON)
# The largest requirements.json and job.md that `facts fit` reads.
MAX_REQUIREMENTS = 256 * 1024
MAX_JOB = 16 * 1024 * 1024
FIT_SCHEMA_VERSION = 1
SUBMITTED = "submitted"
SKILL = "skill:"
ANSWER = "answers:"
FEW_WANTED = 5
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SOFT_HYPHEN = "­"


class FitRefused(InputError):
    """`facts fit` found errors or refuses the run: exit code 1."""

    def __init__(self, problems: list[str]) -> None:
        """Keep `problems`: one line for each problem."""
        super().__init__(" ".join(problems))
        self.problems = problems


class FitInvalid(InputError):
    """The input of `facts fit` is not valid: exit code 2. One line per problem."""

    def __init__(self, problems: list[str]) -> None:
        """Keep `problems`: one line for each problem."""
        super().__init__(" ".join(problems))
        self.problems = problems


class PublicLeak(InputError):
    """The privacy scan found private data in the public copy: exit code 1."""

    def __init__(self, findings: list[Finding]) -> None:
        """Keep the `findings` of the privacy scan."""
        super().__init__("the public copy holds private data")
        self.findings = findings


@dataclass(frozen=True)
class FitResult:
    """The content of `fit.json` and the bytes that `facts fit` wrote."""

    report: dict[str, Any]
    data: bytes


def normalize(text: str) -> str:
    """Return `text` in the form that the quote check compares (rule 1).

    The steps: Unicode NFKC, soft hyphens removed, letter case folded, each run
    of white space (line ends too) as one space, no space at the ends. Quote
    marks and dashes stay as they are.
    """
    text = unicodedata.normalize("NFKC", text).replace(SOFT_HYPHEN, "")
    return " ".join(text.casefold().split())


def rating(
    hard_not_met: bool, with_evidence: int, wanted: int, fit: FitSettings
) -> str:
    """Return red, yellow or green (rule 6). `wanted` must be above 0.

    The coverage is the exact fraction `with_evidence / wanted`. It is compared
    with the exact decimal value of each limit.
    """
    if hard_not_met:
        return "red"
    coverage = Fraction(with_evidence, wanted)
    if coverage >= exact(fit.green):
        return "green"
    if coverage >= exact(fit.yellow):
        return "yellow"
    return "red"


def fit(repo: Path, run_dir: Path) -> FitResult:
    """Check `requirements.json` of `run_dir`, rate the fit, and write `fit.json`.

    Raises FitInvalid or InputError (exit code 2) for invalid input, FitRefused
    or PublicLeak (exit code 1) for errors found. On each error, no file is
    written and an old `fit.json` stays as it was.
    """
    settings = load_config(repo).fit
    _refuse_other_root(repo, run_dir)
    with locked(run_dir, _Busy):
        status: Any = load_meta(run_dir).get("status")
        if status == SUBMITTED:
            raise FitRefused(
                [f"{run_dir} is already submitted. A submitted run does not change."]
            )
        raw = _read(run_dir / REQUIREMENTS_JSON, MAX_REQUIREMENTS, _MISSING_REQ)
        items = _requirements(raw)
        job_bytes = _read(run_dir / JOB, MAX_JOB, "Make the run with facts new-run.")
        job = _text(job_bytes, JOB)
        public = _public(repo)
        answers = _answers(repo) if _needs_answers(items) else None
        report = _report(run_dir, items, job, public, answers, settings)
        report["inputs"] = {
            JOB: hashlib.sha256(job_bytes).hexdigest(),
            REQUIREMENTS_JSON: hashlib.sha256(raw).hexdigest(),
        }
        report = _ordered(report)
        try:
            text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
            data = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise FitInvalid(
                ["a text holds a character that is not valid Unicode. Remove it."]
            ) from exc
        write_atomic(run_dir / FIT_JSON, data)
    return FitResult(report, data)


class _Busy(FitRefused):
    def __init__(self, message: str) -> None:
        super().__init__([message])


_MISSING_REQ = (
    "Read job.md and write requirements.json in the run folder (see the "
    "requirements schema), then run facts fit again."
)


def _refuse_other_root(repo: Path, run_dir: Path) -> None:
    """Raise InputError unless `run_dir` is a real `applications/<run>` of `repo`."""
    try:
        inside = in_data_root(repo, "run", run_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InputError(f"cannot read the path of {run_dir}: {exc}.") from exc
    if not inside or not is_dir(run_dir):
        raise InputError(
            f"{run_dir} is not a run folder of the data root {repo}. Pass a "
            "folder applications/<run> of this data root, or --repo."
        )
    for name in NO_LINKS:
        if (run_dir / name).is_symlink():
            raise InputError(
                f"{run_dir / name} is a symbolic link. facts fit does not follow "
                "links. Put the file itself there."
            )


def _read(path: Path, limit: int, missing: str) -> bytes:
    """Return the bytes of the regular file `path`, not through a link.

    Raises InputError when the file is missing, is a link, is not a regular
    file, cannot be read, or is larger than `limit` bytes.
    """
    if not exists(path) and not path.is_symlink():
        raise InputError(f"{path} does not exist. {missing}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise InputError(f"{path} is not a regular file. Put a file there.")
            data = f.read(limit + 1)
    except OSError as exc:
        raise InputError(
            f"{path} cannot be read: {exc.strerror}. It must be a file, not a link."
        ) from exc
    if len(data) > limit:
        raise InputError(f"{path} is larger than {limit // 1024} KiB. Make it shorter.")
    return data


def _text(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputError(f"{name} is not UTF-8 text. Save it as UTF-8.") from exc


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key is in one object twice")
    return dict(pairs)


def _requirements(raw: bytes) -> list[dict[str, Any]]:
    """Return the requirements of `requirements.json`, after each input check.

    Raises FitInvalid with one line for each problem.
    """
    text = _text(raw, REQUIREMENTS_JSON)
    try:
        data: Any = json.loads(text, object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError) as exc:
        raise FitInvalid(
            [f"{REQUIREMENTS_JSON} is not valid JSON: {exc}. Fix the JSON."]
        ) from exc
    validator = Draft202012Validator(load_schema(REQUIREMENTS))
    errors = sorted(validator.iter_errors(data), key=_error_order)
    if errors:
        raise FitInvalid([_schema_line(data, e) for e in errors])
    items: list[dict[str, Any]] = data["requirements"]
    problems: list[str] = []
    seen: set[str] = set()
    for item in items:
        rid: str = item["id"]
        if not ID.fullmatch(rid):
            problems.append(f"the id {rid!r} has a character that is not permitted.")
        elif rid in seen:
            problems.append(f"{rid}: this id is in {REQUIREMENTS_JSON} twice.")
        seen.add(rid)
        if not normalize(item["quote"]):
            problems.append(f"{rid}: the quote has no visible text.")
        if any(_bad_text(v) for v in _values(item)):
            problems.append(f"{rid}: a text holds a character that is not valid.")
    if not any(item["kind"] == "wanted" for item in items):
        problems.append(
            f"{REQUIREMENTS_JSON} has no wanted requirement. List each skill and "
            "each experience that the job post asks for, as kind wanted with its "
            "evidence (an empty list when there is none)."
        )
    if problems:
        raise FitInvalid(problems)
    return items


def _values(item: dict[str, Any]) -> list[str]:
    values = [item["id"], item["quote"], item.get("basis") or ""]
    return values + list(item.get("evidence") or [])


def _bad_text(value: str) -> bool:
    """Return True for a lone surrogate, which no UTF-8 file can hold."""
    return any(0xD800 <= ord(c) <= 0xDFFF for c in value)


def _error_order(error: ValidationError) -> list[tuple[int, int, str]]:
    """Return a sort key: the path of the error, item numbers in number order."""
    return [
        (0, p, "") if isinstance(p, int) else (1, 0, str(p))
        for p in error.absolute_path
    ]


def _schema_line(data: Any, error: ValidationError) -> str:
    """Return one short line for one schema error of `requirements.json`."""
    path = list(error.absolute_path)
    where = "requirements.json"
    if len(path) >= 2 and path[0] == "requirements" and isinstance(path[1], int):
        item: Any = data["requirements"][path[1]]
        rid: Any = item.get("id") if isinstance(item, dict) else None
        label = rid if isinstance(rid, str) and ID.fullmatch(rid) else f"#{path[1] + 1}"
        where = f"requirement {label}"
        if error.message.startswith("False schema") and isinstance(item, dict):
            # A key that the kind does not permit. The error path of a false
            # schema can stop at the item, so the key comes from the item.
            kind: Any = item.get("kind")
            refused = ("evidence",) if kind == "hard" else ("status", "basis")
            keys = ", ".join(k for k in refused if k in item)
            return f"{where}: {keys} is not permitted on a {kind} requirement."
        if len(path) >= 3:
            key = path[2]
            value: Any = error.instance
            if key == "basis" and isinstance(value, str) and value.startswith(SKILL):
                return (
                    f"{where}: a basis cannot be a skill. Write answers:<id> or a "
                    "fact id."
                )
            where = f"{where}: {'.'.join(str(p) for p in path[2:])}"
    elif path:
        where = f"requirements.json: {'.'.join(str(p) for p in path)}"
    return f"{where}: {error.message}. Correct it to match the requirements schema."


def _needs_answers(items: list[dict[str, Any]]) -> bool:
    return any(str(item.get("basis") or "").startswith(ANSWER) for item in items)


def _public(repo: Path) -> dict[str, Any]:
    """Return the public copy, the same as `facts public`. Raises PublicLeak."""
    rules = load_rules(repo)
    require_public_keys(rules)
    data = public_copy(load_master(repo), rules)
    leaks = privacy_findings(data, rules)
    if leaks:
        raise PublicLeak(leaks)
    return data


def _answers(repo: Path) -> dict[str, dict[str, Any]]:
    """Return each answer of `answers.yaml` by its id. No file gives {}.

    Raises InputError for an answers.yaml that is a link, cannot be read, or
    is not valid against the answers schema.
    """
    path = repo / ANSWERS_YAML
    if not exists(path) and not path.is_symlink():
        return {}
    data = load_answers(repo)
    errors = list(Draft202012Validator(load_schema(ANSWERS)).iter_errors(data))
    if errors:
        raise InputError(
            f"{path} is not valid against the answers schema: {errors[0].message}. "
            "Run facts answers and correct it."
        )
    found: dict[str, dict[str, Any]] = {}
    for item in data["answers"]:
        found.setdefault(item["id"], item)
    return found


def _facts(public: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Return the bullet texts and the basis texts of the public copy, by id."""
    bullets: dict[str, str] = {}
    basis: dict[str, str] = {}
    for section in ENTRY_SECTIONS:
        for entry in public.get(section) or []:
            title = str(entry.get("title") or "")
            org = str(entry.get("org") or "")
            basis.setdefault(
                str(entry.get("id")), ", ".join(x for x in (title, org) if x)
            )
            for bullet in entry.get("bullets") or []:
                text = " ".join(str(bullet.get("text") or "").split())
                bullets.setdefault(str(bullet.get("id")), text)
    for bid, text in bullets.items():
        basis.setdefault(bid, text)
    for edu in public.get("education") or []:
        parts = (str(edu.get("credential") or ""), str(edu.get("institution") or ""))
        basis.setdefault(str(edu.get("id")), ", ".join(x for x in parts if x))
    return bullets, basis


def _not_public(rid: str, what: str, ref: str) -> str:
    # The same message for a private id and an unknown id. It names only the
    # id that the agent wrote, never a text of master.yaml.
    return (
        f'{rid}: {what} id "{ref}" is not in the public copy of the facts. '
        "Use an id that facts public prints."
    )


def _report(
    run_dir: Path,
    items: list[dict[str, Any]],
    job: str,
    public: dict[str, Any],
    answers: dict[str, dict[str, Any]] | None,
    settings: FitSettings,
) -> dict[str, Any]:
    """Return the content of fit.json. Raises FitRefused with each error."""
    bullets, facts = _facts(public)
    skills = {str(s.get("name")) for s in public.get("skills") or []}
    post = normalize(job)
    errors: list[str] = []
    warnings: list[str] = []
    out: list[dict[str, Any]] = []
    for item in items:
        rid: str = item["id"]
        # Rule 1: the quote is in the job post.
        if normalize(item["quote"]) not in post:
            errors.append(
                f"{rid}: the quote is not in job.md. Copy the words exactly from "
                "one line or one list item of job.md."
            )
        entry: dict[str, Any] = {
            "id": rid,
            "kind": item["kind"],
            "quote": item["quote"],
        }
        if item["kind"] == "wanted":
            evidence: list[dict[str, str]] = []
            for ref in item["evidence"]:
                # Rule 2: each evidence id is in the public copy.
                if ref.startswith(SKILL) and ref[len(SKILL) :] in skills:
                    evidence.append({"ref": ref, "text": ref[len(SKILL) :]})
                elif not ref.startswith(SKILL) and ref in bullets:
                    evidence.append({"ref": ref, "text": bullets[ref]})
                else:
                    errors.append(_not_public(rid, "evidence", ref))
            entry["evidence"] = evidence
        else:
            entry |= _hard(item, facts, answers, errors, warnings)
        out.append(entry)
    if errors:
        raise FitRefused(errors)
    return _arithmetic(run_dir, out, warnings, settings)


def _hard(
    item: dict[str, Any],
    facts: dict[str, str],
    answers: dict[str, dict[str, Any]] | None,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    """Return the fit.json keys of one hard requirement (rules 2 and 3)."""
    rid: str = item["id"]
    status: str = item["status"]
    ref: str | None = item.get("basis")
    basis: dict[str, str] | None = None
    if ref is not None and ref.startswith(ANSWER):
        answer = (answers or {}).get(ref[len(ANSWER) :])
        if answer is None:
            errors.append(
                f'{rid}: basis "{ref}" is not an answer id of answers.yaml. '
                "Use an id that is in answers.yaml."
            )
        else:
            basis = {
                "ref": ref,
                "question": str(answer["question"]),
                "answer": str(answer["answer"]),
            }
    elif ref is not None:
        if ref in facts:
            basis = {"ref": ref, "text": facts[ref]}
        else:
            errors.append(_not_public(rid, "basis", ref))
    counted = status
    if status != "unknown" and ref is None:
        # Rule 3: met or not_met with no basis counts as unknown.
        counted = "unknown"
        warnings.append(
            f"{rid}: the status {status} has no basis, so it counts as unknown. "
            "Add a basis: answers:<id> or a fact id."
        )
    return {"status": status, "counted_as": counted, "basis": basis}


def _arithmetic(
    run_dir: Path,
    out: list[dict[str, Any]],
    warnings: list[str],
    settings: FitSettings,
) -> dict[str, Any]:
    """Return fit.json without `inputs` (rules 4 to 7)."""
    wanted = [r for r in out if r["kind"] == "wanted"]
    with_evidence = sum(1 for r in wanted if r["evidence"])
    hard = [r for r in out if r["kind"] == "hard"]
    not_met = [_short(r) for r in hard if r["counted_as"] == "not_met"]
    unknown = [_short(r) for r in hard if r["counted_as"] == "unknown"]
    if len(wanted) < FEW_WANTED:
        warnings.append(
            f"only {len(wanted)} wanted requirements. A short list can hide gaps: "
            "list each skill and each experience that the post asks for."
        )
    return {
        "schema_version": FIT_SCHEMA_VERSION,
        "run": run_target(run_dir),
        "rating": rating(bool(not_met), with_evidence, len(wanted), settings),
        "limits": {"green": settings.green, "yellow": settings.yellow},
        "counts": {"wanted": len(wanted), "wanted_with_evidence": with_evidence},
        "coverage": round(with_evidence / len(wanted), 4),
        "requirements": out,
        "hard_not_met": not_met,
        "hard_unknown": unknown,
        "gaps": [_short(r) for r in wanted if not r["evidence"]],
        "warnings": warnings,
    }


def _short(item: dict[str, Any]) -> dict[str, str]:
    return {"id": item["id"], "quote": item["quote"]}


# The fixed key order of fit.json.
KEYS = (
    "schema_version",
    "run",
    "inputs",
    "rating",
    "limits",
    "counts",
    "coverage",
    "requirements",
    "hard_not_met",
    "hard_unknown",
    "gaps",
    "warnings",
)


def _ordered(report: dict[str, Any]) -> dict[str, Any]:
    return {key: report[key] for key in KEYS}
