"""Tests of the Resume Matcher adapter with a fake CLI (no Resume Matcher)."""

from __future__ import annotations

import fcntl
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from darzi.config import RenderSettings, ResumeMatcherSettings, load_config
from darzi.facts.repo import InputError
from darzi.render.port import RenderError, RenderResult
from darzi.render.resume_matcher import (
    LOCK_FILE,
    SCRATCH_TITLE,
    TIMEOUT_SECONDS,
    ResumeMatcher,
    check_settings,
    loopback_url,
    run_command,
)

from .conftest import DATA
from .render_fakes import FakeCli, fake_install, fake_renderer

RESUME: dict[str, Any] = {
    "personalInfo": {"name": "Test Person", "email": "test.person@example.com"},
    "summary": "Designer.",
}
LETTER = {"pageSize": "LETTER"}


# --- The command and the environment -------------------------------------------


def test_command_is_one_argument_list_with_env_and_no_sync(tmp_path: Path) -> None:
    rm = fake_renderer(tmp_path, FakeCli())
    backend = str(tmp_path / "rm" / "apps" / "backend")
    assert rm.command("get_status") == [
        "/usr/bin/env",
        "-u",
        "VIRTUAL_ENV",
        f"DATA_DIR={tmp_path / 'rm-data'}",
        "FRONTEND_BASE_URL=http://127.0.0.1:3100",
        "DATABASE_URL=",
        "uv",
        "run",
        "--no-sync",
        "--directory",
        backend,
        "resume-matcher",
        "get_status",
        "-",
    ]


def test_environment_sets_data_dir_frontend_and_an_empty_database_url(
    tmp_path: Path,
) -> None:
    rm = fake_renderer(tmp_path, FakeCli())
    assert rm.environment() == [
        f"DATA_DIR={tmp_path / 'rm-data'}",
        "FRONTEND_BASE_URL=http://127.0.0.1:3100",
        "DATABASE_URL=",
    ]


def test_run_command_uses_no_shell_no_env_and_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert run_command(["/usr/bin/env", "uv"], '{"a": 1}') == (0, "{}", "")
    assert seen["cmd"] == ["/usr/bin/env", "uv"]
    kwargs = seen["kwargs"]
    assert kwargs["input"] == '{"a": 1}'
    assert kwargs["timeout"] == TIMEOUT_SECONDS == 120
    assert "env" not in kwargs and "shell" not in kwargs
    assert kwargs["capture_output"] is True and kwargs["text"] is True


@pytest.mark.parametrize(
    ("error", "words"),
    [
        (FileNotFoundError(2, "No such file or directory"), "cannot start"),
        (subprocess.TimeoutExpired(["uv"], 120), "no answer in 120 s"),
        (PermissionError(13, "Permission denied"), "cannot start"),
    ],
)
def test_run_command_errors_are_render_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception, words: str
) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RenderError, match=words):
        run_command(["/usr/bin/env"], "{}")


# --- call() ----------------------------------------------------------------------


def test_call_sends_json_on_stdin_and_reads_json(tmp_path: Path) -> None:
    cli = FakeCli()
    rm = fake_renderer(tmp_path, cli)
    assert rm.call("list_resumes", {"include_tailored": True}) == {"resumes": []}
    assert cli.calls == [("list_resumes", {"include_tailored": True})]


@pytest.mark.parametrize(
    ("answer", "words"),
    [
        ((1, "", "first\nError executing tool x: bad id\n"), "failed: Error executing"),
        ((1, "", ""), "failed: exit code 1"),
        ((0, "not json", ""), "not JSON"),
        ((0, "[1, 2]", ""), "no JSON object"),
    ],
)
def test_call_errors(tmp_path: Path, answer: tuple[int, str, str], words: str) -> None:
    rm = ResumeMatcher(fake_install(tmp_path), runner=lambda cmd, stdin: answer)
    with pytest.raises(RenderError, match=words):
        rm.call("get_status")


# --- The scratch record ----------------------------------------------------------


