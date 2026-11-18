"""Live test of `facts finalize RUN` on a Resume Matcher TEST instance.

The test needs the environment variable DARZI_RENDER_TEST (see
tests/test_render_live.py) and LibreOffice. The run is synthetic, and it is
approved with a key that the test makes in a temporary folder, below the
terminal test, as in the W-5 tests. Never point DARZI_RENDER_TEST at the
owner's own Resume Matcher data folder.
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
from darzi.facts.check import load_run
from darzi.render.pages import find_soffice
from darzi.runs.roundtrip import check_letter, check_resume

from .signing_root import approve, signed_copy

VARIABLE = "DARZI_RENDER_TEST"
RUN = "2026-11-17-acme-product-designer"
LETTER = "Dear team,\n\nI design forms for Acme.\n\nKind regards,\nTest Person\n"
RUN_YAML = (
    "schema_version: 1\ncompany: Acme\nrole: Product Designer\n"
    "created: '2026-11-17'\ntype: ux\nselection_hash: null\nstatus: new\n"
)

pytestmark = [pytest.mark.renderer, pytest.mark.libreoffice]


def _instance() -> dict[str, Any]:
    name = os.environ.get(VARIABLE)
    if not name:
        pytest.skip(f"no Resume Matcher test instance ({VARIABLE} is not set)")
    if find_soffice() is None:
        pytest.skip("LibreOffice is not installed")
    with Path(name).open("rb") as f:
        data: Any = tomllib.load(f)
    return data["render"]["resume_matcher"]


def test_finalize_on_the_test_instance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rm = _instance()
    root = signed_copy(tmp_path / "signed")
    toml = root / "facts.toml"
    text = toml.read_text(encoding="utf-8").split("[render]")[0] + (
        '[render]\nadapter = "resume-matcher"\n\n[render.resume_matcher]\n'
        + "".join(
            f"{key} = {json.dumps(rm[key])}\n"
            for key in ("path", "data_dir", "frontend_url", "backend_url")
        )
    )
    toml.write_text(text, encoding="utf-8")
    run = root / "applications" / RUN
    selection = root / "selections" / "ux.yaml"
    assert main(["export", str(selection), "--run", str(run), "--repo", str(root)]) == 0
    shutil.copy(run / "base.json", run / "tailored.json")
    (run / "cover_letter.md").write_text(LETTER, encoding="utf-8")
    (run / "run.yaml").write_text(RUN_YAML, encoding="utf-8")
    assert approve(root, run) == 0
    capsys.readouterr()

    code = main(["finalize", str(run), "--repo", str(root), "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert json.loads(captured.out)["manifest"] == manifest
    assert manifest["signed"] is True
    files = manifest["files"]
    assert [f["kind"] for f in files] == ["resume_pdf", "resume_word", "letter_word"]
    approved = load_run(run)
    for item in files:
        path = run / item["path"]
        assert path.is_file() and item["pages"] >= 1
        if item["kind"] == "letter_word":
            assert check_letter(LETTER, path) == []
        else:
            assert check_resume(approved.tailored, path) == []
    assert "status: finalized" in (run / "run.yaml").read_text(encoding="utf-8")
    print(
        "live finalize:",
        [(f["kind"], f["pages"]) for f in files],
        manifest["renderer"],
    )
