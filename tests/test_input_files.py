"""Bad input files give one short line and exit code 2 (issue 13, follow-up F-1).

Point 1: `facts check SELECTION`, `facts export`, `facts public` and
`facts query` test the rules.yaml keys that they read. Point 3: each failure of
the YAML load of master.yaml, rules.yaml and a selection is invalid input.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from darzi.cli import main
from darzi.facts.check import PUBLIC_KEYS, PUBLIC_NESTED_KEYS, SELECTION_KEYS
from darzi.facts.repo import InputError, read_yaml

from .conftest import DATA
from .signing_root import approve, setup_signing

NOT_ROOT = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="a permission test needs a POSIX user that is not root",
)
SELECTION = Path("selections") / "corporate-tech.yaml"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A temporary copy of the synthetic data root that a test can change."""
    folder = tmp_path / "root"
    shutil.copytree(DATA, folder)
    return folder


def _one_line(capsys: pytest.CaptureFixture[str], *parts: str) -> str:
    """Assert: stderr is exactly one line that holds each of `parts`."""
    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err
    assert err.endswith("\n") and err.count("\n") == 1, err
    assert "Traceback" not in err
    for part in parts:
        assert part in err, (part, err)
    return err


# --- Point 1: the keys of rules.yaml -------------------------------------------


def _run(command: str, root: Path, tmp_path: Path) -> int:
    repo = ["--repo", str(root)]
    if command == "check":
        return main(["check", str(root / SELECTION), *repo])
    if command == "export":
        run = tmp_path / "run"
        return main(["export", str(root / SELECTION), "--run", str(run), *repo])
    return main([command, *repo])


def _cases(
    table: dict[str, tuple[str, ...]],
    nested: tuple[tuple[str, str, tuple[str, ...], bool], ...],
) -> list[tuple[tuple[str, ...], str]]:
    """Return (key path, expected text) for each key that the table names."""
    cases: list[tuple[tuple[str, ...], str]] = []
    for name, keys in table.items():
        cases.append(((name,), f"rules.yaml has no entry {name}."))
        cases += [((name, k), f"rules.yaml: {name} has no key {k}.") for k in keys]
    for name, value, keys, _ in nested:
        cases += [
            ((name, value, k), f"rules.yaml: {name}.{value} has no key {k}.")
            for k in keys
        ]
    return cases


COMMAND_CASES = [
    (command, path, text)
    for command, table, nested in [
        ("check", SELECTION_KEYS, ()),
        ("export", PUBLIC_KEYS, PUBLIC_NESTED_KEYS),
        ("public", PUBLIC_KEYS, PUBLIC_NESTED_KEYS),
        ("query", PUBLIC_KEYS, PUBLIC_NESTED_KEYS),
    ]
    for path, text in _cases(table, nested)
]


def test_the_tables_name_the_keys_that_the_commands_read() -> None:
    assert SELECTION_KEYS == {"removed_items": (), "doubtful_items": ()}
    assert PUBLIC_KEYS == {
        "public_contact": ("email", "phone"),
        "private_contact_patterns": ("id", "severity", "terms", "regex"),
        "privacy_patterns": ("id", "severity", "regex", "terms"),
        "removed_items": ("terms",),
        "doubtful_items": ("terms",),
    }
    assert PUBLIC_NESTED_KEYS == (
        ("private_contact_patterns", "regex", ("any_email", "any_phone"), False),
    )


@pytest.mark.parametrize(("command", "path", "text"), COMMAND_CASES)
def test_a_missing_rules_key_is_invalid_input(
    root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    path: tuple[str, ...],
    text: str,
) -> None:
    rules: Any = yaml.safe_load((root / "rules.yaml").read_text(encoding="utf-8"))
    node = rules
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    (root / "rules.yaml").write_text(yaml.safe_dump(rules), encoding="utf-8")
    assert _run(command, root, tmp_path) == 2
    _one_line(capsys, f"facts {command}: ", text, "Add it to rules.yaml.")
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("command", ["check", "export", "public", "query"])
@pytest.mark.parametrize(
    ("content", "text"),
    [
        ("", "rules.yaml has no rule entries."),
        ("- a\n", "rules.yaml has no rule entries."),
        # The complete rules.yaml with one entry that is a list.
        ("removed_items", "rules.yaml: removed_items is not a mapping."),
        ("doubtful_items", "rules.yaml: doubtful_items is not a mapping."),
    ],
    ids=["empty", "list", "removed-not-a-mapping", "doubtful-not-a-mapping"],
)
def test_rules_that_are_not_mappings_are_invalid_input(
    root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    content: str,
    text: str,
) -> None:
    path = root / "rules.yaml"
    if content.endswith("_items"):
        rules: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        rules[content] = ["a"]
        content = yaml.safe_dump(rules)
    path.write_text(content, encoding="utf-8")
    assert _run(command, root, tmp_path) == 2
    _one_line(capsys, f"facts {command}: ", text)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize(("command", "code"), [("check", 0), ("public", 0)])