def test_first_render_creates_the_scratch_record_then_updates_it(
    tmp_path: Path,
) -> None:
    cli = FakeCli()
    rm = fake_renderer(tmp_path, cli)
    first = rm.render(RESUME, LETTER, tmp_path / "out" / "a.pdf")
    assert first == RenderResult(pages=1, template="swiss-single", page_size="LETTER")
    assert cli.tools() == ["list_resumes", "create_master_resume", "export_resume_pdf"]
    assert cli.calls[1][1] == {"resume_data": RESUME, "title": SCRATCH_TITLE}
    changed = {**RESUME, "summary": "Researcher."}
    cli.calls.clear()
    rm.render(changed, {**LETTER, "compactMode": True}, tmp_path / "out" / "b.pdf")
    assert cli.tools() == ["list_resumes", "update_resume", "export_resume_pdf"]
    assert cli.calls[1][1] == {"resume_id": "id-1", "resume_data": changed}
    assert cli.calls[2][1] == {
        "resume_id": "id-1",
        "output_path": str((tmp_path / "out" / "b.pdf").resolve()),
        "print_settings": {"pageSize": "LETTER", "compactMode": True},
    }
    assert len(cli.records) == 1 and cli.records[0]["data"] == changed
    assert (tmp_path / "rm-data" / LOCK_FILE).is_file()


@pytest.mark.parametrize(
    "records",
    [
        [("Test Person Master Resume", True)],
        [(SCRATCH_TITLE, True), ("Other", True)],
        [(SCRATCH_TITLE, False)],
        [(SCRATCH_TITLE, True), ("tailored copy", False)],
    ],
)
def test_another_record_stops_the_render_before_a_write(
    tmp_path: Path, records: list[tuple[str, bool]]
) -> None:
    cli = FakeCli()
    for title, master in records:
        cli.add(title, master)
    rm = fake_renderer(tmp_path, cli)
    with pytest.raises(InputError, match="did not make") as exc:
        rm.render(RESUME, LETTER, tmp_path / "a.pdf")
    assert not isinstance(exc.value, RenderError)
    assert cli.tools() == ["list_resumes"]
    assert not (tmp_path / "a.pdf").exists()


def test_two_scratch_records_stop_the_render_before_a_write(tmp_path: Path) -> None:
    cli = FakeCli()
    cli.add(SCRATCH_TITLE)
    cli.add(SCRATCH_TITLE)
    rm = fake_renderer(tmp_path, cli)
    with pytest.raises(InputError, match="2 records with the title") as exc:
        rm.render(RESUME, LETTER, tmp_path / "a.pdf")
    assert not isinstance(exc.value, RenderError)
    assert cli.tools() == ["list_resumes"]


def test_a_list_that_is_not_a_list_is_a_render_error(tmp_path: Path) -> None:
    rm = ResumeMatcher(
        fake_install(tmp_path), runner=lambda cmd, stdin: (0, '{"resumes": 3}', "")
    )
    with pytest.raises(RenderError, match="no list"):
        rm.render(RESUME, LETTER, tmp_path / "a.pdf")


def test_the_limit_error_is_a_render_error(tmp_path: Path) -> None:
    cli = FakeCli(fail={"create_master_resume": "Master resume limit reached (5)."})
    with pytest.raises(RenderError, match="limit reached"):
        fake_renderer(tmp_path, cli).render(RESUME, LETTER, tmp_path / "a.pdf")


def test_each_write_names_only_the_scratch_record(tmp_path: Path) -> None:
    cli = FakeCli()
    scratch = cli.add(SCRATCH_TITLE)
    rm = fake_renderer(tmp_path, cli)
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        rm.render(RESUME, LETTER, tmp_path / name)
    ids = {args["resume_id"] for _, args in cli.calls if "resume_id" in args}
    assert ids == {scratch}
    assert "create_master_resume" not in cli.tools()


# --- The lock --------------------------------------------------------------------


def test_a_held_lock_stops_a_second_render(tmp_path: Path) -> None:
    cli = FakeCli()
    rm = fake_renderer(tmp_path, cli)
    rm.config.data_dir.mkdir()
    with (rm.config.data_dir / LOCK_FILE).open("a") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        with pytest.raises(RenderError, match="another render"):
            rm.render(RESUME, LETTER, tmp_path / "a.pdf")
    assert cli.calls == []
    rm.render(RESUME, LETTER, tmp_path / "a.pdf")
    assert cli.tools()[-1] == "export_resume_pdf"


