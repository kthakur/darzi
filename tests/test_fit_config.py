"""`config.py`: the [fit] section of facts.toml (work item W-12)."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from darzi.config import FACTS_TOML, FitSettings, exact, load_config
from darzi.facts.repo import InputError

from .conftest import DATA


def test_the_synthetic_limits() -> None:
    assert load_config(DATA).fit == FitSettings(0.7, 0.4)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", FitSettings(0.7, 0.4)),
        ("[fit]\n", FitSettings(0.7, 0.4)),
        ("[fit]\ngreen = 0.8\n", FitSettings(0.8, 0.4)),
        ("[fit]\nyellow = 0.1\n", FitSettings(0.7, 0.1)),
        ("[fit]\ngreen = 1\nyellow = 0\n", FitSettings(1.0, 0.0)),
        ("[fit]\ngreen = 0.5\nyellow = 0.5\n", FitSettings(0.5, 0.5)),
    ],
)
def test_fit_limits_are_read(tmp_path: Path, text: str, expected: FitSettings) -> None:
    (tmp_path / FACTS_TOML).write_text(text, encoding="utf-8")
    assert load_config(tmp_path).fit == expected


@pytest.mark.parametrize(
    "text",
    [
        "fit = 0.7\n",
        "[fit]\ngreen = true\n",
        '[fit]\ngreen = "0.7"\n',
        "[fit]\ngreen = nan\n",
        "[fit]\nyellow = inf\n",
        "[fit]\ngreen = 1.01\n",
        "[fit]\nyellow = -0.01\n",
        "[fit]\ngreen = 0.3\n",
        "[fit]\ngreen = 0.4\nyellow = 0.41\n",
    ],
)
def test_bad_fit_limits_are_refused(tmp_path: Path, text: str) -> None:
    (tmp_path / FACTS_TOML).write_text(text, encoding="utf-8")
    with pytest.raises(InputError, match=r"fit"):
        load_config(tmp_path)


def test_exact_uses_the_decimal_text() -> None:
    assert Fraction(0.2) != Fraction(2, 10)
    assert exact(0.2) == Fraction(2, 10)
    assert exact(1) == 1
