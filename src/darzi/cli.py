"""Command-line entry point for `facts`.

Each command comes with its work item of docs/SPEC.md section 11. Exit codes:
0 = no errors, 1 = errors found, 2 = invalid input (docs/SPEC.md section 6).
"""

import argparse
import datetime as dt
import json
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from darzi import __version__
from darzi.config import Approval, load_config
from darzi.facts.check import (
    BASE,
    BASE_MAP,
    CHECK_REPORT,
    check,
    load_run,
    require_public_keys,
    require_selection_keys,
)
from darzi.facts.check_meta import load_open_questions
from darzi.facts.export import export
from darzi.facts.public import privacy_findings, public_copy, query
from darzi.facts.repo import (
    InputError,
    find_repo,
    load_master,
    load_rules,
    load_selections,
)
from darzi.facts.rules import validate_rules
from darzi.facts.selection import (
    check_selection,
    is_approved,
    load_selection,
    selection_hash,
)
from darzi.facts.validate import Finding, validate
from darzi.paths import exists, is_file
from darzi.render import resume_matcher
from darzi.render.pages import find_soffice, word_pages
from darzi.render.port import Renderer, RenderError
from darzi.runs.approve import (
    Ask,
    confirm,
    one_line,
    record_and_sign,
    require_terminal,
    run_text,
    safe,
    selection_text,
    sign_problem,
    unreviewable,
)
from darzi.runs.finalize import Refused, RoundTripFailed, finalize
from darzi.runs.form import (
    ANSWERS_NOTE,
    FormRefused,
    answers_report,
    fill_plan,
    mark_submitted,
)
from darzi.runs.runs import (
    APPLICATIONS,
    APPROVAL,
    content_hash,
    new_run,
    print_settings,
    run_name,
)
from darzi.runs.signing import (
    run_target,
    selection_record,
    selection_target,
    target_problem,
)
from darzi.runs.status import RunRow, list_runs
from darzi.schemas import ANSWERS, MASTER, RULES, SELECTION, load_schema
from darzi.sourcing.fetch import fetch
from darzi.sourcing.model import FetchFailure

JSON_SCHEMA_VERSION = 1

