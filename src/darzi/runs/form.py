"""The form commands: `facts answers`, `facts fill-plan`, `facts submitted`.

darzi fills no form, opens no browser and makes no network call here. The agent
fills the form in the owner's browser only from the plan that `fill_plan()`
returns, and stops before Submit (docs/SPEC.md sections 1 and 6, C-3).

`fill_plan()` is the last gate before an agent puts files into an employer's
form. A manifest is not signed, and any process can write one. So the gate
trusts only the signed approval record (through `run_is_approved()`), the
content that it reads one time, and the files that it reads one time. It
returns the plan only when each check passes, in this order:

a. the run folder is `applications/<run>` of this data root, with no link at
   `run.yaml`, `manifest.json`, `out` or a file in `out/`; the run lock; the
   status `finalized`;
b. the signed approval of `tailored.json` and the cover letter in memory;
c. the manifest: its shape, `signed` exactly `true`, the run name, the
   approval hash and the signer fingerprint;
d. the files in `out/`: exactly the kinds and names that finalize writes, each
   hash, and no other entry;
e. the two Word files built again and byte-equal; the round-trip check again on
   the bytes that were hashed;
f. `answers.yaml` has no error;
g. an `apply_url` whose host is not a blocked host of `[form]`.

No parameter, flag, environment variable or test mode skips a check.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from darzi.config import FormSettings, host_name, load_config
from darzi.facts.check import MANIFEST, RunFiles, load_run, require_public_keys
from darzi.facts.public import privacy_findings
from darzi.facts.repo import InputError, read_yaml
from darzi.facts.validate import Finding
from darzi.paths import exists, is_dir, is_file
from darzi.render.word import letter_docx, resume_docx
from darzi.runs.approve import run_is_approved
from darzi.runs.finalize import (
    LETTER_WORD,
    MANIFEST_SCHEMA_VERSION,
    OUT,
    RESUME_PDF,
    RESUME_WORD,
)
from darzi.runs.lock import locked
from darzi.runs.roundtrip import check_letter, check_resume
from darzi.runs.runs import (
    APPLICATIONS,
    RUN_YAML,
    content_hash,
    load_meta,
    settings_of,
    write_status,
)
from darzi.runs.signing import in_data_root, run_target

ANSWERS = "answers.yaml"
# The largest answers.yaml that load_answers() reads: 256 KiB.
MAX_ANSWERS_YAML = 256 * 1024
# The largest manifest.json and output file that fill-plan reads.
MAX_MANIFEST = 256 * 1024
MAX_OUT_FILE = 64 * 1024 * 1024
PLAN_SCHEMA_VERSION = 1
ANSWERS_RULE = "D-40"
FINALIZED = "finalized"
SUBMITTED = "submitted"
# The keys of a manifest that finalize writes, and of each of its files.
MANIFEST_KEYS = frozenset(
    (
        "schema_version",
        "run",
        "date",
        "signed",
        "approval_hash",
        "signer_fingerprint",
        "renderer",
        "print_settings",
        "files",
    )
)
FILE_KEYS = frozenset(("path", "kind", "sha256", "pages", "reproducible"))
# The name of each output file, after the stem (the same rule as finalize).
SUFFIXES = {
    RESUME_PDF: "_Resume.pdf",
    RESUME_WORD: "_Resume.docx",
    LETTER_WORD: "_Cover_Letter.docx",
}
# Each of these names in the run folder must not be a symbolic link.
NO_LINKS = (RUN_YAML, MANIFEST, OUT)
SHA256 = re.compile(r"[0-9a-f]{64}")
URL_SCHEMES = ("http", "https")
RULES = (
    "Fill each field only from this plan: personal_info, answers and cover_letter.",
    "Upload only the files in this plan.",
    "Leave each field with no answer empty, and list it for the owner.",
    "Never click Submit or Apply, and never accept terms. The owner does that.",
    "Go only to apply_url. Stop on a host of [form] blocked_hosts, also after "
    "a redirect, and tell the owner.",
    "Write form_fill.md in the run folder: the fields that you filled and the "
    "fields that are still empty.",
)
UNSIGNED_NOTE = (
    "company, role and apply_url come from run.yaml. The owner's signature does "
    "not cover them. company and role are labels only: do not put them into "
    "the form. The answers and empty_answers come from answers.yaml: the owner "
    "writes them, but the signature does not cover them either."
)
ANSWERS_NOTE = "from answers.yaml, written by the owner, NOT covered by the signature"


class FormRefused(InputError):
    """A form command refuses: exit code 1. A plain InputError is exit code 2."""


@dataclass(frozen=True)
class Answers:
    """The parsed answers.yaml and the findings of its check."""

    data: dict[str, Any]
    findings: list[Finding]


# --- facts answers -------------------------------------------------------------


def load_answers(repo: Path) -> dict[str, Any]:
    """Return the parsed `answers.yaml` of `repo`.

    Raises InputError when the file is missing, larger than MAX_ANSWERS_YAML,
    uses a YAML alias, is not valid YAML, or is not a mapping.
    """
    path = repo / ANSWERS
    if path.is_symlink():
        raise InputError(
            f"{path} is a symbolic link. The form commands do not follow links. "
            f"Put the file itself at {ANSWERS}."
        )
    data: Any = read_yaml(
        path,
        "The owner writes the standard answers there (see the answers schema).",
        max_bytes=MAX_ANSWERS_YAML,
        aliases=False,
    )
    if not isinstance(data, dict):
        raise InputError(f"{path} is not a mapping. Write schema_version and answers.")
    return data


def run_companies(repo: Path) -> set[str]:
    """Return the company of each run folder of `repo`.

    Raises InputError when a `run.yaml` cannot be read: then the names that an
    answer must not use are not known.
    """
    names: set[str] = set()
    folder = repo / APPLICATIONS
    if not is_dir(folder):
        return names
    for run_dir in sorted(folder.iterdir()):
        if not is_file(run_dir / RUN_YAML):
            continue
        company: Any = load_meta(run_dir).get("company")
        if isinstance(company, str) and company.strip():
            names.add(company.strip())
    return names


def check_answers(
    data: dict[str, Any],
    schema: dict[str, Any],
    rules: dict[str, Any],
    companies: set[str],
) -> list[Finding]:
    """Return the findings of `answers.yaml`: the schema, ids, companies, PV-3.

    The schema comes first; with a schema error the other checks do not run.
    Then: each id one time only; no answer names the company of a run (whole
    word, any letter case); and the privacy scan (PV-3) on all answers.
    """
    findings = [
        Finding(
            ANSWERS_RULE,
            "error",
            "answers: " + (".".join(str(p) for p in e.absolute_path) or "(top level)"),
            f"{e.message}. Correct it to match the answers schema.",
        )
        for e in Draft202012Validator(schema).iter_errors(data)
    ]
    if findings:
        return findings
    seen: set[str] = set()
    for item in data["answers"]:
        where = f"answers: {item['id']}"
        if item["id"] in seen:
            findings.append(
                Finding(ANSWERS_RULE, "error", where, "This id is in the file twice.")
            )
        seen.add(item["id"])
        for company in sorted(companies):
            pattern = r"(?<!\w)" + re.escape(company) + r"(?!\w)"
            if re.search(pattern, item["answer"], re.IGNORECASE):
                findings.append(
                    Finding(
                        ANSWERS_RULE,
                        "error",
                        where,
                        f"This answer names the company {company!r} of a run. "
                        "Keep only general answers here; the owner types an "
                        "answer for one company in the form.",
                        item["answer"],
                    )
                )
    for f in privacy_findings(data["answers"], rules):
        findings.append(
            Finding(f.rule, f.severity, f"answers: {f.location}", f.message, f.text)
        )
    return findings


def answers_report(
    repo: Path, schema: dict[str, Any], rules: dict[str, Any]
) -> Answers:
    """Read and check `answers.yaml` of `repo`. Raises InputError (exit code 2)."""
    require_public_keys(rules)
    data = load_answers(repo)
    return Answers(data, check_answers(data, schema, rules, run_companies(repo)))


# --- facts fill-plan -------------------------------------------------------------


def fill_plan(
    repo: Path, run_dir: Path, schema: dict[str, Any], rules: dict[str, Any]
) -> dict[str, Any]:
    """Return the only data that a form fill can use, after each check passes.

    `schema` is the answers schema and `rules` the parsed rules.yaml. Raises
    InputError (exit code 2) for invalid input, a run folder that is not in
    the data root, a link, or no `[form]` table; FormRefused (exit code 1) for
    each other failed check. This function changes no file.
    """
    config = load_config(repo)
    # a. The run folder, the links, the lock and the status.
    _refuse_other_root(repo, run_dir)
    _refuse_links(run_dir)
    with locked(run_dir, FormRefused):
        meta = load_meta(run_dir)
        status: Any = meta.get("status")
        if status != FINALIZED:
            raise FormRefused(_status_message(run_dir, status))
        # b. The approval, decided on the content in memory.
        try:
            run = load_run(run_dir)
        except UnicodeDecodeError as exc:  # For example a manifest.json in Latin-1.
            raise InputError(
                f"a file of {run_dir} is not UTF-8 text: {exc.reason} at byte "
                f"{exc.start}. Run facts finalize {run_dir} again."
            ) from exc
        verdict = run_is_approved(repo, config.approval, run_dir, run)
        if not verdict.approved or verdict.fingerprint is None:
            raise FormRefused(
                f"{run_dir} is not approved: {verdict.reason or 'no key'}. The "
                f"owner runs facts approve {run_dir}, then facts finalize {run_dir}."
            )
        digest = content_hash(run.tailored, run.letter)
        # c. The manifest.
        manifest = _manifest(run_dir)
        _check_manifest(run_dir, manifest, digest, verdict.fingerprint)
        # d. The files in out/: each read one time.
        files = _read_files(run_dir, manifest["files"], run)
        # e. The Word files built again; the round-trip check again.
        _check_built(run_dir, run, meta, files)
        if content_hash(run.tailored, run.letter) != digest:
            raise FormRefused("the approved content changed in memory. Try again.")
        # f. answers.yaml.
        answers = answers_report(repo, schema, rules)
        errors = [f for f in answers.findings if f.severity == "error"]
        if errors:
            raise FormRefused(
                f"answers.yaml has {len(errors)} errors. Run facts answers, "
                "correct them, then run fill-plan again."
            )
        # g. The apply link and the blocked hosts.
        url = apply_url(meta, run_dir, config.form)
        return _plan(run_dir, run, meta, url, files, answers.data)


def _status_message(run_dir: Path, status: Any) -> str:
    if status == SUBMITTED:
        return (
            f"{run_dir} is already submitted. Do not fill the form again for this run."
        )
    return (
        f"{run_dir / RUN_YAML} has the status {status!r}, not finalized. Run "
        f"facts finalize {run_dir} first."
    )


def _refuse_other_root(repo: Path, run_dir: Path) -> None:
    """Raise InputError unless `run_dir` is a real `applications/<run>` of `repo`."""
    try:
        inside = in_data_root(repo, "run", run_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InputError(f"cannot read the path of {run_dir}: {exc}.") from exc
    if not inside:
        raise InputError(
            f"{run_dir} is not a run folder of the data root {repo}. The form "
            f"commands work only on a real folder {APPLICATIONS}/<run> of the "
            "data root that holds the key list. Pass the right run folder or --repo."
        )


def _refuse_links(run_dir: Path) -> None:
    """Raise InputError for a symbolic link at a name that the commands read."""
    if not is_dir(run_dir):
        raise InputError(f"{run_dir} is not a folder. Pass a run folder.")
    for name in NO_LINKS:
        if (run_dir / name).is_symlink():
            raise InputError(
                f"{run_dir / name} is a symbolic link. The form commands do not "
                "follow links. Remove the link, then run facts finalize again."
            )


def _read_once(path: Path, limit: int) -> bytes:
    """Return the bytes of the regular file `path`, read one time, no link.

    Raises OSError for a link, a file that is not regular, or a read error,
    and ValueError for a file larger than `limit`.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise OSError(f"{path.name} is not a regular file")
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path.name} is too large")
    return data


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key is in the manifest two times")
    return dict(pairs)


