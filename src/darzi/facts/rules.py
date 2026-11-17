"""Checks for rules.yaml itself (D-10, D-11, D-12, D-13).

The checker (`facts check`) reads these rules. This module only
makes sure the rules file is complete and usable. It never writes a file.
"""

import re
from typing import Any

from jsonschema import Draft202012Validator

from darzi.facts.patterns import term_regex
from darzi.facts.validate import Finding

NOT_RULES = ("schema_version", "agents_hard_rules")


def rule_entries(rules: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the rule entries of rules.yaml (every key except the metadata)."""
    return {
        k: v for k, v in rules.items() if k not in NOT_RULES and isinstance(v, dict)
    }


def validate_rules(rules: Any, schema: dict[str, Any]) -> list[Finding]:
    """Return findings for rules.yaml: structure (D-11), patterns, D-12 mapping."""
    findings: list[Finding] = []
    for err in sorted(
        Draft202012Validator(schema).iter_errors(rules), key=lambda e: list(e.path)
    ):
        where = ".".join(str(p) for p in err.absolute_path) or "(top level)"
        findings.append(
            Finding(
                "D-11",
                "error",
                f"rules.yaml: {where}",
                f"{err.message}. Fix it to match the rules schema.",
            )
        )
    if findings or not isinstance(rules, dict):
        return findings
    findings += _pattern_findings(rule_entries(rules))
    findings += _mapping_findings(rules)
    return findings


def _pattern_findings(entries: dict[str, dict[str, Any]]) -> list[Finding]:
    findings: list[Finding] = []
    for key, entry in entries.items():
        regex: Any = entry.get("regex")
        patterns: list[Any] = (
            list(regex.values()) if isinstance(regex, dict) else list(regex or [])
        )
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                findings.append(
                    Finding(
                        "D-13",
                        "error",
                        f"rules.yaml: {key}.regex",
                        f"Regex {pattern!r} does not compile: {exc}. Fix the regex.",
                    )
                )
        for term in entry.get("terms") or []:
            try:
                term_regex(term)
            except ValueError as exc:
                findings.append(
                    Finding("D-13", "error", f"rules.yaml: {key}.terms", str(exc))
                )
    return findings


def _mapping_findings(rules: dict[str, Any]) -> list[Finding]:
    """D-12: each hard rule names existing entries, or says why it has no check."""
    entries = rule_entries(rules)
    findings: list[Finding] = []
    for number, item in rules["agents_hard_rules"].items():
        where = f"rules.yaml: agents_hard_rules.{number}"
        names: list[str] = item["entries"]
        if not names and not item.get("not_checked"):
            findings.append(
                Finding(
                    "D-12",
                    "error",
                    where,
                    "Hard rule has no entries. Name the rules.yaml entries that "
                    "check it, or explain in not_checked why none can.",
                )
            )
        for name in names:
            if name not in entries:
                findings.append(
                    Finding(
                        "D-12",
                        "error",
                        where,
                        f"Entry {name!r} does not exist in rules.yaml. Fix the name.",
                    )
                )
    return findings