# Command -> the work item that builds it (docs/SPEC.md section 11).
PLANNED: dict[str, str] = {
    "doctor": "W-11",
    "workflow": "W-11",
    "fit": "W-12",
}


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for `facts`."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repo", type=Path, help="data root (default: the nearest with master.yaml)"
    )
    common.add_argument("--json", action="store_true", help="print JSON")

    parser = argparse.ArgumentParser(
        prog="facts", description="Check and export career facts."
    )
    parser.add_argument("--version", action="version", version=f"facts {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser(
        "validate", parents=[common], help="check master.yaml and rules.yaml"
    )
    sub.add_parser("public", parents=[common], help="print the public copy as JSON")
    check_cmd = sub.add_parser(
        "check", parents=[common], help="check a run folder or a selection"
    )
    check_cmd.add_argument(
        "run", type=Path, help="run folder with tailored.json, or a selection file"
    )
    query_cmd = sub.add_parser(
        "query", parents=[common], help="find public bullets by tags and text"
    )
    query_cmd.add_argument(
        "--tags", default="", help="comma-separated; a bullet must have all of them"
    )
    query_cmd.add_argument("--text", help="text the bullet must contain (any case)")
    export_cmd = sub.add_parser(
        "export", parents=[common], help="selection -> base.json + base.map.json"
    )
    export_cmd.add_argument("selection", type=Path, help="selections/<type>.yaml")
    export_cmd.add_argument("--run", type=Path, required=True, help="run folder")
    new_cmd = sub.add_parser(
        "new-run", parents=[common], help="make a run folder and save job.md first"
    )
    new_cmd.add_argument("--company", required=True)
    new_cmd.add_argument("--role", required=True)
    source = new_cmd.add_mutually_exclusive_group(required=True)
    source.add_argument("--job", type=Path, help="job post file, or - for stdin")
    source.add_argument("--url", help="job post link: fetch one page")
    sub.add_parser("status", parents=[common], help="list all runs")
    approve_cmd = sub.add_parser(
        "approve",
        parents=[common],
        help="the owner approves and signs a selection or a run",
    )
    approve_cmd.add_argument(
        "target", type=Path, help="selections/<type>.yaml, or a run folder"
    )
    render_cmd = sub.add_parser(
        "render",
        parents=[common],
        help="render tailored.json to a draft PDF outside the run folder",
    )
    render_cmd.add_argument("run", type=Path, help="run folder with tailored.json")
    render_cmd.add_argument(
        "--draft",
        action="store_true",
        help="render to a new temporary folder; write nothing in the run folder",
    )
    finalize_cmd = sub.add_parser(
        "finalize",
        parents=[common],
        help="build the files of an approved run in out/ and write manifest.json",
    )
    finalize_cmd.add_argument("run", type=Path, help="run folder with approval.json")
    sub.add_parser("answers", parents=[common], help="check answers.yaml")
    plan_cmd = sub.add_parser(
        "fill-plan",
        parents=[common],
        help="print the only data that a form fill can use (each check must pass)",
    )
    plan_cmd.add_argument("run", type=Path, help="a finalized run folder")
    submitted_cmd = sub.add_parser(
        "submitted",
        parents=[common],
        help="record that the owner submitted the application",
    )
    submitted_cmd.add_argument("run", type=Path, help="a finalized run folder")
    for name, item in PLANNED.items():
        sub.add_parser(name, parents=[common], help=f"not built yet ({item})")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run `facts` and return the exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    command: str | None = args.command
    if command is None:
        parser.print_help()
        return 0
    if command == "validate":
        return _validate(args.repo, args.json)
    if command == "check":
        return _check(args.run, args.repo, args.json)
    if command == "export":
        return _export(args.selection, args.run, args.repo, args.json)
    if command == "public":
        return _public(args.repo)
    if command == "query":
        return _query(args.repo, args.tags, args.text, args.json)
    if command == "new-run":
        if args.url is not None:
            return _new_run_url(args.company, args.role, args.url, args.repo, args.json)
        return _new_run(args.company, args.role, args.job, args.repo, args.json)
    if command == "status":
        return _status(args.repo, args.json)
    if command == "approve":
        return _approve(args.target, args.repo)
    if command == "render":
        return _render(args.run, args.repo, args.draft, args.json)
    if command == "finalize":
        return _finalize(args.run, args.repo, args.json)
    if command == "answers":
        return _answers(args.repo, args.json)
    if command == "fill-plan":
        return _fill_plan(args.run, args.repo, args.json)
    if command == "submitted":
        return _submitted(args.run, args.repo, args.json)
    print(
        f"facts {command}: not built yet. It comes in work item {PLANNED[command]}.",
        file=sys.stderr,
    )
    return 2


def _validate(repo_arg: Path | None, as_json: bool) -> int:
    try:
        repo = find_repo(repo_arg)
        findings = validate(
            load_master(repo), load_schema(MASTER), load_selections(repo)
        )
        findings += validate_rules(load_rules(repo), load_schema(RULES))
    except InputError as exc:
        print(f"facts validate: {exc}", file=sys.stderr)
        return 2
    _report("validate", repo, findings, as_json)
    return 1 if any(f.severity == "error" for f in findings) else 0


def _check(run: Path, repo_arg: Path | None, as_json: bool) -> int:
    if is_file(run):
        return _check_selection(run, repo_arg, as_json)
    # Invalid input gives exit code 2 and writes no check.json. This includes a
    # rules.yaml or base.map.json that lacks a key that the rules read.
    try:
        repo = find_repo(repo_arg)
        findings = check(
            load_run(run),
            load_rules(repo),
            load_master(repo),
            load_open_questions(repo),
        )
    except InputError as exc:
        print(f"facts check: {exc}", file=sys.stderr)
        return 2
    report = _report_data("check", run, findings)
    (run / CHECK_REPORT).write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    _print(report, findings, as_json, str(run))
    return 1 if report["errors"] else 0