def _manifest(run_dir: Path) -> dict[str, Any]:
    """Return manifest.json, read one time, as a JSON object; or refuse."""
    path = run_dir / MANIFEST
    if not exists(path):
        raise FormRefused(f"{path} does not exist. Run facts finalize {run_dir} first.")
    try:
        data: Any = json.loads(
            _read_once(path, MAX_MANIFEST).decode("utf-8"),
            object_pairs_hook=_unique_keys,
        )
    except (OSError, ValueError, RecursionError) as exc:
        raise FormRefused(
            f"{path} cannot be read as a manifest: {exc}. Run facts finalize again."
        ) from exc
    if not isinstance(data, dict):
        raise FormRefused(f"{path} is not a JSON object. Run facts finalize again.")
    return data  # pyright: ignore[reportUnknownVariableType]


def _check_manifest(
    run_dir: Path, manifest: dict[str, Any], digest: str, fingerprint: str
) -> None:
    """Refuse a manifest that is not the signed manifest of this content."""
    path = run_dir / MANIFEST
    again = f"Run facts finalize {run_dir} again."
    version: Any = manifest.get("schema_version")
    if type(version) is not int or version != MANIFEST_SCHEMA_VERSION:
        raise FormRefused(
            f"{path} is not schema version {MANIFEST_SCHEMA_VERSION}. {again}"
        )
    signed: Any = manifest.get("signed")
    if signed is False:
        raise FormRefused(
            f"{path} is a hand-over manifest (signed: false). Its files are for "
            "the owner to use by hand; an agent cannot put them into a form."
        )
    if signed is not True:
        raise FormRefused(f"{path} does not say signed: true. {again}")
    if frozenset(manifest) != MANIFEST_KEYS:
        raise FormRefused(
            f"{path} does not have the keys that finalize writes. {again}"
        )
    if manifest["run"] != run_target(run_dir):
        raise FormRefused(f"{path} is for a different run. {again}")
    approval: Any = manifest["approval_hash"]
    if not isinstance(approval, str) or not hmac.compare_digest(approval, digest):
        raise FormRefused(f"{path} is for a different approval or content. {again}")
    key: Any = manifest["signer_fingerprint"]
    if not isinstance(key, str) or key != fingerprint:
        raise FormRefused(
            f"{path} names a different signer key than the approval. {again}"
        )
    if not isinstance(manifest["files"], list):
        raise FormRefused(f"{path} has no list of files. {again}")