def test_a_data_folder_that_cannot_be_made_is_an_input_error(tmp_path: Path) -> None:
    config = fake_install(tmp_path)
    (tmp_path / "file").write_text("x", encoding="utf-8")
    bad = ResumeMatcher(
        type(config)(config.path, tmp_path / "file" / "data", "", ""), runner=FakeCli()
    )
    with pytest.raises(InputError, match="cannot use the data folder"):
        bad.render(RESUME, LETTER, tmp_path / "a.pdf")


def test_the_output_must_be_a_pdf_path(tmp_path: Path) -> None:
    cli = FakeCli()
    with pytest.raises(InputError, match="does not end with .pdf"):
        fake_renderer(tmp_path, cli).render(RESUME, LETTER, tmp_path / "a.txt")
    assert cli.calls == []


# --- The answer of export_resume_pdf ---------------------------------------------


@pytest.mark.parametrize("pages", [0, -1, "2", True, None, 1.5])
def test_a_page_count_that_is_not_a_positive_integer_fails(
    tmp_path: Path, pages: Any
) -> None:
    cli = FakeCli(pages=pages)
    with pytest.raises(RenderError, match="page count"):
        fake_renderer(tmp_path, cli).render(RESUME, LETTER, tmp_path / "a.pdf")


def test_the_page_count_comes_from_the_tool(tmp_path: Path) -> None:
    result = fake_renderer(tmp_path, FakeCli(pages=3)).render(
        RESUME, LETTER, tmp_path / "a.pdf"
    )
    assert result.pages == 3


def test_no_file_fails(tmp_path: Path) -> None:
    cli = FakeCli(write_pdf=None)
    with pytest.raises(RenderError, match="wrote no file"):
        fake_renderer(tmp_path, cli).render(RESUME, LETTER, tmp_path / "a.pdf")


def test_a_file_that_is_not_a_pdf_fails(tmp_path: Path) -> None:
    cli = FakeCli(write_pdf=b"<html>")
    with pytest.raises(RenderError, match="not a PDF"):
        fake_renderer(tmp_path, cli).render(RESUME, LETTER, tmp_path / "a.pdf")


# --- The settings guards ---------------------------------------------------------


def _settings(**values: str | None) -> RenderSettings:
    base: dict[str, str | None] = {
        "path": "rm",
        "data_dir": "rm-data",
        "frontend_url": "http://localhost:3100",
        "backend_url": "http://127.0.0.1:8100",
    }
    base.update(values)
    return RenderSettings(
        adapter="resume-matcher", resume_matcher=ResumeMatcherSettings(**base)
    )


def test_good_settings_resolve_from_the_data_root(tmp_path: Path) -> None:
    (tmp_path / "rm" / "apps" / "backend").mkdir(parents=True)
    config = check_settings(tmp_path, _settings(frontend_url="http://[::1]:3100/"))
    assert config.path == (tmp_path / "rm").resolve()
    assert config.data_dir == (tmp_path / "rm-data").resolve()
    assert config.frontend_url == "http://[::1]:3100"
    assert config.backend_url == "http://127.0.0.1:8100"


def test_no_render_section_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InputError, match=r"no \[render\] section"):
        check_settings(tmp_path, None)


@pytest.mark.parametrize("adapter", [None, "other"])
def test_another_adapter_is_refused(tmp_path: Path, adapter: str | None) -> None:
    settings = RenderSettings(adapter=adapter)
    with pytest.raises(InputError, match="adapter must be 'resume-matcher'"):
        check_settings(tmp_path, settings)


@pytest.mark.parametrize("key", ["path", "data_dir", "frontend_url", "backend_url"])
def test_each_required_key_is_refused_when_absent(tmp_path: Path, key: str) -> None:
    (tmp_path / "rm" / "apps" / "backend").mkdir(parents=True)
    with pytest.raises(InputError, match=f"no .* {key}. Add it"):
        check_settings(tmp_path, _settings(**{key: None}))


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:3100",
        "http://192.168.1.5:3100",
        "http://resume-matcher.invalid:3000",
        "http://localhost.example.com:3100",
        "http://user:secret@127.0.0.1:3100",
        "http://127.0.0.1:port",
        "ftp://localhost",
        "127.0.0.1:3100",
        "http://0.0.0.0:3100",
    ],
)
def test_a_url_that_is_not_loopback_http_is_refused(url: str) -> None:
    with pytest.raises(InputError, match="must be an http URL on localhost"):
        loopback_url("frontend_url", url)