def _check_selection(path: Path, repo_arg: Path | None, as_json: bool) -> int:
    try:
        repo = find_repo(repo_arg)
        selection = load_selection(path)
        master = load_master(repo)
        rules = load_rules(repo)
        require_selection_keys(rules)
        questions = load_open_questions(repo)
    except InputError as exc:
        print(f"facts check: {exc}", file=sys.stderr)
        return 2
    findings = check_selection(
        selection, load_schema(SELECTION), master, rules, questions
    )
    report = _report_data("check", path, findings)
    _print(report, findings, as_json, str(path))
    return 1 if report["errors"] else 0


def _export(path: Path, run: Path, repo_arg: Path | None, as_json: bool) -> int:
    # Invalid input (a file is missing or cannot be read, or rules.yaml lacks a
    # key that the export reads) gives exit code 2.
    try:
        repo = find_repo(repo_arg)
        selection = load_selection(path)
        master = load_master(repo)
        rules = load_rules(repo)
        require_public_keys(rules)
        questions = load_open_questions(repo)
        settings = load_config(repo).approval
    except InputError as exc:
        print(f"facts export: {exc}", file=sys.stderr)
        return 2
    findings = check_selection(
        selection, load_schema(SELECTION), master, rules, questions
    )
    if findings:
        _print(_report_data("export", path, findings), findings, as_json, str(path))
        return 1
    # Only a signed approval of this content permits the export. The old
    # approval block in the YAML has no effect (docs/SPEC.md section 7).
    verdict = is_approved(repo, settings, path, selection)
    if not verdict.approved:
        print(
            _line(
                f"facts export: {path} is not approved: {verdict.reason}. "
                f"The owner runs: facts approve {path}"
            ),
            file=sys.stderr,
        )
        return 1
    try:
        rel = str(path.resolve().relative_to(repo))
    except ValueError:
        rel = str(path)
    # A selection that the public copy cannot build, or private data in the
    # export, is an error found: exit code 1.
    try:
        base, base_map = export(public_copy(master, rules), selection, rel, rules)
    except InputError as exc:
        print(f"facts export: {exc}", file=sys.stderr)
        return 1
    run.mkdir(parents=True, exist_ok=True)
    for name, data in ((BASE, base), (BASE_MAP, base_map)):
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        (run / name).write_text(text, encoding="utf-8")
    entries = len(base_map["entries"])
    print(f"facts export: wrote {run / BASE} and {run / BASE_MAP} ({entries} entries).")
    return 0


def make_renderer(repo: Path) -> Renderer:
    """Return the renderer that `[render]` in facts.toml of `repo` names.

    Raises InputError for an absent or bad setting.
    """
    return resume_matcher.from_settings(repo, load_config(repo).render)


def _render(run: Path, repo_arg: Path | None, draft: bool, as_json: bool) -> int:
    # A draft needs no approval. It writes the PDF to a new temporary folder
    # outside the run folder, and nothing in the run folder (docs/SPEC.md
    # section 6). The final PDF comes with facts finalize (W-8).
    if not draft:
        print(
            "facts render: give --draft. The final PDF comes with facts finalize.",
            file=sys.stderr,
        )
        return 2
    try:
        repo = find_repo(repo_arg)
        resume = load_run(run).tailored
        settings = print_settings(run)
        renderer = make_renderer(repo)
    except InputError as exc:
        print(_line(f"facts render: {exc}"), file=sys.stderr)
        return 2
    folder = Path(tempfile.mkdtemp(prefix="darzi-draft-"))
    pdf = folder / "resume.pdf"
    done = False
    try:
        result = renderer.render(resume, settings, pdf)
        done = True
    except InputError as exc:
        print(_line(f"facts render: {exc}"), file=sys.stderr)
        return 1 if isinstance(exc, RenderError) else 2
    finally:
        # Each other exception rises after this clean-up.
        if not done:
            shutil.rmtree(folder, ignore_errors=True)
    if as_json:
        data = {
            "schema_version": JSON_SCHEMA_VERSION,
            "pdf": str(pdf),
            **asdict(result),
        }
        print(json.dumps(data, indent=2))
    else:
        print(
            _line(
                f"facts render: draft PDF {pdf}: {result.pages} pages "
                f"({result.template}, {result.page_size}). "
                "The run folder did not change."
            )
        )
    return 0