def _file_stem(resume: dict[str, Any]) -> str:
    """Return the file name stem from the name in the resume (as finalize)."""
    name = str((resume.get("personalInfo") or {}).get("name") or "Resume")
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "Resume"


@dataclass(frozen=True)
class _OutFile:
    """One output file: its manifest entry and the bytes that were hashed."""

    path: str
    kind: str
    sha256: str
    data: bytes


def _read_files(run_dir: Path, items: list[Any], run: RunFiles) -> list[_OutFile]:
    """Check the manifest files against out/; return each file read one time."""
    again = f"Run facts finalize {run_dir} again."
    kinds = [RESUME_PDF, RESUME_WORD]
    if run.letter is not None:
        kinds.append(LETTER_WORD)
    stem = _file_stem(run.tailored)
    expected = {kind: f"{OUT}/{stem}{SUFFIXES[kind]}" for kind in kinds}
    entries: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or frozenset(item) != FILE_KEYS:  # pyright: ignore[reportUnknownArgumentType]
            raise FormRefused(f"a file entry of the manifest has wrong keys. {again}")
        entry: dict[str, Any] = item  # pyright: ignore[reportUnknownVariableType]
        kind: Any = entry["kind"]
        if not isinstance(kind, str) or kind in entries:
            raise FormRefused(f"the manifest names a file kind two times. {again}")
        entries[kind] = entry
    if sorted(entries) != sorted(kinds):
        raise FormRefused(
            f"the manifest does not name exactly the files of this run "
            f"({', '.join(kinds)}). {again}"
        )
    files: list[_OutFile] = []
    for kind in kinds:
        entry = entries[kind]
        if entry["path"] != expected[kind]:
            raise FormRefused(
                f"the manifest path of {kind} is not {expected[kind]}. {again}"
            )
        digest: Any = entry["sha256"]
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise FormRefused(f"the manifest hash of {kind} is not valid. {again}")
        path = run_dir / expected[kind]
        try:
            data = _read_once(path, MAX_OUT_FILE)
        except (OSError, ValueError) as exc:
            raise FormRefused(
                f"{path} is missing, a link, or cannot be read: {exc}. {again}"
            ) from exc
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest):
            raise FormRefused(
                f"{path} changed after finalize: its hash is not the manifest "
                f"hash. Never upload a changed file. {again}"
            )
        files.append(_OutFile(expected[kind], kind, digest, data))
    try:
        found = sorted(os.listdir(run_dir / OUT))
    except OSError as exc:
        raise FormRefused(f"cannot list {run_dir / OUT}: {exc}. {again}") from exc
    names = sorted(Path(f.path).name for f in files)
    if found != names:
        raise FormRefused(
            f"{run_dir / OUT} holds other entries than the manifest files. {again}"
        )
    return files