def test_a_key_that_the_command_does_not_read_is_not_tested(
    root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    code: int,
) -> None:
    # Option B of the plan: only the keys that the command reads.
    rules: Any = yaml.safe_load((root / "rules.yaml").read_text(encoding="utf-8"))
    del rules["education_forbidden"]
    if command == "check":
        del rules["public_contact"]
    (root / "rules.yaml").write_text(yaml.safe_dump(rules), encoding="utf-8")
    assert _run(command, root, tmp_path) == code
    assert capsys.readouterr().err == ""


def test_export_with_complete_rules_writes_base_json(
    root: Path, tmp_path: Path
) -> None:
    setup_signing(root, tmp_path / "keys")
    assert approve(root, root / SELECTION) == 0
    assert _run("export", root, tmp_path) == 0
    assert (tmp_path / "run" / "base.json").is_file()


# --- Point 3: the YAML load of master.yaml, rules.yaml and a selection ----------

DEEP_LIST = "x: " + "[" * 1000 + "]" * 1000 + "\n"
YAML_CASES: list[tuple[bytes, str]] = [
    (b"x: 2026-02-30\n", "has a value that is not valid, for example a date"),
    (DEEP_LIST.encode(), "has lists or mappings that are nested too deep."),
    (b"x: !!bool maybe\n", "has a value that YAML cannot read."),
    (b'x: !!int ""\n', "has a value that YAML cannot read."),
    (b"x: [unclosed\n", "is not valid YAML (line 2, column 1). Fix the YAML."),
    (b"x: Caf\xe9\n", "is not UTF-8 text. Save it as UTF-8."),
]
YAML_IDS = ["bad-date", "deep-list", "bad-bool", "bad-int", "syntax", "not-utf8"]
# (file, command): the command that loads the file.
FILES = [
    ("master.yaml", "validate"),
    ("rules.yaml", "validate"),
    ("selections/zz.yaml", "validate"),
    (str(SELECTION), "check"),
]


@pytest.mark.parametrize(("name", "command"), FILES)
@pytest.mark.parametrize(("content", "text"), YAML_CASES, ids=YAML_IDS)
def test_a_bad_yaml_file_is_one_line_and_exit_2(
    root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    command: str,
    content: bytes,
    text: str,
) -> None:
    (root / name).write_bytes(content)
    assert _run(command, root, tmp_path) == 2
    _one_line(capsys, f"facts {command}: ", str(root / name), text)


@NOT_ROOT
@pytest.mark.parametrize(("name", "command"), FILES)
def test_a_yaml_file_that_cannot_be_read_is_one_line_and_exit_2(
    root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    command: str,
) -> None:
    path = root / name
    path.write_text("x: 1\n", encoding="utf-8")
    old = path.stat().st_mode
    path.chmod(0)
    try:
        assert _run(command, root, tmp_path) == 2
    finally:
        path.chmod(old)
    _one_line(capsys, str(path), "cannot be read: Permission denied.")


def test_a_valid_file_reads_as_before(root: Path) -> None:
    for name in ("master.yaml", "rules.yaml", str(SELECTION)):
        path = root / name
        with path.open(encoding="utf-8") as f:
            expected = yaml.safe_load(f)
        assert read_yaml(path, "") == expected


def test_line_ends_read_as_before(tmp_path: Path) -> None:
    path = tmp_path / "a.yaml"
    path.write_bytes(b'a: "x\r\n  y"\r\nb: |\r\n  one\r\n  two\r\nc: 1\r')
    with path.open(encoding="utf-8") as f:
        expected = yaml.safe_load(f)
    assert read_yaml(path, "") == expected == {"a": "x y", "b": "one\ntwo\n", "c": 1}


def test_a_missing_file_names_the_fix(tmp_path: Path) -> None:
    with pytest.raises(InputError) as exc:
        read_yaml(tmp_path / "a.yaml", "Restore it from git.")
    assert (
        str(exc.value) == f"{tmp_path / 'a.yaml'} does not exist. Restore it from git."
    )


def test_the_size_limit_and_the_alias_test_are_optional(tmp_path: Path) -> None:
    path = tmp_path / "a.yaml"
    path.write_text("a: &x 1\nb: *x\n" + "#" * 2000 + "\n", encoding="utf-8")
    assert read_yaml(path, "") == {"a": 1, "b": 1}
    with pytest.raises(InputError, match="larger than 1 KiB"):
        read_yaml(path, "", max_bytes=1024)
    with pytest.raises(InputError, match="uses a YAML alias"):
        read_yaml(path, "", aliases=False)