def office_present() -> bool:
    """Return True when LibreOffice is at one of the paths of darzi.render.pages."""
    return find_soffice() is not None


def _finalize(run: Path, repo_arg: Path | None, as_json: bool) -> int:
    # A refusal, a failed render or a round-trip finding gives exit code 1.
    # Invalid input gives exit code 2. Each refusal leaves the run folder as
    # it was (docs/SPEC.md sections 7 and 8).
    try:
        repo = find_repo(repo_arg)
        done = finalize(
            repo,
            run,
            renderer_for=resume_matcher.from_settings,
            count_pages=word_pages,
            office_ready=office_present,
            today=_today(),
        )
    except RoundTripFailed as exc:
        findings = exc.findings
        _print(_report_data("finalize", run, findings), findings, as_json, str(run))
        print(_line(f"facts finalize: {exc}"), file=sys.stderr)
        return 1
    except InputError as exc:
        print(_line(f"facts finalize: {exc}"), file=sys.stderr)
        return 1 if isinstance(exc, Refused | RenderError) else 2
    manifest = done.manifest
    if as_json:
        data = {
            "schema_version": JSON_SCHEMA_VERSION,
            "command": "finalize",
            "target": str(run),
            "ok": True,
            "manifest": manifest,
            "warnings": done.warnings,
        }
        print(json.dumps(data, indent=2))
        return 0
    for item in manifest["files"]:
        print(
            _line(
                f"{item['path']}: {item['pages']} pages, sha256 {item['sha256'][:12]}"
            )
        )
    for warning in done.warnings:
        print(f"warning {warning}")
    print(
        _line(
            f"facts finalize: wrote {run / 'manifest.json'} (signed by "
            f"{manifest['signer_fingerprint']}). Status: finalized."
        )
    )
    return 0


def _answers(repo_arg: Path | None, as_json: bool) -> int:
    try:
        repo = find_repo(repo_arg)
        result = answers_report(repo, load_schema(ANSWERS), load_rules(repo))
    except InputError as exc:
        print(_line(f"facts answers: {exc}"), file=sys.stderr)
        return 2
    findings = result.findings
    report = _report_data("answers", repo, findings)
    if as_json:
        _write_json(report)
    else:
        # A value of answers.yaml can be in a finding: one line for each part.
        for f in findings:
            _write_line(f"{f.severity} {f.rule} {f.location}: {f.message}")
            if f.text:
                _write_line(f"    text: {f.text}")
        _write_line(
            f"answers.yaml: {report['errors']} errors, {report['warnings']} warnings."
        )
    return 1 if report["errors"] else 0


def _fill_plan(run: Path, repo_arg: Path | None, as_json: bool) -> int:
    # Each refusal prints one line on stderr and nothing on stdout. No flag
    # changes a check (docs/SPEC.md sections 6 and 7).
    try:
        repo = find_repo(repo_arg)
        plan = fill_plan(repo, run, load_schema(ANSWERS), load_rules(repo))
    except InputError as exc:
        print(_line(f"facts fill-plan: {exc}"), file=sys.stderr)
        return 1 if isinstance(exc, FormRefused) else 2
    if as_json:
        # The true values, as ASCII JSON: no value can break the document.
        _write_json(plan)
        return 0
    # The plan is a list of lines that is never joined and split again: each
    # value stays in its one line, also a value with a line boundary.
    for line in _plan_lines(plan):
        _write_line(line)
    return 0