@pytest.mark.parametrize("key", ["frontend_url", "backend_url"])
def test_both_urls_are_checked(tmp_path: Path, key: str) -> None:
    (tmp_path / "rm" / "apps" / "backend").mkdir(parents=True)
    with pytest.raises(InputError, match=key):
        check_settings(tmp_path, _settings(**{key: "http://example.com:3000"}))


def test_a_path_with_no_backend_folder_is_refused(tmp_path: Path) -> None:
    (tmp_path / "rm").mkdir()
    with pytest.raises(InputError, match="has no apps/backend folder"):
        check_settings(tmp_path, _settings())


@pytest.mark.parametrize("data_dir", ["rm", "rm/apps/backend/data", "rm/../rm/x"])
def test_a_data_dir_inside_the_install_is_refused(
    tmp_path: Path, data_dir: str
) -> None:
    (tmp_path / "rm" / "apps" / "backend").mkdir(parents=True)
    with pytest.raises(InputError, match="is inside the Resume Matcher folder"):
        check_settings(tmp_path, _settings(data_dir=data_dir))


def test_a_data_dir_link_into_the_install_is_refused(tmp_path: Path) -> None:
    (tmp_path / "rm" / "apps" / "backend" / "data").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "rm" / "apps" / "backend" / "data")
    with pytest.raises(InputError, match="is inside the Resume Matcher folder"):
        check_settings(tmp_path, _settings(data_dir="link"))


def test_an_install_link_that_holds_the_data_dir_is_refused(tmp_path: Path) -> None:
    (tmp_path / "real" / "apps" / "backend").mkdir(parents=True)
    (tmp_path / "rm").symlink_to(tmp_path / "real")
    with pytest.raises(InputError, match="is inside the Resume Matcher folder"):
        check_settings(tmp_path, _settings(path="rm", data_dir="real/data"))


def test_the_synthetic_facts_toml_stops_at_the_loopback_check() -> None:
    with pytest.raises(InputError, match="frontend_url .*resume-matcher.invalid"):
        check_settings(DATA, load_config(DATA).render)


# --- [render] in facts.toml ------------------------------------------------------


def _load(tmp_path: Path, text: str) -> Any:
    (tmp_path / "facts.toml").write_text(text, encoding="utf-8")
    return load_config(tmp_path).render


def test_no_render_section_gives_none(tmp_path: Path) -> None:
    assert _load(tmp_path, "[owner]\nname = 'Test Person'\n") is None


def test_a_render_section_without_the_table_gives_empty_settings(
    tmp_path: Path,
) -> None:
    assert _load(tmp_path, "[render]\nadapter = 'resume-matcher'\n") == (
        RenderSettings(adapter="resume-matcher")
    )


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("render = 3\n", "render must be a table"),
        ("[render]\nresume_matcher = 'x'\n", "render.resume_matcher must be a table"),
        ("[render]\nadapter = ''\n", r"\[render\] adapter must be a text"),
        ("[render.resume_matcher]\npath = 5\n", "path must be a text"),
        ("[render.resume_matcher]\ndata_dir = ' '\n", "data_dir must be a text"),
        ("[render.resume_matcher]\nfrontend_url = []\n", "frontend_url must be a"),
        ("[render.resume_matcher]\nnode = 24\n", "node must be a text"),
    ],
)
def test_bad_render_values_are_input_errors(
    tmp_path: Path, text: str, words: str
) -> None:
    with pytest.raises(InputError, match=words):
        _load(tmp_path, text)


def test_fake_cli_answers_are_json(tmp_path: Path) -> None:
    # The fake must give what the real CLI gives: one JSON object on stdout.
    cli = FakeCli()
    code, out, _ = cli(["x", "list_resumes", "-"], "{}")
    assert code == 0 and isinstance(json.loads(out), dict)
