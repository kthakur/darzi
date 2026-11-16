"""Tests of scripts/privacy_scan.py with a synthetic data root in a temporary folder.

No test reads the real data root. Each test makes a git repo with a bare remote
and runs the scan through the pre-push interface: the remote name and URL as
arguments, and the push lines on standard input.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCAN = ROOT / "scripts" / "privacy_scan.py"
PRE_PUSH = ROOT / "scripts" / "pre-push"
ZERO = "0" * 40

RULES = {
    "schema_version": 1,
    "public_contact": {
        "id": "R-1",
        "severity": "error",
        "reason": "Only the public contact data.",
        "email": "test.person@example.com",
        "phone": "+1 212 555 0142",
    },
    "private_contact_patterns": {
        "id": "R-1",
        "severity": "error",
        "reason": "Private contact data.",
        "terms": ["Quillmore Lane"],
        "regex": {"any_email": r"\S+@\S+", "any_phone": r"\d{3}"},
    },
    "privacy_patterns": {
        "id": "PV-3",
        "severity": "error",
        "reason": "Private words.",
        "terms": ["Brindlewick"],
        "regex": {"case_number": r"CASE-\d{4}"},
    },
    "employer_of_record": {
        "id": "R-9",
        "severity": "error",
        "reason": "The employer of record.",
        "required_any": {"acme-ops": ["Placewell Staffing"]},
    },
    "never_as_company": {
        "id": "R-14",
        "severity": "error",
        "reason": "Never a company.",
        "terms": ["Person Consulting"],
        "freelance_only": [],
    },
}

MASTER = {
    "source_files": [],
    "profile": {
        "name": "Test Person",
        "email": "tp.private@example.com",
        "phone": "212-555-0177",
        "links": [
            {
                "label": "Portfolio",
                "url": "https://www.zoravelt.example.com/work/",
                "sources": ["harbor-notes.pdf"],
            }
        ],
    },
    "roles": [
        {"id": "acme-ops", "org": "Acme Holdings", "display_org": "Acme Labs"},
    ],
    "projects": [
        {"id": "tide-tool", "org": "Orvanta", "alt_names": ["Tidewatch"]},
    ],
    "education": [
        {"id": "edu-one", "institution": "Gallowmere College"},
    ],
    "skills": [],
}


class Repo:
    """A work repo with a bare remote named origin, both in a temporary folder."""

    def __init__(self, base: Path) -> None:
        self.remote = base / "remote.git"
        self.work = base / "work"
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.com",
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": "author@example.com",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.git("init", "--bare", "-q", str(self.remote), cwd=base)
        self.git("init", "-q", "-b", "main", str(self.work), cwd=base)
        self.git("remote", "add", "origin", str(self.remote))
        self.git("config", "core.hooksPath", os.devnull)

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.work,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def commit(self, files: dict[str, str], message: str = "Change files") -> str:
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def remove(self, name: str, message: str = "Remove a file") -> str:
        self.git("rm", "-q", name)
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def push(self, ref: str = "main") -> str:
        """Push with no hook, as history that is already on the remote."""
        self.git("push", "-q", "origin", ref)
        return self.git("rev-parse", ref)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    (root / "rules.yaml").write_text(yaml.safe_dump(RULES), encoding="utf-8")
    (root / "master.yaml").write_text(yaml.safe_dump(MASTER), encoding="utf-8")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    r = Repo(tmp_path)
    r.commit({"README.md": "A clean start.\n"}, "Start")
    r.push()
    return r


def run_scan(
    repo: Repo,
    data_root: Path | None,
    lines: list[str],
    *extra: str,
    command: list[str] | None = None,
    path: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(repo.env)
    env.pop("DARZI_DATA_ROOT", None)
    if data_root is not None:
        env["DARZI_DATA_ROOT"] = str(data_root)
    if path is not None:
        env["PATH"] = path
    cmd = command or [sys.executable, str(SCAN)]
    return subprocess.run(
        [*cmd, "origin", str(repo.remote), *extra],
        cwd=repo.work,
        env=env,
        input="".join(f"{line}\n" for line in lines),
        capture_output=True,
        text=True,
    )


def push_line(local: str, remote: str, ref: str = "main") -> str:
    return f"refs/heads/{ref} {local} refs/heads/{ref} {remote}"


def scan_one(
    repo: Repo, data_root: Path, files: dict[str, str], message: str = "Change"
) -> subprocess.CompletedProcess[str]:
    remote = repo.head()
    local = repo.commit(files, message)
    return run_scan(repo, data_root, [push_line(local, remote)])


def test_clean_push_passes(repo: Repo, data_root: Path) -> None:
    result = scan_one(repo, data_root, {"notes.md": "Acme Example Corp only.\n"})
    assert result.returncode == 0, result.stderr
    assert "no findings" in result.stderr


@pytest.mark.parametrize(
    "text",
    [
        "Worked at Acme Holdings.",
        "Shown as Acme Labs.",
        "Built Tidewatch.",
        "Client: Orvanta.",
        "Studied at Gallowmere College.",
        "See zoravelt.example.com/work for more.",
        "Ref acme-ops in the notes.",
        "Lives on Quillmore Lane.",
        "The Brindlewick family.",
        "Placed by Placewell Staffing.",
        "Not Person Consulting.",
        "Mail tp.private@example.com.",
        "Mail test.person@example.com.",
        "Call (212) 555-0177.",
        "Call 212.555.0142.",
        "File CASE-1234 here.",
        "Written by Test Person.",
        "Hello Person.",
    ],
)
def test_term_in_file_fails(repo: Repo, data_root: Path, text: str) -> None:
    result = scan_one(repo, data_root, {"docs/notes.md": f"Line one.\n{text}\n"})
    assert result.returncode == 1
    assert "docs/notes.md, line 2: private data." in result.stderr


@pytest.mark.parametrize(
    "text",
    [
        "Link: https://www.zoravelt.example.com/work/ here.",
        "Host zoravelt.example.com only.",
        "Host www.zoravelt.example.com only.",
        "Other page zoravelt.example.com/blog.",
    ],
)
def test_link_url_and_host_fail(repo: Repo, data_root: Path, text: str) -> None:
    result = scan_one(repo, data_root, {"a.md": f"{text}\n"})
    assert result.returncode == 1
    assert "a.md, line 1: private data." in result.stderr


def test_link_label_and_sources_pass(repo: Repo, data_root: Path) -> None:
    text = "My Portfolio is in harbor-notes.pdf and at example.com.\n"
    result = scan_one(repo, data_root, {"a.md": text})
    assert result.returncode == 0, result.stderr


def test_output_never_shows_the_term(repo: Repo, data_root: Path) -> None:
    result = scan_one(repo, data_root, {"a.md": "Studied at Gallowmere College.\n"})
    assert result.returncode == 1
    assert "Gallowmere" not in result.stderr + result.stdout


def test_show_terms_shows_the_term(repo: Repo, data_root: Path) -> None:
    remote = repo.head()
    local = repo.commit({"a.md": "Studied at Gallowmere College.\n"})
    result = run_scan(repo, data_root, [push_line(local, remote)], "--show-terms")
    assert result.returncode == 1
    assert "Gallowmere College" in result.stderr


def test_other_case_and_line_break_fail(repo: Repo, data_root: Path) -> None:
    text = "one\nACME HOLDINGS\ntwo gallowmere\ncollege\n"
    result = scan_one(repo, data_root, {"a.md": text})
    assert result.returncode == 1
    assert "a.md, line 2" in result.stderr
    assert "a.md, line 3" in result.stderr


def test_term_inside_a_word_passes(repo: Repo, data_root: Path) -> None:
    result = scan_one(repo, data_root, {"a.md": "Orvantage and xOrvanta.\n"})
    assert result.returncode == 0, result.stderr


def test_term_in_commit_message_fails(repo: Repo, data_root: Path) -> None:
    result = scan_one(repo, data_root, {"a.md": "clean\n"}, "Add notes\n\nFor Orvanta.")
    assert result.returncode == 1
    assert "commit message, line 3" in result.stderr


def test_term_in_file_name_fails(repo: Repo, data_root: Path) -> None:
    result = scan_one(repo, data_root, {"docs/orvanta-notes.md": "clean\n"})
    assert result.returncode == 1
    assert "docs/orvanta-notes.md (file name)" in result.stderr


def test_term_removed_by_later_commit_fails(repo: Repo, data_root: Path) -> None:
    remote = repo.head()
    repo.commit({"a.md": "Client: Orvanta.\n"}, "Add a file")
    local = repo.remove("a.md")
    result = run_scan(repo, data_root, [push_line(local, remote)])
    assert result.returncode == 1
    assert "a.md, line 1" in result.stderr


def test_new_branch_scans_only_commits_not_on_remote(
    repo: Repo, data_root: Path
) -> None:
    # A term in history that is already on the remote is not scanned again.
    repo.commit({"old.md": "Client: Orvanta.\n"}, "Old work")
    repo.push()
    repo.git("checkout", "-q", "-b", "feature")
    local = repo.commit({"new.md": "clean\n"}, "New work")
    result = run_scan(repo, data_root, [push_line(local, ZERO, "feature")])
    assert result.returncode == 0, result.stderr
    assert "1 commits scanned" in result.stderr


def test_new_branch_with_term_fails(repo: Repo, data_root: Path) -> None:
    repo.git("checkout", "-q", "-b", "feature")
    local = repo.commit({"new.md": "Client: Orvanta.\n"}, "New work")
    result = run_scan(repo, data_root, [push_line(local, ZERO, "feature")])
    assert result.returncode == 1
    assert "new.md, line 1" in result.stderr


def test_delete_push_passes(repo: Repo, data_root: Path) -> None:
    result = run_scan(repo, data_root, [push_line(ZERO, repo.head(), "old")])
    assert result.returncode == 0, result.stderr


def test_owner_name_allowed_only_in_licence_and_pyproject(
    repo: Repo, data_root: Path
) -> None:
    files = {
        "LICENSE": "Copyright (c) 2026 Test Person\n",
        "pyproject.toml": 'authors = [{ name = "Test Person" }]\n',
    }
    assert scan_one(repo, data_root, files).returncode == 0
    result = scan_one(repo, data_root, {"README.md": "By Test Person.\n"})
    assert result.returncode == 1
    assert "README.md, line 1" in result.stderr


def test_other_term_in_licence_fails(repo: Repo, data_root: Path) -> None:
    result = scan_one(repo, data_root, {"LICENSE": "Copyright Orvanta\n"})
    assert result.returncode == 1


def test_darzi_passes(repo: Repo, data_root: Path) -> None:
    (data_root / "privacy_scan_terms.txt").write_text("darzi\n", encoding="utf-8")
    result = scan_one(repo, data_root, {"a.md": "Darzi and darzi.\n"})
    assert result.returncode == 0, result.stderr


def test_term_file_adds_terms(repo: Repo, data_root: Path) -> None:
    (data_root / "privacy_scan_terms.txt").write_text(
        "\nKestrelgate\n\n", encoding="utf-8"
    )
    result = scan_one(repo, data_root, {"a.md": "The kestrelgate plan.\n"})
    assert result.returncode == 1


def test_allow_list_is_case_sensitive(repo: Repo, data_root: Path) -> None:
    (data_root / "privacy_scan_allow.txt").write_text("Orvanta\n", encoding="utf-8")
    assert scan_one(repo, data_root, {"a.md": "Orvanta.\n"}).returncode == 0
    for text in ("orvanta.\n", "ORVANTA.\n"):
        assert scan_one(repo, data_root, {"a.md": text}).returncode == 1


def test_allow_list_passes_exact_regex_finding(repo: Repo, data_root: Path) -> None:
    (data_root / "privacy_scan_allow.txt").write_text("CASE-0000\n", encoding="utf-8")
    assert scan_one(repo, data_root, {"a.md": "CASE-0000\n"}).returncode == 0
    assert scan_one(repo, data_root, {"a.md": "CASE-0001\n"}).returncode == 1


def test_any_patterns_are_not_used(repo: Repo, data_root: Path) -> None:
    result = scan_one(repo, data_root, {"a.md": "Mail info@example.org, 123.\n"})
    assert result.returncode == 0, result.stderr


def test_no_data_root_fails(repo: Repo) -> None:
    result = run_scan(repo, None, [push_line(repo.head(), repo.head())])
    assert result.returncode == 1
    assert "DARZI_DATA_ROOT is not set" in result.stderr


def test_data_root_with_no_rules_fails(repo: Repo, data_root: Path) -> None:
    (data_root / "rules.yaml").unlink()
    result = run_scan(repo, data_root, [push_line(repo.head(), repo.head())])
    assert result.returncode == 1
    assert "rules.yaml is not in DARZI_DATA_ROOT" in result.stderr


def test_pre_push_runs_the_scan(repo: Repo, data_root: Path) -> None:
    remote = repo.head()
    local = repo.commit({"a.md": "Client: Orvanta.\n"})
    result = run_scan(
        repo, data_root, [push_line(local, remote)], command=[str(PRE_PUSH)]
    )
    assert result.returncode == 1
    assert "a.md, line 1: private data." in result.stderr


def test_pre_push_fails_without_uv(repo: Repo, data_root: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    result = run_scan(
        repo,
        data_root,
        [push_line(repo.head(), repo.head())],
        command=["/bin/sh", str(PRE_PUSH)],
        path=str(empty),
    )
    assert result.returncode == 1
    assert "uv is not found" in result.stderr