def _plan_lines(plan: dict[str, Any]) -> list[str]:
    """Return the fill plan as text lines. Each value is in one line only.

    The cover letter is in the lines that the approved text has when it is
    split at LF only. `_write_line()` shows each other line boundary as "?".
    """
    lines = [f"Fill plan for {plan['run']}", "", "Files to upload:"]
    lines += [
        f"  {f['kind']}: {f['path']} (sha256 {f['sha256']})" for f in plan["files"]
    ]
    lines += ["", "Personal data (approved resume):"]
    info: dict[str, Any] = plan["personal_info"]
    lines += [f"  {key}: {one_line(value)}" for key, value in info.items()]
    lines += ["", f"Answers ({ANSWERS_NOTE}):"]
    lines += [
        f"  {a['id']}: {one_line(a['question'])} -> {one_line(a['answer'])}"
        for a in plan["answers"]
    ]
    lines += ["", "Empty answers (leave the field empty, tell the owner):"]
    # Each id and question is from answers.yaml: not covered by the signature.
    lines += [f"  {a['id']}: {one_line(a['question'])}" for a in plan["empty_answers"]]
    lines += ["", "Rules:"] + [f"  - {rule}" for rule in plan["rules"]]
    unsigned: dict[str, Any] = plan["not_signed"]
    lines += ["", "NOT covered by the signature:", f"  {unsigned['note']}"]
    lines += [f"  {key}: {one_line(unsigned[key])}" for key in ("company", "role")]
    lines += [f"  apply_url: {one_line(unsigned['apply_url'])}"]
    letter = plan["cover_letter"]
    lines += ["", "Cover letter (approved):"]
    lines += ["  (none)"] if letter is None else [f"  {x}" for x in letter.split("\n")]
    return lines


def _submitted(run: Path, repo_arg: Path | None, as_json: bool) -> int:
    try:
        repo = find_repo(repo_arg)
        result = mark_submitted(repo, run, _today())
    except InputError as exc:
        print(_line(f"facts submitted: {exc}"), file=sys.stderr)
        return 1 if isinstance(exc, FormRefused) else 2
    if as_json:
        data = {"schema_version": JSON_SCHEMA_VERSION, "command": "submitted", **result}
        _write_json(data, indent=None)
    else:
        _write_line(
            f"facts submitted: {run} has the status submitted ({result['submitted']})."
        )
    return 0


def _public_data(repo_arg: Path | None) -> tuple[dict[str, Any], list[Finding]]:
    """Return the public copy and the private data that the scan finds in it.

    Raises InputError when a file is missing or cannot be read, or when
    rules.yaml lacks a key that the public copy or the scan reads.
    """
    repo = find_repo(repo_arg)
    rules = load_rules(repo)
    require_public_keys(rules)
    data = public_copy(load_master(repo), rules)
    return data, privacy_findings(data, rules)


def _leak_message(leaks: list[Finding]) -> str:
    lines = "\n".join(f"  {f.rule} {f.location}: {f.message}" for f in leaks)
    return f"the public copy holds private data:\n{lines}"


