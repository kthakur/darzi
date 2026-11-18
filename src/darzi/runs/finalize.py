"""`facts finalize RUN`: the files for an employer, from signed content only.

The command is in two parts (docs/SPEC.md sections 7, 8 and 9):

1. `finalize()` is the gate. It reads the run inputs one time into memory and
   decides "approved" with `run_is_approved()` on that content. Then it runs
   `unreviewable()` and `facts check` on the same content.
2. `_build_and_commit()` is private. It takes the content that the gate
   passed and the fingerprint of the key that signed it. It builds the files
   in a work folder, counts their pages, runs the round-trip check on each
   file, and only then moves the files to `out/` and writes `manifest.json`
   and the status `finalized`.

The gate has no parameter, flag, environment variable or test mode that skips
the approval. W-8 writes `"signed": true` only. The hand-over path of W-13
(`--handover`, `"signed": false`) adds its own gate; W-8 has no such path.

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
system temporary folder (see `lock_path`), not in the run folder. So it is
never an output, and a refusal leaves the run folder as it was.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from darzi.config import RenderSettings, load_config
from darzi.facts.check import MANIFEST, RunFiles, check, load_run
from darzi.facts.check_meta import load_open_questions
from darzi.facts.repo import InputError, load_master, load_rules
from darzi.facts.validate import Finding
from darzi.paths import exists, is_dir, is_file
from darzi.render.resume_matcher import ADAPTER, ResumeMatcher
from darzi.render.word import letter_docx, resume_docx
from darzi.runs.approve import run_is_approved, unreviewable
from darzi.runs.roundtrip import check_letter, check_resume
from darzi.runs.runs import (
    APPLICATIONS,
    RUN_YAML,
    content_hash,
    load_meta,
    settings_of,
    write_atomic,
    write_status,
)
from darzi.runs.signing import run_target

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
LOCK_PREFIX = "darzi-finalize-"
LOCK_SUFFIX = ".lock"

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


@dataclass(frozen=True)
class Finalized:
    """The result of a finalize: the manifest that was written, and warnings."""

    manifest: dict[str, Any]
    warnings: list[str]


@dataclass(frozen=True)
class _Passed:
    """The content that the gate passed. Only `finalize()` makes one."""

    run_dir: Path
    run: RunFiles
    meta: dict[str, Any]
    digest: str
    fingerprint: str
    settings: dict[str, Any]


def lock_path(run_dir: Path) -> Path:
    """Return the lock file of `run_dir`, in the system temporary folder.

    The name holds the SHA-256 hash of the resolved run folder path, so the
    same run folder always gives the same lock file.
    """
    key = hashlib.sha256(os.fsencode(run_dir.resolve())).hexdigest()[:32]
    return Path(tempfile.gettempdir()) / f"{LOCK_PREFIX}{key}{LOCK_SUFFIX}"


@contextmanager
def _locked(run_dir: Path) -> Generator[None]:
    """Hold the lock of `run_dir`. The system releases it when the process stops."""
    try:
        # O_NOFOLLOW: a symbolic link in the temporary folder is refused.
        fd = os.open(
            lock_path(run_dir), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
    except (OSError, RuntimeError) as exc:
        raise Refused(
            f"cannot open the lock file of {run_dir}: {exc}. "
            "Check the temporary folder."
        ) from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Refused(
                f"another finalize of {run_dir} runs now. Wait for it to end, "
                "then try again."
            ) from exc
        yield
    finally:
        os.close(fd)  # This also releases the lock.


def _refuse_other_root(repo: Path, run_dir: Path) -> None:
    """Raise InputError unless `run_dir` is `applications/<name>` of `repo`.

    The real path of the run folder (each symbolic link followed) must be a
    direct child of the `applications` folder in the real path of the data
    root whose facts.toml and allowed_signers finalize uses. A run folder that
    is a symbolic link is refused too.
    """
    try:
        real = run_dir.resolve(strict=True)
        applications = repo.resolve(strict=True) / APPLICATIONS
        link = run_dir.is_symlink()
    except (OSError, RuntimeError) as exc:
        raise InputError(f"cannot read the path of {run_dir}: {exc}.") from exc
    if link or real.parent != applications:
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
    with _locked(run_dir):
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
            fingerprint=verdict.fingerprint,
            settings=settings_of(meta, run_dir),
        )
        renderer = renderer_for(repo, config.render)
        return _build_and_commit(passed, renderer, count_pages, office_ready, today)


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


def _build_and_commit(
    passed: _Passed,
    renderer: ResumeMatcher,
    count_pages: CountPages,
    office_ready: OfficeReady,
    today: dt.date,
) -> Finalized:
    """Build, check and commit the files of content that the gate passed.

    Private: only a gate calls it. W-13 adds its own gate for the hand-over
    path; W-8 has none.
    """
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
        "signed": True,
        "approval_hash": passed.digest,
        "signer_fingerprint": passed.fingerprint,
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
    stem = _file_stem(run.tailored)
    pdf = work / f"{stem}_Resume.pdf"
    word = work / f"{stem}_Resume.docx"
    letter = work / f"{stem}_Cover_Letter.docx"
    result = renderer.render(run.tailored, settings, pdf)
    resume_docx(run.tailored, word, settings)
    built: list[tuple[str, Path, int]] = [(RESUME_PDF, pdf, result.pages)]
    built.append((RESUME_WORD, word, _pages(count_pages, word)))
    findings = check_resume(run.tailored, pdf) + check_resume(run.tailored, word)
    if run.letter is not None:
        letter_docx(run.letter, letter)
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
