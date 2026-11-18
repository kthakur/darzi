"""Tests of README.md: the privacy gates, the command table and the W-12 marker.

The CI privacy gate scans src/, tests/, docs/ and scripts/, not README.md. So
this test runs the functions of both gates on README.md, with no change to
either gate.
"""

from __future__ import annotations

import re

from .conftest import DATA
from .docs_text import ROOT, built_commands, privacy_scan
from .test_privacy_gate import findings

README = (ROOT / "README.md").read_text(encoding="utf-8")
W12_MARKER = "<!-- W-12: fit rating limits (SPEC section 14) -->"
# The README may show the synthetic owner of tests/data: the name and the
# phone number of "Test Person", and the word "test".
SYNTHETIC_OWNER = frozenset({"Test Person", "Test", "Person", "test", "555-0100"})


def table_commands() -> list[str]:
    """Return the command of each row of the command table, in order."""
    return re.findall(r"^\| `facts ([a-z][a-z-]*)` \|", README, flags=re.MULTILINE)


def test_the_readme_has_no_real_email_or_phone() -> None:
    assert findings(README) == []


def test_the_readme_has_no_synthetic_private_term() -> None:
    scan = privacy_scan()
    patterns = scan.build_patterns(DATA)
    found = scan.scan_text(README, patterns, SYNTHETIC_OWNER, "README.md")
    assert found == []


def test_the_scan_finds_a_synthetic_term_in_a_changed_readme() -> None:
    scan = privacy_scan()
    patterns = scan.build_patterns(DATA)
    changed = README + "\nWe worked with Globex Corporation.\n"
    assert scan.scan_text(changed, patterns, SYNTHETIC_OWNER, "README.md")


def test_the_command_table_lists_exactly_the_built_commands() -> None:
    listed = table_commands()
    assert len(listed) == len(set(listed))
    assert set(listed) == built_commands()


def test_each_facts_command_in_the_readme_is_built() -> None:
    used = set(re.findall(r"`facts ([a-z][a-z-]*)", README))
    assert used <= built_commands(), used - built_commands()


def test_the_w12_marker_is_replaced_by_the_fit_limits() -> None:
    assert W12_MARKER not in README
    assert "This part comes with the fit rating." not in README
    section = README.split("### The fit rating", 1)[1].split("\n## ", 1)[0]
    flat = " ".join(section.split())
    for limit in (
        "It does not show your chance of an interview.",
        "the agent decides which fact matches which requirement. You can read "
        "each pair in `fit.json`.",
        "the rating never blocks a run. A red rating stops the agent only when "
        "a hard requirement is not met. Then you decide.",
    ):
        assert limit in flat, limit


def test_the_readme_length() -> None:
    assert 250 <= len(README.splitlines()) <= 310


def test_the_readme_keeps_the_hand_over_path_for_later() -> None:
    assert "--handover" not in README
    assert "comes in a later version" in README


def test_the_key_commands_ask_for_a_passphrase() -> None:
    assert "ssh-keygen -t ed25519 -f ~/.ssh/darzi_approval" in README
    assert '-N ""' not in README and "-N ''" not in README
    assert 'owner namespaces="darzi-approval" ssh-ed25519' in README
