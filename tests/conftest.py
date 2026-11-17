"""Shared fixtures: a minimal synthetic master.yaml and a temporary data root."""

from __future__ import annotations

import copy
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from darzi.schemas import MASTER, load_schema

DATA = Path(__file__).resolve().parent / "data"

# The smallest synthetic master.yaml that passes every check. Tests change a copy.
MINIMAL: dict[str, Any] = {
    "source_files": {"cv-a": "Test resume A", "confirmed": "Facts the owner confirmed"},
    "profile": {
        "name": "Test Person",
        "email": "test.person@example.com",
        "phone": "555-0100",
        "contact_sources": ["confirmed"],
        "headlines": [{"text": "Designer", "sources": ["cv-a"]}],
        "residence": [
            {"place": "Springfield, ST", "from": "2019-06", "until": "present"}
        ],
    },
    "roles": [
        {
            "id": "acme",
            "org": "Acme",
            "title": "Designer",
            "location": "Springfield, ST",
            "start": "2020-01",
            "end": "present",
            "type": "employee",
            "verified": False,
            "verified_fields": [],
            "sources": ["cv-a"],
            "notes": "",
            "bullets": [
                {
                    "id": "acme-forms",
                    "text": "Designed the sign-up forms.",
                    "tags": ["ux"],
                    "evidence": "",
                    "sources": ["cv-a"],
                }
            ],
            "positions": [{"title": "Designer", "start": "2020-01", "end": "present"}],
        }
    ],
    "projects": [
        {
            "id": "notes-app",
            "org": "Independent",
            "title": "Notes App",
            "location": "",
            "start": "2024",
            "end": "",
            "type": "independent",
            "verified": False,
            "verified_fields": ["unpaid"],
            "sources": ["cv-a"],
            "notes": "",
            "bullets": [],
            "alt_names": ["Jotter"],
        }
    ],
    "education": [
        {
            "id": "example-ba",
            "institution": "Example University",
            "credential": "B.A.",
            "status": "completed",
            "start": 2006,
            "end": "2009-05",
            "verified": True,
            "sources": ["confirmed"],
        }
    ],
    "skills": [{"name": "Figma", "tags": ["design-tool"], "sources": ["cv-a"]}],
}


@pytest.fixture
def master() -> dict[str, Any]:
    """A fresh copy of the minimal valid master.yaml data."""
    return copy.deepcopy(MINIMAL)


@pytest.fixture
def schema() -> dict[str, Any]:
    """The master schema from the package data."""
    return load_schema(MASTER)


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[[Any], Path]:
    """Write `data` as master.yaml in a temporary data root with the test rules."""

    def _make(data: Any) -> Path:
        shutil.copy(DATA / "rules.yaml", tmp_path)
        text = data if isinstance(data, str) else yaml.safe_dump(data)
        (tmp_path / "master.yaml").write_text(text, encoding="utf-8")
        return tmp_path

    return _make