def _check_built(
    run_dir: Path, run: RunFiles, meta: dict[str, Any], files: list[_OutFile]
) -> None:
    """Build the Word files again and run the round-trip check on the bytes."""
    again = f"Run facts finalize {run_dir} again."
    settings = settings_of(meta, run_dir)
    with tempfile.TemporaryDirectory(prefix="darzi-fill-plan-") as tmp:
        built, copies = Path(tmp) / "built", Path(tmp) / "copies"
        built.mkdir()
        copies.mkdir()
        findings: list[Finding] = []
        for item in files:
            name = Path(item.path).name
            if item.kind == RESUME_WORD:
                resume_docx(run.tailored, built / name, settings)
            elif item.kind == LETTER_WORD and run.letter is not None:
                letter_docx(run.letter, built / name)
            if item.kind != RESUME_PDF and (built / name).read_bytes() != item.data:
                raise FormRefused(
                    f"{run_dir / item.path} is not the Word file that the approved "
                    f"content gives. {again}"
                )
            # The check reads a path: a new copy of the bytes that were hashed.
            copy = copies / name
            copy.write_bytes(item.data)
            if item.kind == LETTER_WORD and run.letter is not None:
                findings += check_letter(run.letter, copy)
            else:
                findings += check_resume(run.tailored, copy)
        if findings:
            raise FormRefused(
                f"the round-trip check found {len(findings)} problems in "
                f"{run_dir / OUT}: a file does not show the approved text. {again}"
            )


