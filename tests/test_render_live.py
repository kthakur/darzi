"""Live test of `facts render RUN --draft` on a Resume Matcher TEST instance.

The test runs only when the environment variable DARZI_RENDER_TEST names a TOML
file with a `[render.resume_matcher]` table for a test instance: `path`,
`data_dir`, `frontend_url` and `backend_url`, with absolute paths. The backend
and frontend servers of that instance must run with the same data folder. Only
this test reads the variable; `src/darzi` does not.

The data folder of the test instance must hold no record, or only the scratch
record. Never point the file at the owner's own Resume Matcher data folder.
"""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from pathlib import Path
from typing import Any

import pytest

from darzi.cli import main
from darzi.config import load_config
from darzi.render.resume_matcher import SCRATCH_TITLE, from_settings

from .conftest import DATA

VARIABLE = "DARZI_RENDER_TEST"
EXPORT = DATA.parent / "expected" / "corporate-tech.export.json"
RUN = "2026-11-03-example-labs-product-designer"

pytestmark = pytest.mark.renderer


def _instance() -> dict[str, Any]:
    name = os.environ.get(VARIABLE)
    if not name:
        pytest.skip(f"no Resume Matcher test instance ({VARIABLE} is not set)")
    with Path(name).open("rb") as f:
        data: Any = tomllib.load(f)
    return data["render"]["resume_matcher"]


def _snapshot(folder: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (str(p.relative_to(folder)), p.stat().st_size, p.stat().st_mtime_ns)
        for p in [folder, *folder.rglob("*")]
    )


def test_draft_render_two_times_on_the_test_instance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rm = _instance()
    root = tmp_path / "root"
    shutil.copytree(DATA, root)
    text = (root / "facts.toml").read_text(encoding="utf-8")
    text = text.split("[render]")[0] + (
        '[render]\nadapter = "resume-matcher"\n\n[render.resume_matcher]\n'
        + "".join(
            f"{key} = {json.dumps(rm[key])}\n"
            for key in ("path", "data_dir", "frontend_url", "backend_url")
        )
    )
    (root / "facts.toml").write_text(text, encoding="utf-8")
    run = root / "applications" / RUN
    export: dict[str, Any] = json.loads(EXPORT.read_text(encoding="utf-8"))
    for name, data in (
        ("base.json", export["base"]),
        ("base.map.json", export["base_map"]),
        ("tailored.json", export["base"]),
    ):
        (run / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    renderer = from_settings(root, load_config(root).render)
    records = renderer.call("list_resumes", {"include_tailored": True})["resumes"]
    assert all(r["title"] == SCRATCH_TITLE for r in records), (
        "the test data folder holds other records: use an empty test data folder"
    )
    assert len(records) <= 1

    before = _snapshot(run)
    results: list[dict[str, Any]] = []
    for _ in range(2):
        code = main(["render", str(run), "--draft", "--repo", str(root), "--json"])
        captured = capsys.readouterr()
        assert code == 0, captured.err
        results.append(json.loads(captured.out))
    assert _snapshot(run) == before
    for result in results:
        pdf = Path(result["pdf"])
        assert pdf.read_bytes().startswith(b"%PDF-")
        assert isinstance(result["pages"], int) and result["pages"] >= 1
        assert run.resolve() not in pdf.resolve().parents
    assert results[0]["pdf"] != results[1]["pdf"]
    records = renderer.call("list_resumes", {"include_tailored": True})["resumes"]
    assert [r["title"] for r in records] == [SCRATCH_TITLE]
    print(
        "live render:",
        [(r["pages"], r["template"], r["page_size"]) for r in results],
    )
    for result in results:
        shutil.rmtree(Path(result["pdf"]).parent)
