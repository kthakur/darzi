"""Tests of `facts render RUN --draft` and print_settings(), with fakes only."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from darzi import cli
from darzi.cli import main
from darzi.facts.repo import InputError
from darzi.render.port import Renderer, RenderError, RenderResult
from darzi.runs.runs import DEFAULT_PRINT, print_settings

from .conftest import DATA
from .render_fakes import FakeCli, fake_renderer

EXPORT = DATA.parent / "expected" / "corporate-tech.export.json"


@pytest.fixture(autouse=True)
def own_temp_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give the draft folders and the lock files a temporary folder of the test."""
    folder = tmp_path / "system-tmp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


def _root(tmp_path: Path) -> tuple[Path, Path]:
    """Return a copy of the synthetic data root and a run folder in it."""
    root = tmp_path / "root"
    shutil.copytree(DATA, root)
    run = root / "applications" / "2026-11-03-example-labs-product-designer"
    export: dict[str, Any] = json.loads(EXPORT.read_text(encoding="utf-8"))
    for name, data in (
        ("base.json", export["base"]),
        ("base.map.json", export["base_map"]),
        ("tailored.json", export["base"]),
    ):
        (run / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return root, run


def _snapshot(folder: Path) -> list[tuple[str, int, int]]:
    """Return each path in `folder` with its size and write time."""
    return sorted(
        (str(p.relative_to(folder)), p.stat().st_size, p.stat().st_mtime_ns)
        for p in [folder, *folder.rglob("*")]
    )


class FakeRenderer:
    """A renderer that writes a small PDF and records each call."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[dict[str, Any], dict[str, Any], Path]] = []
        self.error = error

    def render(
        self, resume: dict[str, Any], settings: dict[str, Any], out: Path
    ) -> RenderResult:
        self.calls.append((resume, settings, out))
        if self.error is not None:
            raise self.error
        out.write_bytes(b"%PDF-1.7\n")
        return RenderResult(pages=2, template="swiss-single", page_size="LETTER")


def _use(monkeypatch: pytest.MonkeyPatch, renderer: Renderer) -> None:
    def make(repo: Path) -> Renderer:
        return renderer

    monkeypatch.setattr(cli, "make_renderer", make)


def _no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a subprocess started")

    monkeypatch.setattr(subprocess, "run", refuse)


def test_draft_two_times_gives_two_pdfs_and_no_change_in_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, run = _root(tmp_path)
    fake = FakeRenderer()
    _use(monkeypatch, fake)
    before = _snapshot(run)
    pdfs: list[Path] = []
    for _ in range(2):
        assert main(["render", str(run), "--draft", "--repo", str(root)]) == 0
        out = capsys.readouterr().out
        assert "2 pages (swiss-single, LETTER)" in out
        pdfs.append(fake.calls[-1][2])
        assert str(pdfs[-1]) in out
    assert _snapshot(run) == before
    assert pdfs[0] != pdfs[1]
    for pdf in pdfs:
        assert pdf.read_bytes().startswith(b"%PDF")
        assert pdf.parent.name.startswith("darzi-draft-")
        assert run.resolve() not in pdf.resolve().parents
        shutil.rmtree(pdf.parent)
    tailored = json.loads((run / "tailored.json").read_text(encoding="utf-8"))
    assert fake.calls[0][0] == tailored
    assert fake.calls[0][1] == DEFAULT_PRINT


def test_draft_json_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, run = _root(tmp_path)
    _use(monkeypatch, FakeRenderer())
    assert main(["render", str(run), "--draft", "--repo", str(root), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"schema_version", "pdf", "pages", "template", "page_size"}
    assert data["pages"] == 2 and data["schema_version"] == 1
    assert Path(data["pdf"]).is_file()
    shutil.rmtree(Path(data["pdf"]).parent)


def test_draft_with_the_adapter_and_a_fake_cli_two_times(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, run = _root(tmp_path)
    fake_cli = FakeCli(pages=1)
    _use(monkeypatch, fake_renderer(tmp_path / "install", fake_cli))
    before = _snapshot(run)
    for _ in range(2):
        assert main(["render", str(run), "--draft", "--repo", str(root)]) == 0
        assert ": 1 pages" in capsys.readouterr().out
    assert _snapshot(run) == before
    assert len(fake_cli.records) == 1
    assert fake_cli.tools().count("create_master_resume") == 1
    assert fake_cli.tools().count("update_resume") == 1


def test_render_without_draft_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, run = _root(tmp_path)
    fake = FakeRenderer()
    _use(monkeypatch, fake)
    assert main(["render", str(run), "--repo", str(root)]) == 2
    assert "give --draft" in capsys.readouterr().err
    assert fake.calls == []


@pytest.mark.parametrize(
    ("error", "code"),
    [(RenderError("resume-matcher failed: x"), 1), (InputError("other record"), 2)],
)
def test_renderer_errors_give_exit_codes_and_remove_the_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    code: int,
) -> None:
    root, run = _root(tmp_path)
    fake = FakeRenderer(error)
    _use(monkeypatch, fake)
    before = _snapshot(run)
    assert main(["render", str(run), "--draft", "--repo", str(root)]) == code
    assert str(error) in capsys.readouterr().err
    assert not fake.calls[0][2].parent.exists()
    assert _snapshot(run) == before


def test_another_exception_rises_after_the_folder_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_temp_folder: Path
) -> None:
    root, run = _root(tmp_path)
    fake = FakeRenderer(ValueError("a bug in the renderer"))
    _use(monkeypatch, fake)
    with pytest.raises(ValueError, match="a bug in the renderer"):
        main(["render", str(run), "--draft", "--repo", str(root)])
    assert not fake.calls[0][2].parent.exists()
    assert list(own_temp_folder.iterdir()) == []


def test_a_nul_in_a_setting_gives_exit_code_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, run = _root(tmp_path)
    toml = root / "facts.toml"
    text = toml.read_text(encoding="utf-8")
    assert text.count('data_dir = ".rm-data"') == 1
    toml.write_text(text.replace('".rm-data"', '".rm\\u0000data"'), "utf-8")
    _no_subprocess(monkeypatch)
    assert main(["render", str(run), "--draft", "--repo", str(root)]) == 2
    assert "holds a NUL character" in capsys.readouterr().err


def test_bad_run_folders_give_exit_code_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, run = _root(tmp_path)
    fake = FakeRenderer()
    _use(monkeypatch, fake)
    assert main(["render", str(root / "nope"), "--draft", "--repo", str(root)]) == 2
    (run / "tailored.json").write_text("{", encoding="utf-8")
    assert main(["render", str(run), "--draft", "--repo", str(root)]) == 2
    (run / "tailored.json").write_text('{"a": 1}', encoding="utf-8")
    assert main(["render", str(run), "--draft", "--repo", str(root)]) == 2
    assert capsys.readouterr().err.count("facts render:") == 3
    assert fake.calls == []


def test_the_synthetic_settings_stop_before_a_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, run = _root(tmp_path)
    _no_subprocess(monkeypatch)
    assert main(["render", str(run), "--draft", "--repo", str(root)]) == 2
    assert "must be an http URL on localhost" in capsys.readouterr().err


def test_no_render_section_gives_exit_code_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, run = _root(tmp_path)
    (root / "facts.toml").write_text("[owner]\nname = 'Test Person'\n", "utf-8")
    _no_subprocess(monkeypatch)
    assert main(["render", str(run), "--draft", "--repo", str(root)]) == 2
    assert "no [render] section" in capsys.readouterr().err


# --- print_settings() ------------------------------------------------------------


def test_print_settings_default_is_us_letter(tmp_path: Path) -> None:
    assert print_settings(tmp_path) == {"pageSize": "LETTER"}
    (tmp_path / "run.yaml").write_text("status: new\n", encoding="utf-8")
    assert print_settings(tmp_path) == {"pageSize": "LETTER"}


def test_print_settings_of_run_yaml_replace_the_default(tmp_path: Path) -> None:
    (tmp_path / "run.yaml").write_text(
        "print_settings:\n  pageSize: A4\n  compactMode: true\n", encoding="utf-8"
    )
    assert print_settings(tmp_path) == {"pageSize": "A4", "compactMode": True}


def test_print_settings_that_are_not_a_mapping_are_refused(tmp_path: Path) -> None:
    (tmp_path / "run.yaml").write_text("print_settings: [1]\n", encoding="utf-8")
    with pytest.raises(InputError, match="print_settings is not a mapping"):
        print_settings(tmp_path)


def test_draft_sends_the_print_settings_of_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, run = _root(tmp_path)
    with (run / "run.yaml").open("a", encoding="utf-8") as f:
        f.write("print_settings:\n  compactMode: true\n")
    fake = FakeRenderer()
    _use(monkeypatch, fake)
    assert main(["render", str(run), "--draft", "--repo", str(root)]) == 0
    capsys.readouterr()
    assert fake.calls[0][1] == {"pageSize": "LETTER", "compactMode": True}
    shutil.rmtree(fake.calls[0][2].parent)