def apply_url(meta: dict[str, Any], run_dir: Path, form: FormSettings | None) -> str:
    """Return the `apply_url` of `run.yaml` when its host is not blocked.

    Raises InputError (exit code 2) when facts.toml has no `[form]` table, and
    FormRefused (exit code 1) when the link is absent, is not a plain http or
    https link to a host name, or its host is a blocked host or a sub-domain
    of one.
    """
    if form is None:
        raise InputError(
            "facts.toml has no [form] table. Add [form] with blocked_hosts = "
            "[...] (or blocked_hosts = [] for no blocked host)."
        )
    where = run_dir / RUN_YAML
    url: Any = meta.get("apply_url")
    if url is None or url == "":
        raise FormRefused(
            f"{where} has no apply_url. Write apply_url: <the link of the "
            "application form> in it, then run fill-plan again."
        )
    if not isinstance(url, str):
        raise FormRefused(f"{where}: apply_url is not a text. Write the link.")
    host = _url_host(url)
    if host is None:
        raise FormRefused(
            f"{where}: apply_url must be a plain http or https link to a host "
            "name, with no user name, space, backslash or control character."
        )
    for blocked in form.blocked_hosts:
        if host == blocked or host.endswith("." + blocked):
            raise FormRefused(
                f"the host of apply_url is {host}, which [form] blocked_hosts "
                "blocks. No form fill there: the owner applies by hand."
            )
    return url