def _public(repo_arg: Path | None) -> int:
    try:
        data, leaks = _public_data(repo_arg)
    except InputError as exc:
        print(f"facts public: {exc}", file=sys.stderr)
        return 2
    if leaks:
        print(f"facts public: {_leak_message(leaks)}", file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def _query(repo_arg: Path | None, tags: str, text: str | None, as_json: bool) -> int:
    try:
        data, leaks = _public_data(repo_arg)
    except InputError as exc:
        print(f"facts query: {exc}", file=sys.stderr)
        return 2
    if leaks:
        print(f"facts query: {_leak_message(leaks)}", file=sys.stderr)
        return 1
    hits = query(data, tags.split(","), text)
    if as_json:
        print(
            json.dumps(
                {
                    "schema_version": JSON_SCHEMA_VERSION,
                    "command": "query",
                    "bullets": hits,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    for hit in hits:
        mark = "" if hit["verified"] else "  (not verified)"
        tags = ",".join(hit["tags"])
        print(f"{hit['id']}  [{hit['entry']}] {tags}{mark}\n    {hit['text']}")
    print(f"{len(hits)} bullets.")
    return 0


def _ask(prompt: str) -> str:
    return input(prompt)


def _approve(target: Path, repo_arg: Path | None) -> int:
    # The terminal test comes first, before any file is read.
    try:
        require_terminal(sys.stdin.isatty())
    except InputError as exc:
        print(f"facts approve: {exc}", file=sys.stderr)
        return 1
    return approve_steps(target, repo_arg, _ask, _today())


def approve_steps(target: Path, repo_arg: Path | None, ask: Ask, day: dt.date) -> int:
    """Run the steps of `facts approve` after the terminal test; return the exit code.

    Run the checks, show the review text, ask with `ask`, and sign. The
    signature uses the real settings, the real `allowed_signers` and the key of
    `facts.toml`; this function has no other way to approve.
    """
    # Each refusal gives exit code 1, as in the source. A value of the wrong
    # type in facts.toml is invalid input: exit code 2.
    try:
        repo = find_repo(repo_arg)
    except InputError as exc:
        print(f"facts approve: {exc}", file=sys.stderr)
        return 1
    try:
        settings = load_config(repo).approval
    except InputError as exc:
        print(f"facts approve: {exc}", file=sys.stderr)
        return 2
    try:
        problem = sign_problem(repo, settings)
        if problem:
            raise InputError(f"cannot sign: {problem}. Fix facts.toml first.")
        kind = "selection" if is_file(target) else "run"
        # The target must be in this data root, before a file is read or written.
        problem = target_problem(repo, kind, target)
        if problem:
            raise InputError(f"{problem}. Pass a target of this data root or --repo.")
        if kind == "selection":
            return _approve_selection(target, repo, settings, ask, day)
        return _approve_run(target, repo, settings, ask, day)
    except InputError as exc:
        print(f"facts approve: {_line(str(exc))}", file=sys.stderr)
        return 1


def _approve_selection(
    path: Path, repo: Path, settings: Approval, ask: Ask, day: dt.date
) -> int:
    selection = load_selection(path)
    master = load_master(repo)
    rules = load_rules(repo)
    require_selection_keys(rules)
    findings = check_selection(
        selection, load_schema(SELECTION), master, rules, load_open_questions(repo)
    )
    if any(f.severity == "error" for f in findings):
        _print(_report_data("approve", path, findings), findings, False, str(path))
        raise InputError("the selection has errors. Correct them, then approve.")
    if is_approved(repo, settings, path, selection).approved:
        print(f"{_line(str(path))}: this content has a valid approval already.")
        return 0
    print(selection_text(selection, master))
    if not confirm(ask, _line(str(path))):
        print("Not approved. No file was written.")
        return 1
    digest = selection_hash(selection)
    verdict = record_and_sign(
        repo,
        settings,
        "selection",
        selection_target(path),
        selection_record(path),
        digest,
        day,
        target_path=path,
    )
    _approved(path, day, digest, verdict.fingerprint)
    return 0


def _approve_run(
    run_dir: Path, repo: Path, settings: Approval, ask: Ask, day: dt.date
) -> int:
    run = load_run(run_dir)
    findings = check(
        run, load_rules(repo), load_master(repo), load_open_questions(repo)
    )
    report = _report_data("check", run_dir, findings)
    (run_dir / CHECK_REPORT).write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    if report["errors"]:
        _print(report, findings, False, str(run_dir))
        raise InputError("facts check found errors. Correct them, then approve.")
    hidden = unreviewable(run.tailored)
    if hidden:
        raise InputError(
            "the review cannot show these parts of tailored.json: "
            + ", ".join(hidden)
            + ". Remove them, then approve."
        )
    print(run_text(run))
    print(f"\nfacts check: 0 errors, {report['warnings']} warnings.")
    if not confirm(ask, f"the resume and the cover letter in {_line(str(run_dir))}"):
        print("Not approved. No file was written.")
        return 1
    # The hash comes from the same loaded data that the review showed.
    digest = content_hash(run.tailored, run.letter)
    verdict = record_and_sign(
        repo,
        settings,
        "run",
        run_target(run_dir),
        run_dir / APPROVAL,
        digest,
        day,
        target_path=run_dir,
    )
    _approved(run_dir, day, digest, verdict.fingerprint)
    return 0


def _approved(target: Path, day: dt.date, digest: str, key: str | None) -> None:
    print(
        f"Approved {_line(str(target))} on {day.isoformat()} "
        f"(sha256 {digest[:12]}, key {key})."
    )


def _today() -> dt.date:
    """Return the date of a new run: today."""
    return dt.date.today()


def _new_run_url(
    company: str, role: str, url: str, repo_arg: Path | None, as_json: bool
) -> int:
    # Test the data root, the names and the run folder before the fetch: bad
    # input makes no network call, and a failed fetch makes no folder.
    try:
        repo = find_repo(repo_arg)
        day = _today()
        target = repo / APPLICATIONS / run_name(company, role, day)
        if exists(target):
            raise InputError(
                f"{target} already exists. Use it, or choose another role name."
            )
    except InputError as exc:
        print(f"facts new-run: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"facts new-run: cannot read the run folder: {exc}", file=sys.stderr)
        return 1
    result = fetch(url)
    if isinstance(result, FetchFailure):
        # A refusal of the input is exit code 2. Each other failure is 1.
        print(
            f"facts new-run: cannot fetch the job post ({result.reason}). "
            f"{result.detail} Save the job post to a file and pass --job FILE, "
            "or paste it with --job -.",
            file=sys.stderr,
        )
        return 2 if result.refused else 1
    try:
        run = new_run(repo, company, role, result.text, day, apply_url=url)
    except InputError as exc:
        print(f"facts new-run: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"facts new-run: cannot write the run folder: {exc}", file=sys.stderr)
        return 1
    if as_json:
        data = {
            "schema_version": JSON_SCHEMA_VERSION,
            "command": "new-run",
            "run": str(run),
            "url": url,
        }
        print(json.dumps(data, indent=2))
        return 0
    print(run)
    return 0


def _read_job(job: Path) -> str:
    """Return the job post from the file `job`, or from stdin when `job` is "-".

    Raises InputError when the file is missing or cannot be read, and when the
    text is not UTF-8.
    """
    if str(job) == "-":
        # Decode the bytes as UTF-8, independent of the locale. The line ends
        # stay as they are, as in the source on macOS and Linux.
        try:
            return sys.stdin.buffer.read().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InputError(
                "The text from stdin is not UTF-8 text. "
                "Paste the job post as plain text, or save it as a UTF-8 file "
                "and pass --job FILE."
            ) from exc
    try:
        return job.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise InputError(
            f"{job} does not exist. Pass the job post file with --job FILE, "
            "or paste the job post with --job -."
        ) from exc
    except UnicodeDecodeError as exc:
        raise InputError(
            f"{job} is not UTF-8 text. Save the job post as plain text (UTF-8), "
            "or paste it with --job -."
        ) from exc
    except OSError as exc:
        raise InputError(
            f"{job} cannot be read: {exc.strerror}. Check the path, "
            "or paste the job post with --job -."
        ) from exc


def _new_run(
    company: str, role: str, job: Path, repo_arg: Path | None, as_json: bool
) -> int:
    # Invalid input gives exit code 2 (docs/SPEC.md section 6): no data root; a
    # job post that is missing, cannot be read, is not UTF-8 or is empty; a
    # company or role that gives an empty name; a run folder that exists.
    try:
        text = _read_job(job)
    except InputError as exc:
        print(f"facts new-run: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        # _read_job() turns each OSError of a job file into an InputError, so
        # this error comes from the read of stdin.
        print(
            f"facts new-run: cannot read the job post from stdin: "
            f"{exc.strerror or exc}. Save the job post to a file and pass --job FILE.",
            file=sys.stderr,
        )
        return 1
    try:
        run = new_run(find_repo(repo_arg), company, role, text, _today())
    except InputError as exc:
        print(f"facts new-run: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"facts new-run: cannot write the run folder: {exc}", file=sys.stderr)
        return 1
    if as_json:
        data = {
            "schema_version": JSON_SCHEMA_VERSION,
            "command": "new-run",
            "run": str(run),
        }
        print(json.dumps(data, indent=2))
        return 0
    print(run)
    return 0


def _status(repo_arg: Path | None, as_json: bool) -> int:
    try:
        repo = find_repo(repo_arg)
        rows, findings = list_runs(repo)
    except InputError as exc:
        print(f"facts status: {exc}", file=sys.stderr)
        return 2
    report = _report_data("status", repo, findings)
    if as_json:
        report["runs"] = [asdict(row) for row in rows]
        print(json.dumps(report, indent=2))
    else:
        _print_runs(rows, findings)
    return 1 if report["errors"] else 0


# Each line boundary of `str.splitlines()`, and the tab.
NOT_IN_A_LINE = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029\t")


def _encodable(text: str) -> str:
    """Return `text` with "?" for each character that stdout cannot encode.

    Python writes to stdout with errors="strict", so a character that the
    terminal encoding (for example ASCII) does not have would raise an error.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return text.encode(encoding, "replace").decode(encoding, "replace")
    except LookupError:  # an encoding name that Python does not know
        return text.encode("ascii", "replace").decode("ascii")


def _line(text: str) -> str:
    """Return `text` as one line of visible characters for the terminal.

    `safe()` keeps line ends and tabs. A line boundary (each character at
    which `str.splitlines()` starts a new line) or a tab would break the table
    or start a false line, so it shows as "?" too. A character that stdout
    cannot encode shows as "?".
    """
    shown = safe(text)
    return _encodable("".join("?" if c in NOT_IN_A_LINE else c for c in shown))


def _write_line(text: str) -> None:
    """Write `text` on stdout as exactly one line, with one write."""
    sys.stdout.write(_line(text) + "\n")


def _write_json(data: Any, indent: int | None = 2) -> None:
    """Write `data` on stdout as one ASCII JSON document with the true values."""
    sys.stdout.write(json.dumps(data, indent=indent, ensure_ascii=True) + "\n")


def _cell(value: str | None) -> str:
    """Return the table cell of `value`: "-" when absent, '""' when blank."""
    if value is None:
        return "-"
    if not value.strip():
        # An empty or blank value would leave the cell empty, and the table
        # would look as if a column is missing.
        return '""'
    return _line(value)


def _print_runs(rows: list[RunRow], findings: list[Finding]) -> None:
    if rows:
        table = [["DATE", "COMPANY", "ROLE", "TYPE", "STATUS"]]
        table += [
            [_cell(v) for v in (r.date, r.company, r.role, r.type, r.status)]
            for r in rows
        ]
        widths = [max(len(line[i]) for line in table) for i in range(5)]
        for line in table:
            cells = zip(line, widths, strict=True)
            print(safe("  ".join(c.ljust(w) for c, w in cells).rstrip()))
    for f in findings:
        # Exactly one line for each finding. A folder name can hold a line end.
        print(f"{f.severity} {f.rule} {_line(f.location)}: {_line(f.message)}")
    print(f"{len(rows)} run." if len(rows) == 1 else f"{len(rows)} runs.")


def _report_data(command: str, target: Path, findings: list[Finding]) -> dict[str, Any]:
    errors = sum(f.severity == "error" for f in findings)
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "command": command,
        "target": str(target),
        "ok": errors == 0,
        "errors": errors,
        "warnings": len(findings) - errors,
        "findings": [asdict(f) for f in findings],
    }


def _report(command: str, repo: Path, findings: list[Finding], as_json: bool) -> None:
    _print(
        _report_data(command, repo, findings),
        findings,
        as_json,
        "master.yaml and rules.yaml",
    )


def _print(
    report: dict[str, Any], findings: list[Finding], as_json: bool, label: str
) -> None:
    if as_json:
        print(json.dumps(report, indent=2))
        return
    for f in findings:
        line = f"{f.severity} {f.rule} {f.location}: {f.message}"
        print(safe(f"{line}\n    text: {f.text}" if f.text else line))
    print(f"{label}: {report['errors']} errors, {report['warnings']} warnings.")


if __name__ == "__main__":
    raise SystemExit(main())
