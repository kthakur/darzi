"""`facts finalize RUN` and `facts finalize RUN --handover`.

The command is in parts (docs/SPEC.md sections 7, 8 and 9):

1. `finalize()` is the signed gate. It reads the run inputs one time into
   memory and decides "approved" with `run_is_approved()` on that content.
   Then it runs `unreviewable()` and `facts check` on the same content.
2. `finalize_handover()` is the hand-over gate (W-13). It reads no approval.
   It refuses a run with a signed manifest, runs `unreviewable()`, a scan of
   the fields that `facts check` does not read (`handover_scan()`), and
   `facts check`. After the commit, it copies the output files to
   `[handover] copy_to` when that is set.
3. `_build_and_commit()` is private. It takes the content that a gate passed
   and the approval fields of the manifest from that gate, with no default.
   It builds the files in a work folder, counts their pages, runs the
   round-trip check on each file, and only then moves the files to `out/` and
   writes `manifest.json` and the status `finalized`.

Each gate writes its own `signed` value as a literal: the signed gate `True`
with `approval_hash` and `signer_fingerprint`, the hand-over gate `False` with
neither key. Neither gate has a parameter, flag, environment variable or test
mode that changes this. Only the command-line flag `--handover` calls
`finalize_handover()`.

The order of the writes:

- Nothing in the run folder changes before each check passes and each file is
  built and tested, except the removal of the work folders that a crashed
  finalize left (after the gate).
- Then: remove the old `manifest.json`; move `out/` to `.finalize-old/`, move
  `.finalize-new/` to `out/`, remove `.finalize-old/`; write the new
  `manifest.json` (temporary file, then rename); write `status: finalized` in
  `run.yaml` (temporary file, then rename).
- So no reader ever sees new files with an old manifest.

The lock that stops two finalize runs of one run folder is a file in the
system temporary folder (see `lock_path` in `darzi.runs.lock`), not in the run
folder. So it is never an output, and a refusal leaves the run folder as it
was. `fill-plan` and `submitted` take the same lock. finalize reads `run.yaml`
again inside the lock, so a run that became submitted meanwhile is refused.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pwd
import re
import shutil
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from darzi.config import HandoverSettings, RenderSettings, load_config
from darzi.facts.check import MANIFEST, RunFiles, check, load_run, require_public_keys
from darzi.facts.check_meta import load_open_questions
from darzi.facts.public import privacy_findings
from darzi.facts.repo import InputError, load_master, load_rules
from darzi.facts.validate import Finding
from darzi.paths import exists, is_dir, is_file, write_atomic
from darzi.render.resume_matcher import ADAPTER, ResumeMatcher
from darzi.render.word import (
    DEFAULT_LEVELS,
    FONT_SIZE_PX,
    letter_docx,
    resume_docx,
)
from darzi.runs.approve import run_is_approved, unreviewable
from darzi.runs.lock import lock_path as lock_path
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

OUT = "out"
WORK_NEW = ".finalize-new"
WORK_OLD = ".finalize-old"
MANIFEST_SCHEMA_VERSION = 1
# A run with one of these status values can be finalized.
FINALIZABLE = ("new", "finalized")
# Each of these names in the run folder must not be a symbolic link.
NO_LINKS = (OUT, WORK_NEW, WORK_OLD, MANIFEST, RUN_YAML)
# When one of these names exists in the run folder, it must be of this kind.
KINDS = {MANIFEST: "file", OUT: "folder", WORK_NEW: "folder", WORK_OLD: "folder"}

# The kinds of the output files, and whether a second build of the same
# content gives the same bytes. The Word files have fixed zip dates (W-7).
# Chrome writes a creation date into the PDF.
RESUME_PDF = "resume_pdf"
RESUME_WORD = "resume_word"
LETTER_WORD = "letter_word"
REPRODUCIBLE = {RESUME_PDF: False, RESUME_WORD: True, LETTER_WORD: True}

# Makes the renderer from the data root and its [render] settings.
RendererFor = Callable[[Path, RenderSettings | None], ResumeMatcher]
# Counts the pages of a Word file (LibreOffice in production).
CountPages = Callable[[Path], int]
# Tells whether the page counter can run (LibreOffice is present).
OfficeReady = Callable[[], bool]


class Refused(InputError):
    """`finalize` refuses the run: exit code 1.

    Nothing in the run folder changed, except when a commit step fails after
    the old manifest was removed: then the message says so, and a new
    finalize repairs the run. A plain InputError is invalid input: exit code 2.
    """


class RoundTripFailed(Refused):
    """The round-trip check found approved text that a file does not show."""

    def __init__(self, findings: list[Finding]) -> None:
        """Keep the findings for the report."""
        super().__init__(
            f"the round-trip check found {len(findings)} problems. No file in "
            "out/ and no manifest changed. Correct the cause, then finalize again."
        )
        self.findings = findings


class CopyFailed(Refused):
    """The run is finalized, but the copy to `[handover] copy_to` failed."""


@dataclass(frozen=True)
class Finalized:
    """The result of a finalize: the manifest that was written, and warnings.

    `copied_to` is the folder with the copy of the hand-over files, or None.
    """

    manifest: dict[str, Any]
    warnings: list[str]
    copied_to: Path | None = None


@dataclass(frozen=True)
class _Passed:
    """The content that the gate passed. Only `finalize()` makes one."""

    run_dir: Path
    run: RunFiles
    meta: dict[str, Any]
    digest: str
    settings: dict[str, Any]


def _refuse_other_root(repo: Path, run_dir: Path) -> None:
    """Raise InputError unless `run_dir` is `applications/<name>` of `repo`.

    The real path of the run folder (each symbolic link followed) must be a
    direct child of the `applications` folder in the real path of the data
    root whose facts.toml and allowed_signers finalize uses. A run folder that
    is a symbolic link is refused too.
    """
    try:
        inside = in_data_root(repo, "run", run_dir)
    except (OSError, RuntimeError) as exc:
        raise InputError(f"cannot read the path of {run_dir}: {exc}.") from exc
    if not inside:
        raise InputError(
            f"{run_dir} is not a run folder of the data root {repo}. finalize "
            f"works only on a real folder {APPLICATIONS}/<run> of the data root "
            "that holds the key list. Pass the right run folder or --repo."
        )


def _refuse_links(run_dir: Path) -> None:
    """Raise InputError for a name that finalize writes: a link, or a wrong kind."""
    if not is_dir(run_dir):
        raise InputError(f"{run_dir} is not a folder. Pass a run folder.")
    for name in NO_LINKS:
        if (run_dir / name).is_symlink():
            raise InputError(
                f"{run_dir / name} is a symbolic link. finalize does not follow "
                "links out of the run folder. Remove the link."
            )
    for name, kind in KINDS.items():
        path = run_dir / name
        right = is_file(path) if kind == "file" else is_dir(path)
        if exists(path) and not right:
            raise InputError(f"{path} is not a {kind}. Remove it, then finalize again.")


def finalize(
    repo: Path,
    run_dir: Path,
    *,
    renderer_for: RendererFor,
    count_pages: CountPages,
    office_ready: OfficeReady,
    today: dt.date,
) -> Finalized:
    """Build the files of an approved run in `out/` and write `manifest.json`.

    This is the gate. The approval is decided with `run_is_approved()` on the
    content that this function reads one time; the build uses the same
    objects. `renderer_for`, `count_pages` and `office_ready` change only how
    the files are built, never the approval.

    Raises InputError (exit code 2) first when `run_dir` is not a real folder
    `applications/<run>` of `repo`. Raises Refused (exit code 1) when the run
    is submitted, another finalize
    holds the lock, the run is not approved, the review cannot show a part of
    it, `facts check` has an error, LibreOffice is absent, or a file fails;
    RoundTripFailed (a Refused) with the findings of the round-trip check;
    RenderError (exit code 1) when the renderer fails; and InputError (exit
    code 2) for invalid input.
    """
    # Step 1: facts.toml and run.yaml. A submitted run is never changed.
    config = load_config(repo)
    _refuse_other_root(repo, run_dir)
    _refuse_links(run_dir)
    meta = load_meta(run_dir)
    status: Any = meta.get("status")
    if status not in FINALIZABLE:
        raise Refused(
            f"{run_dir / RUN_YAML} has the status {status!r}. finalize works "
            "only on a run with the status new or finalized."
        )
    # Step 2: one finalize at a time for this run folder.
    with locked(run_dir, Refused):
        # Step 2b: read run.yaml again inside the lock. A submitted run is
        # never changed, also when it became submitted after step 1.
        meta = load_meta(run_dir)
        status = meta.get("status")
        if status not in FINALIZABLE:
            raise Refused(
                f"{run_dir / RUN_YAML} has the status {status!r}. finalize works "
                "only on a run with the status new or finalized."
            )
        # Step 3: read the inputs ONE time. Each later step uses `run`.
        run = load_run(run_dir)
        # Step 4: the approval, decided on the content in memory.
        verdict = run_is_approved(repo, config.approval, run_dir, run)
        if not verdict.approved or verdict.fingerprint is None:
            reason = verdict.reason or "no key fingerprint"
            raise Refused(
                f"{run_dir} is not approved: {reason}. "
                f"The owner runs: facts approve {run_dir}"
            )
        # Step 5: refuse a part that the review cannot show.
        hidden = unreviewable(run.tailored)
        if hidden:
            raise Refused(
                "the review cannot show these parts of tailored.json: "
                + ", ".join(hidden)
                + ". Remove them, then approve again."
            )
        # Step 6: facts check on the same content. check.json is not written.
        findings = check(
            run, load_rules(repo), load_master(repo), load_open_questions(repo)
        )
        errors = [f for f in findings if f.severity == "error"]
        if errors:
            raise Refused(
                f"facts check has {len(errors)} errors. Run facts check {run_dir}, "
                "correct them, then approve again."
            )
        # Step 7: the print settings from the run.yaml in memory, the renderer.
        passed = _Passed(
            run_dir=run_dir,
            run=run,
            meta=meta,
            digest=content_hash(run.tailored, run.letter),
            settings=settings_of(meta, run_dir),
        )
        renderer = renderer_for(repo, config.render)
        signing = {
            "signed": True,
            "approval_hash": passed.digest,
            "signer_fingerprint": verdict.fingerprint,
        }
        return _build_and_commit(
            passed, renderer, count_pages, office_ready, today, signing
        )


# --- The hand-over path (W-13) ------------------------------------------------

# The fields that `facts check` does not read, with their sections.
SCAN_FIELDS = (
    ("workExperience", ("website", "github")),
    ("personalProjects", ("website", "github", "location")),
    ("education", ("location",)),
)
# The name of each output file, after the stem.
COPY_SUFFIXES = ("_Resume.pdf", "_Resume.docx", "_Cover_Letter.docx")


def finalize_handover(
    repo: Path,
    run_dir: Path,
    *,
    renderer_for: RendererFor,
    count_pages: CountPages,
    office_ready: OfficeReady,
    today: dt.date,
) -> Finalized:
    """Build the files of a run with no approval, for the owner to use by hand.

    This is the hand-over gate. It reads no approval and writes `"signed":
    false` with no `approval_hash` and no `signer_fingerprint`. `fill-plan`
    refuses such a run. The steps are those of `finalize()`, with these
    differences: the `[handover] copy_to` folder is tested before anything
    changes; a run whose manifest is not a hand-over manifest is refused (a
    hand-over never replaces a signed result); `handover_scan()` runs after
    `unreviewable()`; and after the commit the output files are copied to
    `<copy_to>/<run folder name>/` when copy_to is set.

    Raises InputError (exit code 2) for invalid input, a run folder that is
    not in the data root, a link, or a copy_to folder that fails a test;
    Refused (exit code 1) for each refusal; CopyFailed (a Refused) when the
    copy fails after the commit; RoundTripFailed and RenderError as finalize.
    """
    # Steps 1 to 3: facts.toml, the binding to the data root, the links.
    config = load_config(repo)
    _refuse_other_root(repo, run_dir)
    _refuse_links(run_dir)
    # Step 4: the copy folder, before anything changes.
    copy_dir = copy_folder(repo, config.handover)
    if copy_dir is not None:
        _refuse_copy_target(copy_dir / run_dir.name, [])
    # Step 5: the status.
    _refuse_status(run_dir, load_meta(run_dir))
    with locked(run_dir, Refused):
        # Step 6: run.yaml again inside the lock.
        meta = load_meta(run_dir)
        _refuse_status(run_dir, meta)
        # Step 7: a hand-over never replaces a signed result.
        _refuse_signed_manifest(run_dir)
        # Step 8: read the inputs ONE time. Each later step uses `run`.
        run = load_run(run_dir)
        if copy_dir is not None:
            stem = _file_stem(run.tailored)
            names = [f"{stem}{end}" for end in COPY_SUFFIXES]
            _refuse_copy_target(copy_dir / run_dir.name, names)
        # Step 9: no approval is read on this path.
        # Step 10: refuse a part that the checks cannot read.
        hidden = unreviewable(run.tailored)
        if hidden:
            raise Refused(
                "tailored.json has parts that the checks cannot read: "
                + ", ".join(hidden)
                + ". Remove them, then finalize --handover again."
            )
        # Step 11: the contact and privacy rules on the fields that facts
        # check does not read.
        rules = load_rules(repo)
        found = handover_scan(run.tailored, rules)
        if found:
            places = "; ".join(f"{f.rule} at {f.location}" for f in found)
            raise Refused(
                f"the hand-over scan found private data: {places}. Remove it "
                "from tailored.json, then finalize --handover again."
            )
        # Step 12: facts check on the same content.
        findings = check(run, rules, load_master(repo), load_open_questions(repo))
        errors = [f for f in findings if f.severity == "error"]
        if errors:
            raise Refused(
                f"facts check has {len(errors)} errors. Run facts check {run_dir}, "
                "correct them, then finalize --handover again."
            )
        # Steps 13 and 14: build, check and commit, with signed false.
        passed = _Passed(
            run_dir=run_dir,
            run=run,
            meta=meta,
            digest=content_hash(run.tailored, run.letter),
            settings=settings_of(meta, run_dir),
        )
        renderer = renderer_for(repo, config.render)
        done = _build_and_commit(
            passed, renderer, count_pages, office_ready, today, {"signed": False}
        )
        if copy_dir is None:
            return done
        # Step 15: the copy, after the commit.
        target = copy_dir / run_dir.name
        try:
            _copy_files(run_dir, done.manifest["files"], target)
        except (OSError, InputError) as exc:
            raise CopyFailed(
                f"the run is finalized and its files are in {run_dir / OUT}/, but "
                f"the copy to {target} failed: {exc}. Give the owner the files in "
                f"{run_dir / OUT}/, or correct the folder and run facts finalize "
                f"{run_dir} --handover again."
            ) from exc
        return Finalized(done.manifest, done.warnings, target)


def _refuse_status(run_dir: Path, meta: dict[str, Any]) -> None:
    """Raise Refused unless the status of `meta` is new or finalized."""
    status: Any = meta.get("status")
    if status not in FINALIZABLE:
        raise Refused(
            f"{run_dir / RUN_YAML} has the status {status!r}. finalize works "
            "only on a run with the status new or finalized."
        )


def _refuse_signed_manifest(run_dir: Path) -> None:
    """Raise Refused unless the run has no manifest or a hand-over manifest.

    A manifest that says `"signed": true`, or that cannot be read as a JSON
    object with `"signed": false`, is refused. So a hand-over never replaces
    a signed result.
    """
    path = run_dir / MANIFEST
    if not exists(path):
        return
    try:
        data: Any = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise Refused(
            f"{path} cannot be read: {exc}. finalize --handover does not replace "
            "a manifest that it cannot read. Ask the owner what to do."
        ) from exc
    signed: Any = data.get("signed") if isinstance(data, dict) else None
    if signed is True:
        raise Refused(
            f"{path} is a signed manifest. A hand-over never replaces a signed "
            f"result. The signed files are in {run_dir / OUT}/."
        )
    if signed is not False:
        raise Refused(
            f"{path} is not a hand-over manifest (no signed: false). "
            "finalize --handover does not replace it. Ask the owner what to do."
        )


def handover_scan(tailored: dict[str, Any], rules: dict[str, Any]) -> list[Finding]:
    """Return the R-1 and PV-3 findings in fields that `facts check` does not read.

    The fields: `website` and `github` of each job and project, `location` of
    each project and education entry, and `displayName` of each section. The
    rule code is `privacy_findings()` of the public copy (the R-1 contact
    rules and PV-3), with no change. The PV-3 exemption `allowed_bullet_ids`
    is for bullets, so it does not apply to these fields. Raises InputError
    when rules.yaml lacks a key that the scan reads.
    """
    require_public_keys(rules)
    fields: dict[str, str] = {}
    for section, keys in SCAN_FIELDS:
        entries: Any = tailored.get(section) or []
        for i, entry in enumerate(entries):
            for key in keys:
                value: Any = entry.get(key) if isinstance(entry, dict) else None
                if value is not None and value != "":
                    fields[f"{section}[{i}].{key}"] = str(value)
    metas: Any = tailored.get("sectionMeta") or []
    for i, meta in enumerate(metas):
        name: Any = meta.get("displayName") if isinstance(meta, dict) else None
        if name is not None and name != "":
            fields[f"sectionMeta[{i}].displayName"] = str(name)
    privacy: dict[str, Any] = {**rules["privacy_patterns"], "allowed_bullet_ids": []}
    return privacy_findings(fields, {**rules, "privacy_patterns": privacy})


def copy_folder(repo: Path, settings: HandoverSettings) -> Path | None:
    """Return the tested `[handover] copy_to` folder, or None when not set.

    A relative path is relative to the data root. A leading `~/` means the
    home folder of the user in the system user database; the HOME
    environment variable is not read. The folder must exist (darzi does not
    make it), be a folder, be writable, and its real path must be outside the
    real path of the data root, and the data root must not be inside it. The
    folder itself may be a symbolic link.
    Raises InputError (exit code 2) for each failed test.
    """
    value = settings.copy_to
    if value is None:
        return None
    fix = "Correct [handover] copy_to in facts.toml, then finalize --handover again."
    if value == "~" or value.startswith("~/"):
        path = Path(pwd.getpwuid(os.getuid()).pw_dir) / value[2:]
    elif value.startswith("~"):
        raise InputError(
            f"[handover] copy_to {value!r}: only ~/ for your own home folder "
            f"is permitted. {fix}"
        )
    else:
        path = repo / value
    try:
        if not is_dir(path):
            raise InputError(
                f"the copy_to folder {path} does not exist or is not a folder. "
                f"darzi does not make it. Make the folder or correct copy_to."
            )
        if not os.access(path, os.W_OK | os.X_OK):
            raise InputError(f"the copy_to folder {path} is not writable. {fix}")
        real, root = path.resolve(strict=True), repo.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InputError(
            f"cannot read the copy_to folder {path}: {exc}. {fix}"
        ) from exc
    if real == root or root in real.parents:
        raise InputError(
            f"the copy_to folder {path} is inside the data root {root}. The "
            f"hand-over files must not go into the private data root. {fix}"
        )
    if real in root.parents:
        raise InputError(
            f"the data root {root} is inside the copy_to folder {path}. A sync "
            f"of that folder would copy the private data root. {fix}"
        )
    return path


def _refuse_copy_target(target: Path, names: list[str]) -> None:
    """Raise InputError when the run sub-folder or one of its files is a link.

    `target` is `<copy_to>/<run folder name>`. When it exists, it must be a
    real folder, and each name of `names` in it must not be a link or a
    folder.
    """
    if target.is_symlink():
        raise InputError(
            f"{target} is a symbolic link. darzi does not copy through a link. "
            "Remove the link, then finalize --handover again."
        )
    if not exists(target):
        return
    if not is_dir(target):
        raise InputError(
            f"{target} is not a folder. Remove it, then finalize --handover again."
        )
    for name in names:
        path = target / name
        if path.is_symlink() or is_dir(path):
            raise InputError(
                f"{path} is a symbolic link or a folder. Remove it, then "
                "finalize --handover again."
            )


def _copy_files(run_dir: Path, files: list[dict[str, Any]], target: Path) -> None:
    """Copy the output files that `files` lists into the folder `target`.

    Only the files of the manifest are copied: never the manifest, run.yaml,
    the job post, the answers or a fact file. Each file is read one time, its
    hash is tested against the manifest, and it is written with a temporary
    file and one rename. A file of the same name is replaced; other files in
    `target` are not touched. Raises OSError or InputError.
    """
    if not exists(target) and not target.is_symlink():
        target.mkdir()
    _refuse_copy_target(target, [Path(item["path"]).name for item in files])
    for item in files:
        data = (run_dir / item["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise InputError(f"{item['path']} does not have the manifest hash")
        write_atomic(target / Path(item["path"]).name, data)


def _file_stem(resume: dict[str, Any]) -> str:
    """Return the file name stem from the name in the resume (as the source)."""
    name = str((resume.get("personalInfo") or {}).get("name") or "Resume")
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "Resume"


def _remove(path: Path) -> None:
    """Remove a file, a link or a folder tree at `path`. No link is followed."""
    if path.is_symlink() or (exists(path) and not is_dir(path)):
        path.unlink()
    elif is_dir(path):
        shutil.rmtree(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# The approval fields of a manifest: exactly one of these two key sets.
SIGNED_FIELDS = frozenset(("signed", "approval_hash", "signer_fingerprint"))
HANDOVER_FIELDS = frozenset(("signed",))


def _check_signing(signing: dict[str, Any]) -> None:
    """Raise ValueError unless `signing` is the field set of exactly one gate.

    The signed gate gives `signed` True with the two signed keys as texts; the
    hand-over gate gives only `signed` False. Each other value is a bug.
    """
    keys = frozenset(signing)
    if keys == SIGNED_FIELDS and signing["signed"] is True:
        if all(isinstance(signing[k], str) and signing[k] for k in keys - {"signed"}):
            return
    elif keys == HANDOVER_FIELDS and signing["signed"] is False:
        return
    raise ValueError(f"not the approval fields of a gate: {sorted(keys)}")


def _build_and_commit(
    passed: _Passed,
    renderer: ResumeMatcher,
    count_pages: CountPages,
    office_ready: OfficeReady,
    today: dt.date,
    signing: dict[str, Any],
) -> Finalized:
    """Build, check and commit the files of content that a gate passed.

    Private: only a gate calls it. `signing` holds the approval fields of the
    manifest that the gate gives, with no default: `signed` True with
    `approval_hash` and `signer_fingerprint` (finalize), or only `signed`
    False (finalize_handover).
    """
    _check_signing(signing)
    run_dir, run = passed.run_dir, passed.run
    # Step 8: LibreOffice counts the Word pages. Refuse before the slow render.
    if not office_ready():
        raise Refused(
            "LibreOffice was not found, so the Word pages cannot be counted. "
            "Install LibreOffice, then finalize again."
        )
    # Step 9: remove the work folders of a crashed finalize; make a new one.
    work = run_dir / WORK_NEW
    try:
        _remove(work)
        _remove(run_dir / WORK_OLD)
        work.mkdir()
    except OSError as exc:
        raise Refused(
            f"cannot make the work folder {work}: {exc}. No file in out/ and no "
            "manifest changed. Correct the cause, then finalize again."
        ) from exc
    try:
        files, template, page_size = _build(passed, renderer, count_pages, work)
    except BaseException:
        _remove(work)
        raise
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run": run_target(run_dir),
        "date": today.isoformat(),
        **signing,
        "renderer": {
            "name": ADAPTER,
            "template": template,
            "page_size": page_size,
            "commit": renderer.commit(),
        },
        "print_settings": passed.settings,
        "files": files,
    }
    _commit(run_dir, work, files, manifest, passed.meta)
    pages = {item["kind"]: item["pages"] for item in files}
    warnings = _page_warnings(
        pages[RESUME_PDF], pages[RESUME_WORD], run.the_map.get("max_pages")
    )
    return Finalized(manifest, warnings)


def _commit(
    run_dir: Path,
    work: Path,
    files: list[dict[str, Any]],
    manifest: dict[str, Any],
    meta: dict[str, Any],
) -> None:
    """Put the built files, the manifest and the status into place (11 to 14).

    An OSError becomes Refused. Before the old manifest is removed, the work
    folder is removed and the old state stays. After that, nothing is undone:
    the run has no manifest, or a correct new manifest (the crash states of
    the plan), and a new finalize repairs it.
    """
    # Step 11: no manifest from now on until the new one is complete.
    try:
        (run_dir / MANIFEST).unlink(missing_ok=True)
    except OSError as exc:
        with suppress(OSError):
            _remove(work)
        raise Refused(
            f"cannot remove {run_dir / MANIFEST}: {exc}. No file in out/ and no "
            "manifest changed. Correct the cause, then finalize again."
        ) from exc
    try:
        # Step 12: swap the folders.
        out, old = run_dir / OUT, run_dir / WORK_OLD
        if exists(out):
            os.rename(out, old)
        os.rename(work, out)
        _remove(old)
        # The files in out/ must still have the hashes that the manifest gives.
        for item in files:
            if _sha256(run_dir / item["path"]) != item["sha256"]:
                raise Refused(
                    f"{run_dir / item['path']} changed during finalize. "
                    "No manifest was written. Finalize again."
                )
        # Step 13: the manifest.
        text = json.dumps(manifest, indent=2) + "\n"
        write_atomic(run_dir / MANIFEST, text.encode("utf-8"))
    except OSError as exc:
        raise Refused(
            f"finalize stopped after it removed the old manifest: {exc}. The run "
            "has no manifest now. Correct the cause, then run facts finalize "
            "again to repair the run."
        ) from exc
    # Step 14: the status.
    try:
        write_status(run_dir, meta, "finalized")
    except OSError as exc:
        raise Refused(
            f"the files in out/ and {MANIFEST} are correct, but the status in "
            f"{RUN_YAML} was not written: {exc}. Run facts finalize again to "
            "repair the run."
        ) from exc


def _build(
    passed: _Passed, renderer: ResumeMatcher, count_pages: CountPages, work: Path
) -> tuple[list[dict[str, Any]], str, str]:
    """Build the files in `work`, count the pages and run the round-trip check.

    Return the manifest entries, the template and the page size. Raises
    Refused, RoundTripFailed, RenderError or InputError.
    """
    run, settings = passed.run, passed.settings
    _check_word_settings(settings)
    stem = _file_stem(run.tailored)
    pdf = work / f"{stem}_Resume.pdf"
    word = work / f"{stem}_Resume.docx"
    letter = work / f"{stem}_Cover_Letter.docx"
    result = renderer.render(run.tailored, settings, pdf)
    try:
        resume_docx(run.tailored, word, settings)
    except ValueError as exc:
        found = _field_with_bad_char(run.tailored)
        if found is None:
            raise
        raise _not_in_word(found, "tailored.json") from exc
    built: list[tuple[str, Path, int]] = [(RESUME_PDF, pdf, result.pages)]
    built.append((RESUME_WORD, word, _pages(count_pages, word)))
    findings = check_resume(run.tailored, pdf) + check_resume(run.tailored, word)
    if run.letter is not None:
        try:
            letter_docx(run.letter, letter)
        except ValueError as exc:
            found = _field_with_bad_char(run.letter, "the cover letter")
            if found is None:
                raise
            raise _not_in_word(found, "cover_letter.md") from exc
        built.append((LETTER_WORD, letter, _pages(count_pages, letter)))
        findings += check_letter(run.letter, letter)
    if findings:
        raise RoundTripFailed(findings)
    # The builders must not change the content that the signature covers.
    if content_hash(run.tailored, run.letter) != passed.digest:
        raise Refused("the approved content changed in memory during the build.")
    files = [
        {
            "path": f"{OUT}/{path.name}",
            "kind": kind,
            "sha256": _sha256(path),
            "pages": pages,
            "reproducible": REPRODUCIBLE[kind],
        }
        for kind, path, pages in built
    ]
    return files, result.template, result.page_size


# The print settings that the Word résumé reads as whole numbers (see
# word_layout()), with the accepted values of each.
_LEVELS = ", ".join(str(level) for level in sorted(FONT_SIZE_PX))
WORD_WHOLE_NUMBERS = {
    **{key: f"one of {_LEVELS}" for key in DEFAULT_LEVELS},
    **{
        f"margin{side}": "a whole number of millimetres, such as 10"
        for side in ("Top", "Bottom", "Left", "Right")
    },
}


def _check_word_settings(settings: dict[str, Any]) -> None:
    """Raise InputError (exit code 2) for a print setting that is not a number.

    word_layout() calls int() on each key of WORD_WHOLE_NUMBERS. Without this
    check, a value such as '12.5' stops the build with a ValueError.
    """
    for key, accepted in WORD_WHOLE_NUMBERS.items():
        if key not in settings:
            continue
        try:
            int(settings[key])
        except (TypeError, ValueError):
            raise InputError(
                f"{RUN_YAML}: the print_settings value of {key} is not a whole "
                f"number. Write {accepted}, then finalize again."
            ) from None


def _xml_char(c: str) -> bool:
    """Return True when `c` is a character of XML 1.0, which a Word file holds."""
    code = ord(c)
    return (
        c in "\t\n\r"
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def _field_with_bad_char(value: Any, where: str = "") -> tuple[str, str] | None:
    """Return the first field of `value` with a character that is not XML 1.0.

    The result is the path of the field (for example `personalInfo.phone`) and
    the character, or None when no field has one.
    """
    if isinstance(value, str):
        bad = next((c for c in value if not _xml_char(c)), None)
        return None if bad is None else (where, bad)
    items: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        pairs: dict[Any, Any] = value
        items = [(f"{where}.{k}" if where else str(k), v) for k, v in pairs.items()]
    elif isinstance(value, list):
        values: list[Any] = value
        items = [(f"{where}[{i}]", v) for i, v in enumerate(values)]
    for path, item in items:
        found = _field_with_bad_char(item, path)
        if found is not None:
            return found
    return None


def _not_in_word(found: tuple[str, str], source: str) -> Refused:
    """Return the refusal for a value that the Word library cannot hold."""
    where, char = found
    return Refused(
        f"{where} has the character U+{ord(char):04X}, which a Word file cannot "
        f"hold. Remove it from {source}, then approve again."
    )


def _pages(count_pages: CountPages, path: Path) -> int:
    """Return the page count of the Word file `path`, or raise Refused."""
    try:
        pages = count_pages(path)
    except InputError as exc:
        raise Refused(f"cannot count the pages of {path.name}: {exc}") from exc
    if isinstance(pages, bool) or pages < 1:
        raise Refused(f"the page count of {path.name} is {pages!r}, not 1 or more.")
    return pages


def _page_warnings(pdf: int, word: int, max_pages: Any) -> list[str]:
    """Return an R-18 warning for each resume file longer than max_pages."""
    if isinstance(max_pages, bool) or not isinstance(max_pages, int):
        return []
    return [
        f"R-18: {label} has {pages} pages; max_pages of the selection is {max_pages}."
        for label, pages in (
            ("the PDF r\u00e9sum\u00e9", pdf),
            ("the Word r\u00e9sum\u00e9", word),
        )
        if pages > max_pages
    ]