def _url_host(url: str) -> str | None:
    """Return the host name of `url` in the form of `host_name()`, or None."""
    for c in url:
        if c == "\\" or c.isspace() or unicodedata.category(c) in ("Cc", "Cf"):
            return None
    try:
        parts = urlsplit(url)
        _ = parts.port  # Raises ValueError for a port that is not valid.
    except ValueError:
        return None
    if parts.scheme not in URL_SCHEMES or "@" in parts.netloc:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if not parts.hostname:
        return None
    return host_name(parts.hostname)


def _plan(
    run_dir: Path,
    run: RunFiles,
    meta: dict[str, Any],
    url: str,
    files: list[_OutFile],
    answers: dict[str, Any],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = answers["answers"]
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "command": "fill-plan",
        "run": run_target(run_dir),
        "files": [{"path": f.path, "kind": f.kind, "sha256": f.sha256} for f in files],
        "personal_info": run.tailored.get("personalInfo") or {},
        "cover_letter": run.letter,
        "answers": [
            {"id": a["id"], "question": a["question"], "answer": a["answer"]}
            for a in items
            if a["answer"].strip()
        ],
        "empty_answers": [
            {"id": a["id"], "question": a["question"]}
            for a in items
            if not a["answer"].strip()
        ],
        "rules": list(RULES),
        "not_signed": {
            "note": UNSIGNED_NOTE,
            "company": meta.get("company"),
            "role": meta.get("role"),
            "apply_url": url,
        },
    }


# --- facts submitted ---------------------------------------------------------------


def mark_submitted(repo: Path, run_dir: Path, day: dt.date) -> dict[str, Any]:
    """Write `status: submitted` and the date `day` in `run.yaml` of `run_dir`.

    Only for a run of this data root with the status `finalized` and a
    manifest (signed true or false). The run lock is held, and `run.yaml` is
    read inside it. A second call is refused and changes nothing. Raises
    InputError (exit code 2) or FormRefused (exit code 1).
    """
    load_config(repo)
    _refuse_other_root(repo, run_dir)
    _refuse_links(run_dir)
    with locked(run_dir, FormRefused):
        meta = load_meta(run_dir)
        status: Any = meta.get("status")
        if status == SUBMITTED:
            raise FormRefused(f"{run_dir} is already submitted. Nothing changed.")
        if status != FINALIZED:
            raise FormRefused(_status_message(run_dir, status))
        manifest = _manifest(run_dir)
        if not isinstance(manifest.get("signed"), bool):
            raise FormRefused(
                f"{run_dir / MANIFEST} has no signed value. Run facts finalize again."
            )
        new = dict(meta)
        new["submitted"] = day.isoformat()
        write_status(run_dir, new, SUBMITTED)
    return {"run": run_target(run_dir), "status": SUBMITTED, "submitted": str(day)}
