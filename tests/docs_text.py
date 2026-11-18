"""Helpers for the tests of the agent workflow and the README text."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from darzi import cli

ROOT = Path(__file__).resolve().parents[1]


def privacy_scan() -> ModuleType:
    """Import scripts/privacy_scan.py (the pre-push gate) with no change to it."""
    name = "darzi_test_privacy_scan"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "privacy_scan.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The dataclasses of the module need the module in sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def built_commands() -> set[str]:
    """Return each `facts` command that the parser has and that is built."""
    parser = cli.build_parser()
    [sub] = [
        a
        for a in parser._actions  # pyright: ignore[reportPrivateUsage]
        if isinstance(a, argparse._SubParsersAction)  # pyright: ignore[reportPrivateUsage]
    ]
    return set(sub.choices) - set(cli.PLANNED)
