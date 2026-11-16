"""Stop a push that holds a private term from the real data root.

`scripts/pre-push` runs this script with the arguments and the standard input of
a git pre-push hook. The script reads the data root from `DARZI_DATA_ROOT`,
builds a term list from it (docs/SPEC.md section 10) and scans each new commit
of the push: the commit message, the path of each added or changed file and the
full content of that file.

A finding gives the commit, the file and the line number. It never gives the
term, so the output is safe to show to an agent. `--show-terms` also shows the
term; it is for the owner only, and the hook does not use it.

Exit codes: 0 = no finding, 1 = a finding, or the scan could not run.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

ZERO_SHA = "0" * 40
TERMS_FILE = "privacy_scan_terms.txt"
ALLOW_FILE = "privacy_scan_allow.txt"
# Files in which the owner's name is permitted (AGENTS.md hard rule 1).
NAME_FILES = frozenset({"LICENSE", "pyproject.toml"})
# The project name is never a finding, in any letter case.
ALWAYS_ALLOWED = "darzi"


class ScanError(Exception):
    """The scan cannot run. The message says how to correct it."""


@dataclass(frozen=True)
class Pattern:
    """One thing to find: a term, a phone number or a regex."""

    regex: re.Pattern[str]
    label: str
    owner_name: bool = False


@dataclass(frozen=True)
class Finding:
    """One private match. `place` is a file path or "commit message"."""

    commit: str
    place: str
    line: int
    found: str
    label: str


def term_regex(term: str) -> re.Pattern[str]:
    """Return a case-insensitive regex for `term` on word boundaries.

    Spaces in the term match any run of white space, so a line break inside a
    phrase still matches. This is the rule of `patterns.term_regex()` in the
    source.
    """
    words = term.split()
    if not words:
        raise ValueError("A term must contain text.")
    body = r"\s+".join(re.escape(w) for w in words)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def phone_regex(phone: str) -> re.Pattern[str] | None:
    """Return a regex for the digits of `phone`, with any separators between them.

    Only the last ten digits must match, so a country code is optional.
    """
    digits = re.sub(r"\D", "", phone)[-10:]
    if len(digits) < 7:
        return None
    body = r"[\s().+-]*".join(digits)
    return re.compile(rf"(?<!\d){body}(?!\d)")


def _strings(values: Any) -> Iterator[str]:
    """Yield each non-empty string in a YAML value, at each depth."""
    if isinstance(values, str):
        if values.strip():
            yield values
    elif isinstance(values, dict):
        for value in values.values():
            yield from _strings(value)
    elif isinstance(values, list):
        for value in values:
            yield from _strings(value)


def _ids(node: Any) -> Iterator[str]:
    """Yield the value of each `id` key in a YAML value, at each depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "id" and isinstance(value, str) and value.strip():
                yield value
            else:
                yield from _ids(value)
    elif isinstance(node, list):
        for value in node:
            yield from _ids(value)


def _url_terms(url: str) -> Iterator[str]:
    """Yield the full URL and its host name without `www.`."""
    url = url.strip()
    if not url:
        return
    yield url
    parts = urlsplit(url if "://" in url else f"//{url}")
    host = (parts.hostname or "").removeprefix("www.")
    if host:
        yield host


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ScanError(
            f"{path.name} is not in DARZI_DATA_ROOT. Set it to the real data root."
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScanError(f"{path.name} is not valid YAML. Correct it: {exc}") from exc
    if not isinstance(data, dict):
        raise ScanError(f"{path.name} is not a YAML mapping. Correct it.")
    return data


def _lines(path: Path) -> list[str]:
    """Return the non-empty lines of an owner file, or no lines if it is absent."""
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip()]


