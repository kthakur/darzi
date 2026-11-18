"""Tests of the release: the version, CHANGELOG.md and the install at a tag.

The CI privacy gate scans src/, tests/, docs/ and scripts/, not CHANGELOG.md.
So this test runs the functions of both gates on CHANGELOG.md, as
tests/test_readme.py does on README.md.
"""

from __future__ import annotations

import importlib.metadata
import re
import tomllib

from darzi import __version__

from .conftest import DATA
from .docs_text import ROOT, privacy_scan
from .test_privacy_gate import findings
from .test_readme import README, SYNTHETIC_OWNER

CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
VERSION: str = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]["version"]


def test_the_installed_version_is_the_project_version() -> None:
    assert importlib.metadata.version("darzi") == VERSION
    assert __version__ == VERSION


def test_the_lock_file_has_the_project_version() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    [darzi] = [p for p in lock["package"] if p["name"] == "darzi"]
    assert darzi["version"] == VERSION


def test_the_changelog_has_a_heading_for_the_version() -> None:
    headings = re.findall(r"^## (\d+\.\d+\.\d+) \(", CHANGELOG, flags=re.MULTILINE)
    assert headings, "CHANGELOG.md has no version heading."
    assert headings[0] == VERSION


def test_the_changelog_points_to_the_readme_limits() -> None:
    assert '"Limits of each step"' in CHANGELOG
    assert "\n## Limits of each step\n" in README


def test_the_changelog_has_no_real_email_or_phone() -> None:
    assert findings(CHANGELOG) == []


def test_the_changelog_has_no_synthetic_private_term() -> None:
    scan = privacy_scan()
    patterns = scan.build_patterns(DATA)
    found = scan.scan_text(CHANGELOG, patterns, SYNTHETIC_OWNER, "CHANGELOG.md")
    assert found == []


def test_the_readme_installs_at_a_tag_with_a_placeholder_address() -> None:
    assert "git+https://github.com/<owner>/darzi@v" in README
    addresses = re.findall(r"github\.com/([^/\s\"`]+)/darzi", README)
    assert addresses and set(addresses) == {"<owner>"}
    for step in ("`uv lock`", "`uv sync`", "`uv run facts --version`"):
        assert step in README
    assert "`uv run facts doctor`" in README
