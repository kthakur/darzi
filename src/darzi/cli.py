"""Command-line entry point for `facts`.

Each command comes with its work item of docs/SPEC.md section 11. Exit codes:
0 = no errors, 1 = errors found, 2 = invalid input (docs/SPEC.md section 6).
"""

import argparse
import datetime as dt
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from darzi import __version__
from darzi.facts.check import BASE, BASE_MAP, CHECK_REPORT, check, load_run
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
from darzi.facts.selection import check_selection, load_selection
from darzi.facts.validate import Finding, validate
from darzi.runs.approve import safe
from darzi.runs.runs import new_run
from darzi.runs.status import RunRow, list_runs
from darzi.schemas import MASTER, RULES, SELECTION, load_schema

JSON_SCHEMA_VERSION = 1

# Command -> the work item that builds it (docs/SPEC.md section 11).
PLANNED: dict[str, str] = {
    "approve": "W-5",
    "render": "W-6",
    "finalize": "W-8",
    "answers": "W-9",
    "fill-plan": "W-9",
    "submitted": "W-9",
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
    source.add_argument("--url", help="job post link (not built yet: W-10)")
    sub.add_parser("status", parents=[common], help="list all runs")
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
            return _new_run_url()
        return _new_run(args.company, args.role, args.job, args.repo, args.json)
    if command == "status":
        return _status(args.repo, args.json)
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
    if run.is_file():
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
    # Invalid input (a file is missing or cannot be read) gives exit code 2.
    try:
        repo = find_repo(repo_arg)
        selection = load_selection(path)
        master = load_master(repo)
        rules = load_rules(repo)
        questions = load_open_questions(repo)
    except InputError as exc:
        print(f"facts export: {exc}", file=sys.stderr)
        return 2
    findings = check_selection(
        selection, load_schema(SELECTION), master, rules, questions
    )
    if findings:
        _print(_report_data("export", path, findings), findings, as_json, str(path))
        return 1
    # Work item W-5 adds the signed approval test here (docs/SPEC.md section 7).
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


def _public_data(repo_arg: Path | None) -> tuple[dict[str, Any], list[Finding]]:
    """Return the public copy and the private data that the scan finds in it.

    Raises InputError when a file is missing or cannot be read.
    """
    repo = find_repo(repo_arg)
    rules = load_rules(repo)
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


def _today() -> dt.date:
    """Return the date of a new run: today."""
    return dt.date.today()


def _new_run_url() -> int:
    # Work item W-10 builds the fetch. Until then --url makes no folder and no
    # network call.
    print(
        "facts new-run: --url is not built yet. It comes in work item W-10. "
        "Save the job post to a file and pass --job FILE, "
        "or paste it with --job -.",
        file=sys.stderr,
    )
    return 2


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


def _line(text: str) -> str:
    """Return `text` as one line of visible characters for the terminal.

    `safe()` keeps line ends and tabs. A line end or a tab would break the table
    or start a false line, so it shows as "?" too.
    """
    return safe(text).replace("\n", "?").replace("\t", "?")


def _cell(value: str | None) -> str:
    if value is None:
        return "-"
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
    print(safe(f"{len(rows)} runs."))


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