def _block(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def build_patterns(root: Path) -> list[Pattern]:
    """Return each pattern from the data root at `root` (docs/SPEC.md section 10)."""
    rules = _load_yaml(root / "rules.yaml")
    master = _load_yaml(root / "master.yaml")
    terms: set[str] = set()
    phones: set[str] = set()
    names: set[str] = set()
    regexes: list[str] = []

    # rules.yaml: contact data, private patterns, private terms, organizations.
    public = _block(rules, "public_contact")
    for key in ("email", "phone"):
        if isinstance(public.get(key), str):
            (phones if key == "phone" else terms).add(public[key])
    terms.update(_strings(_block(rules, "private_contact_patterns").get("terms")))
    privacy = _block(rules, "privacy_patterns")
    terms.update(_strings(privacy.get("terms")))
    privacy_regex = privacy.get("regex")
    regexes.extend(_strings(privacy_regex))
    employer = _block(rules, "employer_of_record")
    terms.update(_strings(employer.get("required_any")))
    terms.update(_strings(employer.get("terms")))
    terms.update(_strings(_block(rules, "never_as_company").get("terms")))

    # master.yaml: each id, organizations, institutions, the profile.
    terms.update(_ids(master))
    for entry in _list(master.get("roles")) + _list(master.get("projects")):
        if isinstance(entry, dict):
            terms.update(_strings([entry.get("org"), entry.get("display_org")]))
            terms.update(_strings(entry.get("alt_names")))
    for entry in _list(master.get("education")):
        if isinstance(entry, dict):
            terms.update(
                _strings([entry.get("institution"), entry.get("display_institution")])
            )
    profile = _block(master, "profile")
    for link in _list(profile.get("links")):
        if isinstance(link, dict) and isinstance(link.get("url"), str):
            terms.update(_url_terms(link["url"]))
    if isinstance(profile.get("email"), str):
        terms.add(profile["email"])
    if isinstance(profile.get("phone"), str):
        phones.add(profile["phone"])
    if isinstance(profile.get("name"), str) and profile["name"].strip():
        names.add(profile["name"])
        names.update(profile["name"].split())

    # The owner's term file.
    terms.update(_lines(root / TERMS_FILE))

    patterns = [Pattern(term_regex(n), n, owner_name=True) for n in sorted(names)]
    patterns += [Pattern(term_regex(t), t) for t in sorted(terms - names)]
    for phone in sorted(phones):
        regex = phone_regex(phone)
        if regex is not None:
            patterns.append(Pattern(regex, phone))
    for source in regexes:
        try:
            patterns.append(Pattern(re.compile(source), source))
        except re.error as exc:
            raise ScanError(
                f"rules.yaml privacy_patterns.regex has an invalid regex: {exc}. "
                "Correct it."
            ) from exc
    return patterns


def scan_text(
    text: str,
    patterns: Iterable[Pattern],
    allow: frozenset[str],
    path: str | None,
) -> list[tuple[int, str, str]]:
    """Return (line number, found text, label) for each private match in `text`.

    A match passes when its found text is an exact entry of `allow`, when it is
    the word "darzi", or when it is the owner's name in a file of `NAME_FILES`.
    """
    patterns = list(patterns)
    names: set[str] = set()
    if path in NAME_FILES:
        names = {" ".join(p.label.lower().split()) for p in patterns if p.owner_name}
    found: list[tuple[int, str, str]] = []
    for pattern in patterns:
        for m in pattern.regex.finditer(text):
            hit = m.group(0)
            if hit in allow or hit.lower() == ALWAYS_ALLOWED:
                continue
            if " ".join(hit.lower().split()) in names:
                continue
            line = text.count("\n", 0, m.start()) + 1
            found.append((line, hit, pattern.label))
    return sorted(found)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise ScanError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def _git_bytes(*args: str) -> bytes:
    result = subprocess.run(["git", *args], capture_output=True)
    if result.returncode != 0:
        raise ScanError(f"git {args[0]} failed: {result.stderr.decode().strip()}")
    return result.stdout


def new_commits(remote: str, push_lines: Iterable[str]) -> list[str]:
    """Return each commit of the push that is not on `remote`, oldest first."""
    commits: list[str] = []
    for raw in push_lines:
        parts = raw.split()
        if not parts:
            continue
        if len(parts) != 4:
            raise ScanError(f"Unexpected pre-push input line: {raw.strip()!r}.")
        _, local_sha, _, remote_sha = parts
        if local_sha == ZERO_SHA:
            continue  # A delete sends no content.
        exclude = [f"--remotes={remote}"]
        if remote_sha != ZERO_SHA and _known(remote_sha):
            exclude.append(remote_sha)
        out = _git("rev-list", "--reverse", local_sha, "--not", *exclude)
        for sha in out.split():
            if sha not in commits:
                commits.append(sha)
    return commits


def _known(sha: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True
    )
    return result.returncode == 0


def changed_files(commit: str) -> list[str]:
    """Return each path that `commit` adds or changes (for a merge: on any side)."""
    out = _git_bytes(
        "diff-tree",
        "-r",
        "-m",
        "--root",
        "--no-commit-id",
        "--name-only",
        "--diff-filter=ACMRT",
        "-z",
        commit,
    )
    paths = [p.decode("utf-8", errors="replace") for p in out.split(b"\0") if p]
    return list(dict.fromkeys(paths))


def scan_commit(
    commit: str, patterns: list[Pattern], allow: frozenset[str]
) -> list[Finding]:
    """Return each finding in the message, the paths and the files of `commit`."""
    short = commit[:12]
    findings: list[Finding] = []
    message = _git("log", "-1", "--format=%B", commit)
    for line, hit, label in scan_text(message, patterns, allow, None):
        findings.append(Finding(short, "commit message", line, hit, label))
    for path in changed_files(commit):
        for _, hit, label in scan_text(path, patterns, allow, path):
            findings.append(Finding(short, f"{path} (file name)", 0, hit, label))
        content = _git_bytes("cat-file", "blob", f"{commit}:{path}")
        text = content.decode("utf-8", errors="replace")
        for line, hit, label in scan_text(text, patterns, allow, path):
            findings.append(Finding(short, path, line, hit, label))
    return findings


def report(findings: list[Finding], show_terms: bool) -> str:
    """Return the report text. The term is in it only when `show_terms` is set."""
    lines: list[str] = []
    seen: set[tuple[str, str, int]] = set()
    for f in findings:
        key = (f.commit, f.place, f.line)
        if key in seen and not show_terms:
            continue
        seen.add(key)
        where = f"commit {f.commit}, {f.place}"
        if f.line:
            where += f", line {f.line}"
        text = f"privacy scan: {where}: private data."
        if show_terms:
            text += f" Found {f.found!r} (term {f.label!r})."
        lines.append(text)
    lines.append(
        f"privacy scan: push stopped ({len(lines)} findings). Remove the private "
        "data from each commit, or ask the owner to add an allow-list entry."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None, stdin: Iterable[str] | None = None) -> int:
    """Run the scan as the pre-push hook does. Return the exit code."""
    parser = argparse.ArgumentParser(
        description="Stop a push that holds a private term from the data root."
    )
    parser.add_argument("remote", help="remote name (from git)")
    parser.add_argument("url", nargs="?", default="", help="remote URL (from git)")
    parser.add_argument(
        "--show-terms",
        action="store_true",
        help="also show each term (for the owner only)",
    )
    args = parser.parse_args(argv)
    try:
        root_env = os.environ.get("DARZI_DATA_ROOT", "").strip()
        if not root_env:
            raise ScanError(
                "DARZI_DATA_ROOT is not set. Set it to the real data root and push "
                "again."
            )
        root = Path(root_env).expanduser()
        if not root.is_dir():
            raise ScanError("DARZI_DATA_ROOT is not a folder. Correct it.")
        patterns = build_patterns(root)
        allow = frozenset(_lines(root / ALLOW_FILE))
        commits = new_commits(args.remote, stdin if stdin is not None else sys.stdin)
        findings: list[Finding] = []
        for commit in commits:
            findings.extend(scan_commit(commit, patterns, allow))
    except ScanError as exc:
        print(f"privacy scan: {exc} Push stopped.", file=sys.stderr)
        return 1
    if findings:
        print(report(findings, args.show_terms), file=sys.stderr)
        return 1
    print(
        f"privacy scan: {len(commits)} commits scanned, no findings.", file=sys.stderr
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
