"""Tests of `facts doctor` with a synthetic data root and fake tools.

No test makes a network call (the autouse fixture of conftest.py) or starts a
real program, except the tests that use a throw-away key in `tmp_path` with
the real ssh-keygen.
"""

from __future__ import annotations

import base64
import builtins
import io
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from darzi import __version__, cli, doctor
from darzi.doctor import (
    FAIL,
    OK,
    RELEASE_URL,
    WARN,
    Check,
    ProgramFailed,
    Tools,
    key_fingerprint,
    newest_tag,
    parse_allowed_signers,
    run_doctor,
    run_program,
)
from darzi.render import pages
from darzi.render.port import RenderError
from darzi.runs.signing import SSH_KEYGEN

from .conftest import DATA

BLOB = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519 synthetic key").decode()
FP = key_fingerprint(BLOB) or ""
OTHER_BLOB = base64.b64encode(b"another synthetic key").decode()
OTHER_FP = key_fingerprint(OTHER_BLOB) or ""
COMMIT = "950b700" + "a" * 33
NEWER = "1234567" + "b" * 33
NAMES = [
    "versions",
    "data root",
    "facts.toml",
    "approval settings",
    "allowed_signers",
    "ssh-keygen",
    "key file",
    "key fingerprint",
    "key passphrase",
    "ssh-agent",
    "signer history",
    "renderer settings",
    "renderer commit",
    "release tags",
    "renderer records",
    "renderer backend",
    "renderer frontend",
    "LibreOffice",
    "form settings",
]

Answer = tuple[int, str, str] | Exception


def facts_toml(key_file: Path, clone: Path, **change: str) -> str:
    values = {
        "allowed_signers": "allowed_signers",
        "key": str(key_file),
        "path": str(clone),
        "commit": "950b700",
        "data_dir": ".rm-data",
        "frontend_url": "http://localhost:3000",
        "backend_url": "http://127.0.0.1:8000",
        "form": '[form]\nblocked_hosts = ["jobs.example.com"]\n',
    }
    values.update(change)
    commit = f'commit = "{values["commit"]}"\n' if values["commit"] else ""
    return (
        '[owner]\nname = "Test Person"\n\n'
        '[approval]\nsigner = "owner"\nnamespace = "darzi-approval"\n'
        f'allowed_signers = "{values["allowed_signers"]}"\nkey = "{values["key"]}"\n\n'
        '[render]\nadapter = "resume-matcher"\n\n[render.resume_matcher]\n'
        f'path = "{values["path"]}"\n{commit}data_dir = "{values["data_dir"]}"\n'
        f'frontend_url = "{values["frontend_url"]}"\n'
        f'backend_url = "{values["backend_url"]}"\n\n{values["form"]}'
    )


class Setup:
    """A synthetic data root, a key file, a clone and fake tools."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "root"
        shutil.copytree(DATA, self.root)
        (self.root / ".rm-data").mkdir()
        self.database = self.root / ".rm-data" / doctor.RM_DATABASE
        self.database.write_bytes(b"")
        self.key = tmp_path / "keys" / "approval_key"
        self.key.parent.mkdir()
        self.key.write_text("not a real key\n", encoding="utf-8")
        self.key.chmod(0o600)
        self.clone = tmp_path / "rm"
        (self.clone / "apps" / "backend").mkdir(parents=True)
        (self.clone / ".git").mkdir()
        (self.clone / ".git" / "HEAD").write_text(COMMIT + "\n", encoding="utf-8")
        self.ssh_add = tmp_path / "bin" / "ssh-add"
        self.ssh_add.parent.mkdir()
        self.ssh_add.write_text("", encoding="utf-8")
        self.signers(f'owner namespaces="darzi-approval" ssh-ed25519 {BLOB} test\n')
        self.toml()
        self.calls: list[tuple[list[str], float]] = []
        self.requests: list[httpx.Request] = []
        self.answers: dict[str, Answer] = {
            "version": (0, "", "OpenSSH_9.9p1, LibreSSL 3.3.6\n"),
            "fingerprint": (0, f"256 {FP} test (ED25519)\n", ""),
            "passphrase": (
                255,
                "",
                "Load key: incorrect passphrase supplied to decrypt private key\n",
            ),
            "agent": (1, "The agent has no identities.\n", ""),
            "records": (0, '{"resumes": []}', ""),
        }
        self.tags: list[dict[str, Any]] = [
            {"name": "v1.1.0", "commit": {"sha": COMMIT}},
            {"name": "v1.0.9", "commit": {"sha": NEWER}},
            {"name": "nightly", "commit": {"sha": NEWER}},
        ]
        self.health = httpx.Response(200, json={"status": "healthy"})
        self.addresses = ["127.0.0.1", "::1"]
        self.soffice: Path | None = Path("/fake/soffice")

    def signers(self, text: str) -> None:
        (self.root / "allowed_signers").write_text(text, encoding="utf-8")

    def toml(self, **change: str) -> None:
        text = facts_toml(self.key, self.clone, **change)
        (self.root / "facts.toml").write_text(text, encoding="utf-8")

    def run(self, args: Sequence[str], timeout: float) -> tuple[int, str, str]:
        argv = list(args)
        self.calls.append((argv, timeout))
        if argv[0] == doctor.SSH:
            kind = "version"
        elif argv[0] == SSH_KEYGEN and argv[1] == "-l":
            kind = "fingerprint"
        elif argv[0] == SSH_KEYGEN and argv[1] == "-y":
            kind = "passphrase"
        elif argv[0] == doctor.SSH_ADD:
            kind = "agent"
        else:
            kind = "records"
        answer = self.answers[kind]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=self.tags)
        return self.health

    def resolve(self, host: str) -> list[str]:
        return self.addresses

    def tools(self, run: Callable[[Sequence[str], float], Any] | None = None) -> Tools:
        return Tools(
            run=run or self.run,
            transport=httpx.MockTransport(self.handle),
            resolver=self.resolve,
            find_soffice=lambda: self.soffice,
        )

    def doctor(self, offline: bool = False) -> dict[str, Check]:
        checks = run_doctor(self.root, offline, self.tools())
        assert [c.name for c in checks] == NAMES
        return {c.name: c for c in checks}


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
    made = Setup(tmp_path)
    monkeypatch.setattr(doctor, "SSH_ADD", str(made.ssh_add))
    return made


def statuses(checks: dict[str, Check]) -> dict[str, str]:
    return {name: c.status for name, c in checks.items()}


# --- The whole report -------------------------------------------------------------


def test_each_check_is_ok_on_a_good_set_up(setup: Setup) -> None:
    checks = setup.doctor()
    assert set(statuses(checks).values()) == {OK}, checks
    assert FP in checks["key fingerprint"].detail
    assert "has a passphrase" in checks["key passphrase"].detail


def test_the_text_output_and_exit_code_0(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli._doctor(setup.root, False, False, setup.tools()) == 0  # pyright: ignore[reportPrivateUsage]
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(NAMES) + 1
    assert lines[0].startswith(f"ok   versions: darzi {__version__}, Python ")
    assert lines[-1] == "facts doctor: 19 checks, 0 WARN, 0 FAIL."


def test_a_fail_gives_exit_code_1_and_json(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.soffice = None
    assert cli._doctor(setup.root, False, True, setup.tools()) == 1  # pyright: ignore[reportPrivateUsage]
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1 and data["command"] == "doctor"
    assert data["ok"] is False
    [office] = [c for c in data["checks"] if c["name"] == "LibreOffice"]
    assert office == {
        "name": "LibreOffice",
        "status": FAIL,
        "ok": False,
        "detail": pages.INSTALL_HINT,
    }


def test_a_warning_keeps_exit_code_0(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli._doctor(setup.root, True, False, setup.tools()) == 0  # pyright: ignore[reportPrivateUsage]
    out = capsys.readouterr().out
    assert "WARN release tags: not checked: --offline" in out
    assert "facts doctor: 19 checks, 1 WARN, 0 FAIL." in out


def test_no_data_root_is_invalid_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["doctor", "--repo", str(tmp_path)]) == 2
    assert "No master.yaml found" in capsys.readouterr().err


def test_a_facts_toml_that_is_not_valid_is_invalid_input(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    (setup.root / "facts.toml").write_text("[owner\n", encoding="utf-8")
    assert cli._doctor(setup.root, True, False, setup.tools()) == 2  # pyright: ignore[reportPrivateUsage]
    assert capsys.readouterr().out == ""


def test_a_failed_check_does_not_stop_the_others(setup: Setup) -> None:
    setup.key.unlink()
    setup.answers["records"] = ProgramFailed("no uv")
    checks = setup.doctor()
    assert checks["key file"].status == FAIL
    assert checks["renderer records"].status == FAIL
    assert checks["LibreOffice"].status == OK
    assert checks["form settings"].status == OK
    assert checks["renderer backend"].status == OK


def test_each_line_goes_through_the_line_function(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.answers["fingerprint"] = ProgramFailed("bad\nline\x1b[31m\u2028end")
    cli._doctor(setup.root, True, False, setup.tools())  # pyright: ignore[reportPrivateUsage]
    out = capsys.readouterr().out
    [line] = [x for x in out.splitlines() if " key fingerprint:" in x]
    assert line == "FAIL key fingerprint: bad?line?[31m?end"
    assert len(out.splitlines()) == len(NAMES) + 1


# --- Hard rule 5: no signature, no key read, no prompt ---------------------------


def test_each_program_call_cannot_prompt(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def spy(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        argv = list(args)
        return subprocess.CompletedProcess(argv, *setup.run(argv, kwargs["timeout"]))

    monkeypatch.setattr(doctor.subprocess, "run", spy)
    checks = run_doctor(setup.root, False, setup.tools(run=run_program))
    assert {c.status for c in checks} == {OK}
    programs = [args[0] for args, _ in calls]
    assert programs == [
        doctor.SSH,
        SSH_KEYGEN,
        SSH_KEYGEN,
        str(setup.ssh_add),
        "/usr/bin/env",
    ]
    for args, kwargs in calls:
        assert isinstance(args, list) and os.path.isabs(args[0])
        assert "-Y" not in args
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["start_new_session"] is True
        assert 0 < kwargs["timeout"] <= 120
        assert "shell" not in kwargs and "env" not in kwargs
        assert "input" not in kwargs
    # The Resume Matcher CLI gets only the list call, with its JSON as an argument.
    assert calls[-1][0][-2:] == ["list_resumes", '{"include_tailored": true}']


def test_run_program_refuses_a_relative_path() -> None:
    with pytest.raises(ProgramFailed, match="absolute"):
        run_program(["ssh-keygen", "-l"], 1)


def test_run_program_turns_a_timeout_into_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow(args: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(doctor.subprocess, "run", slow)
    with pytest.raises(ProgramFailed, match="no answer in 2 s"):
        run_program([SSH_KEYGEN, "-l"], 2)


def test_doctor_never_opens_the_key_file(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    real_open = builtins.open
    real_os_open = os.open

    def spy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def spy_os_open(path: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(str(path))
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(io, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    checks = setup.doctor()
    assert checks["key file"].status == OK
    assert opened, "the spy saw no file open"
    assert not [p for p in opened if Path(p).name == setup.key.name]


def test_the_agent_check_gets_the_environment_unchanged(setup: Setup) -> None:
    setup.doctor()
    [agent] = [args for args, _ in setup.calls if args[0] == str(setup.ssh_add)]
    assert agent == [str(setup.ssh_add), "-l"]


# --- The approval key ---------------------------------------------------------------


def test_bad_approval_settings_fail(setup: Setup) -> None:
    setup.toml(allowed_signers="../outside")
    checks = setup.doctor()
    assert checks["approval settings"].status == FAIL
    assert checks["allowed_signers"].status == FAIL


def test_no_signer_entry_fails(setup: Setup) -> None:
    setup.signers(f"someone ssh-ed25519 {BLOB}\n")
    checks = setup.doctor()
    assert checks["allowed_signers"].status == FAIL
    assert checks["key fingerprint"].status == FAIL


def test_two_keys_for_the_signer_warn(setup: Setup) -> None:
    setup.signers(
        f"owner ssh-ed25519 {BLOB}\n# a comment\nowner ssh-ed25519 {OTHER_BLOB}\n"
    )
    checks = setup.doctor()
    assert checks["allowed_signers"].status == WARN
    assert checks["key fingerprint"].status == OK


def test_a_line_for_another_namespace_warns(setup: Setup) -> None:
    setup.signers(
        f'owner namespaces="darzi-approval" ssh-ed25519 {BLOB}\n'
        f'owner namespaces="git,file" ssh-ed25519 {OTHER_BLOB}\n'
    )
    assert setup.doctor()["allowed_signers"].status == WARN


def test_parse_allowed_signers() -> None:
    [one, two] = parse_allowed_signers(
        f'a,b cert-authority,namespaces="x,y" ssh-ed25519 {BLOB} c\n'
        f"\n  # skip\n*  ssh-rsa not-base64!\n"
    )
    assert one.principals == ("a", "b") and one.namespaces == ("x", "y")
    assert one.fingerprint == FP and one.names("b") and not one.names("c")
    assert one.permits("y") and not one.permits("z")
    assert two.fingerprint is None and two.names("owner") and two.permits("z")


def test_a_missing_key_file_fails(setup: Setup) -> None:
    setup.key.unlink()
    checks = setup.doctor()
    assert checks["key file"].status == FAIL
    for name in ("key fingerprint", "key passphrase", "ssh-agent", "signer history"):
        assert checks[name].detail.startswith("not checked")
    assert not [a for a, _ in setup.calls if a[0] in (SSH_KEYGEN, str(setup.ssh_add))]


def test_a_key_that_others_can_read_fails(setup: Setup) -> None:
    setup.key.chmod(0o640)
    assert "chmod 600" in setup.doctor()["key file"].detail


def test_a_key_inside_the_data_root_fails(setup: Setup) -> None:
    inside = setup.root / "approval_key"
    shutil.copy(setup.key, inside)
    inside.chmod(0o600)
    setup.toml(key=str(inside))
    assert setup.doctor()["key file"].status == FAIL


def test_a_key_that_is_a_link_warns(setup: Setup, tmp_path: Path) -> None:
    link = tmp_path / "keys" / "link_key"
    link.symlink_to(setup.key)
    setup.toml(key=str(link))
    assert setup.doctor()["key file"].status == WARN


def test_a_key_that_is_not_in_allowed_signers_fails(setup: Setup) -> None:
    setup.answers["fingerprint"] = (0, f"256 {OTHER_FP} test (ED25519)\n", "")
    assert setup.doctor()["key fingerprint"].status == FAIL


def test_a_key_with_no_passphrase_fails(setup: Setup) -> None:
    setup.answers["passphrase"] = (0, "ssh-ed25519 AAAA public\n", "")
    check = setup.doctor()["key passphrase"]
    assert check.status == FAIL and "AAAA" not in check.detail


def test_an_unclear_passphrase_answer_warns(setup: Setup) -> None:
    setup.answers["passphrase"] = (255, "", "Load key: invalid format\n")
    assert setup.doctor()["key passphrase"].status == WARN


def test_a_key_loaded_in_the_agent_fails(setup: Setup) -> None:
    setup.answers["agent"] = (0, f"256 {OTHER_FP} a\n256 {FP} b (ED25519)\n", "")
    assert setup.doctor()["ssh-agent"].status == FAIL


def test_an_agent_without_the_key_is_ok(setup: Setup) -> None:
    setup.answers["agent"] = (0, f"256 {OTHER_FP} a (ED25519)\n", "")
    assert setup.doctor()["ssh-agent"].detail == "the approval key is not loaded"


def test_no_agent_is_ok(setup: Setup) -> None:
    setup.answers["agent"] = (2, "", "Could not open a connection to your agent.\n")
    assert setup.doctor()["ssh-agent"] == Check("ssh-agent", OK, "no agent runs")


def test_no_ssh_add_is_ok(setup: Setup) -> None:
    setup.ssh_add.unlink()
    check = setup.doctor()["ssh-agent"]
    assert check.status == OK and "no agent check" in check.detail
    assert not [a for a, _ in setup.calls if a[0] == str(setup.ssh_add)]


def test_a_run_signed_by_another_key_warns(setup: Setup) -> None:
    runs = setup.root / "applications"
    for name, fp in (("2026-09-14-globex-ux-designer", OTHER_FP), ("x", FP)):
        (runs / name).mkdir(exist_ok=True)
        manifest = {"signed": True, "signer_fingerprint": fp}
        (runs / name / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    link = runs / "2026-10-02-initech-design-technologist" / "manifest.json"
    link.symlink_to(runs / "2026-09-14-globex-ux-designer" / "manifest.json")
    check = setup.doctor()["signer history"]
    assert check.status == WARN
    assert check.detail.startswith("1 signed run(s) name a different key")


def test_ssh_keygen_absent_fails(setup: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "SSH_KEYGEN", "/nonexistent/ssh-keygen")
    assert setup.doctor()["ssh-keygen"].status == FAIL


# --- The real ssh-keygen with a throw-away key --------------------------------------


def _real_key(folder: Path, passphrase: str) -> tuple[Path, str]:
    folder.mkdir()
    key = folder / "approval_key"
    subprocess.run(
        [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", passphrase, "-f", str(key)],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    public = key.with_name("approval_key.pub").read_text(encoding="utf-8").split()
    key.with_name("approval_key.pub").unlink()  # doctor needs no .pub file
    return key, f'owner namespaces="darzi-approval" {public[0]} {public[1]}\n'


@pytest.mark.parametrize(("passphrase", "status"), [("test pass 1", OK), ("", FAIL)])
def test_the_real_ssh_keygen(
    setup: Setup, tmp_path: Path, passphrase: str, status: str
) -> None:
    key, line = _real_key(tmp_path / "real", passphrase)
    setup.signers(line)
    setup.toml(key=str(key))

    def real_keygen(args: Sequence[str], timeout: float) -> tuple[int, str, str]:
        if args[0] == SSH_KEYGEN:
            return run_program(args, timeout)
        return setup.run(args, timeout)

    checks = {c.name: c for c in run_doctor(setup.root, True, setup.tools(real_keygen))}
    assert checks["allowed_signers"].status == OK
    assert checks["key fingerprint"].status == OK
    assert checks["key passphrase"].status == status


# --- The renderer -------------------------------------------------------------------


def test_bad_renderer_settings_fail_and_skip_the_renderer_checks(
    setup: Setup,
) -> None:
    setup.toml(frontend_url="http://resume-matcher.invalid:3000")
    checks = setup.doctor()
    assert checks["renderer settings"].status == FAIL
    for name in ("renderer commit", "renderer records", "renderer backend"):
        assert checks[name].detail.startswith("not checked")
    assert [r.url.host for r in setup.requests] == ["api.github.com"]


def test_a_clone_at_another_commit_fails(setup: Setup) -> None:
    (setup.clone / ".git" / "HEAD").write_text(NEWER + "\n", encoding="utf-8")
    assert setup.doctor()["renderer commit"].status == FAIL


def test_no_pinned_commit_warns(setup: Setup) -> None:
    setup.toml(commit="")
    checks = setup.doctor()
    assert checks["renderer commit"].status == WARN
    assert checks["release tags"].detail == "not checked: facts.toml pins no commit"
    assert setup.requests and "api.github.com" not in [
        r.url.host for r in setup.requests
    ]


def test_a_record_that_darzi_did_not_make_fails(setup: Setup) -> None:
    record = {"resume_id": "r1", "title": "Mine", "is_master": True}
    setup.answers["records"] = (0, json.dumps({"resumes": [record]}), "")
    assert setup.doctor()["renderer records"].status == FAIL


def test_the_scratch_record_is_ok(setup: Setup) -> None:
    record = {"resume_id": "r1", "title": "darzi scratch", "is_master": True}
    setup.answers["records"] = (0, json.dumps({"resumes": [record]}), "")
    assert "only the darzi scratch" in setup.doctor()["renderer records"].detail


def test_no_data_folder_starts_no_cli(setup: Setup) -> None:
    setup.database.unlink()
    (setup.root / ".rm-data").rmdir()
    assert setup.doctor()["renderer records"].status == OK
    assert not [a for a, _ in setup.calls if a[0] == "/usr/bin/env"]
    assert not (setup.root / ".rm-data").exists()


def test_a_data_folder_with_no_database_starts_no_cli(setup: Setup) -> None:
    setup.database.unlink()
    check = setup.doctor()["renderer records"]
    assert check.status == OK
    assert f"holds no {doctor.RM_DATABASE} yet; the first render makes it" in (
        check.detail
    )
    assert not [a for a, _ in setup.calls if a[0] == "/usr/bin/env"]
    assert list((setup.root / ".rm-data").iterdir()) == []


def test_deeply_nested_json_of_the_list_call_is_one_fail(setup: Setup) -> None:
    setup.answers["records"] = (0, "[" * 200_000, "")
    checks = setup.doctor()
    assert checks["renderer records"].status == FAIL
    assert "nested too deeply" in checks["renderer records"].detail
    assert checks["LibreOffice"].status == OK
    assert checks["form settings"].status == OK


def test_a_deeply_nested_manifest_is_skipped(setup: Setup) -> None:
    run = setup.root / "applications" / "2026-01-02-acme-test"
    run.mkdir(parents=True, exist_ok=True)
    (run / "manifest.json").write_text("[" * 200_000, encoding="utf-8")
    checks = setup.doctor()
    assert checks["signer history"].status == OK
    assert set(statuses(checks).values()) == {OK}


def test_an_unexpected_exception_is_one_fail_line(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken() -> Path | None:
        raise RuntimeError("synthetic failure")

    tools = setup.tools()
    tools = Tools(tools.run, tools.transport, tools.resolver, broken)
    assert cli._doctor(setup.root, False, False, tools) == 1  # pyright: ignore[reportPrivateUsage]
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(NAMES) + 1
    [office] = [line for line in lines if " LibreOffice: " in line]
    assert office.startswith("FAIL LibreOffice: the check stopped with RuntimeError.")
    assert any(line.startswith("ok   form settings:") for line in lines)
    assert lines[-1] == "facts doctor: 19 checks, 0 WARN, 1 FAIL."


def test_the_list_runner_refuses_each_other_tool(setup: Setup) -> None:
    runner = doctor._list_runner(setup.tools())  # pyright: ignore[reportPrivateUsage]
    for tool in ("create_master_resume", "update_resume", "export_resume_pdf"):
        with pytest.raises(RenderError, match="only list_resumes"):
            runner(["/usr/bin/env", "resume-matcher", tool, "-"], "{}")
    assert setup.calls == []


def test_a_host_that_is_not_loopback_gets_no_request(setup: Setup) -> None:
    setup.addresses = ["127.0.0.1", "192.0.2.10"]
    checks = setup.doctor(offline=True)
    assert checks["renderer backend"].status == FAIL
    assert "not a loopback address" in checks["renderer frontend"].detail
    assert setup.requests == []


def test_an_unhealthy_renderer_fails(setup: Setup) -> None:
    setup.health = httpx.Response(503, text="starting")
    checks = setup.doctor(offline=True)
    assert checks["renderer backend"].status == FAIL
    assert checks["renderer frontend"].status == FAIL


def test_a_renderer_that_does_not_answer_fails(setup: Setup) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    tools = Tools(
        run=setup.run,
        transport=httpx.MockTransport(refuse),
        resolver=setup.resolve,
        find_soffice=lambda: setup.soffice,
    )
    checks = {c.name: c for c in run_doctor(setup.root, True, tools)}
    assert "Start Resume Matcher" in checks["renderer backend"].detail


def test_the_health_requests(setup: Setup) -> None:
    setup.doctor(offline=True)
    assert [str(r.url) for r in setup.requests] == [
        "http://127.0.0.1:8000/api/v1/health",
        "http://localhost:3000/api/v1/health",
    ]
    for request in setup.requests:
        assert request.method == "GET" and request.content == b""
        assert "cookie" not in request.headers


# --- The release lookup --------------------------------------------------------------


def test_the_one_release_request(setup: Setup) -> None:
    check = setup.doctor()["release tags"]
    assert check == Check(
        "release tags", OK, "the pin 950b700 is the newest release v1.1.0"
    )
    [request] = [r for r in setup.requests if r.url.host == "api.github.com"]
    assert str(request.url) == RELEASE_URL
    assert RELEASE_URL.startswith("https://api.github.com/repos/")
    assert request.method == "GET" and request.content == b""
    assert request.headers["User-Agent"] == f"darzi/{__version__}"
    for name in ("authorization", "cookie"):
        assert name not in request.headers


def test_a_newer_release_warns(setup: Setup) -> None:
    setup.tags.append({"name": "v1.2.0", "commit": {"sha": NEWER}})
    check = setup.doctor()["release tags"]
    assert check.status == WARN and "v1.2.0 (1234567)" in check.detail


def test_offline_sends_no_request(setup: Setup) -> None:
    setup.doctor(offline=True)
    assert "api.github.com" not in [r.url.host for r in setup.requests]


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ConnectError("no network"),
        httpx.Response(403, json={"message": "rate limit"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, content=b"[" * 200_000),
        httpx.Response(200, content=b"[" + b" " * (2 * 1024 * 1024) + b"]"),
        httpx.Response(301, headers={"Location": "https://example.com/"}),
    ],
)
def test_a_failed_release_lookup_only_warns(
    setup: Setup, answer: httpx.Response | Exception
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            setup.requests.append(request)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return setup.handle(request)

    tools = Tools(
        run=setup.run,
        transport=httpx.MockTransport(handle),
        resolver=setup.resolve,
        find_soffice=lambda: setup.soffice,
    )
    checks = run_doctor(setup.root, False, tools)
    by_name = {c.name: c for c in checks}
    assert by_name["release tags"].status == WARN
    assert by_name["release tags"].detail.startswith("not checked")
    assert [c.status for c in checks].count(OK) == len(NAMES) - 1
    assert len([r for r in setup.requests if r.url.host == "api.github.com"]) == 1


def test_the_clients_ignore_proxy_settings_and_redirects() -> None:
    client = doctor._client(Tools(), doctor.RELEASE_TIMEOUT)  # pyright: ignore[reportPrivateUsage]
    with client:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client.timeout.read == 5.0
        assert not client.cookies


def test_newest_tag() -> None:
    names = ["v1.9.0", "v1.10.0", "2.0.0-rc1", "latest", "v1.10.0b", "0.9.9"]
    assert newest_tag(names) == "v1.10.0"
    assert newest_tag(["latest", "nightly"]) is None
    assert newest_tag([]) is None


# --- LibreOffice and the form -------------------------------------------------------


def test_libreoffice_not_found_fails_with_the_install_hint(setup: Setup) -> None:
    setup.soffice = None
    assert setup.doctor()["LibreOffice"] == Check(
        "LibreOffice", FAIL, pages.INSTALL_HINT
    )


def test_no_form_table_fails(setup: Setup) -> None:
    setup.toml(form="")
    assert setup.doctor()["form settings"].status == FAIL


def test_an_empty_blocked_hosts_list_warns(setup: Setup) -> None:
    setup.toml(form="[form]\nblocked_hosts = []\n")
    assert setup.doctor()["form settings"].status == WARN


def test_the_synthetic_data_root_runs_offline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # tests/data has a key path that does not exist and .invalid renderer URLs.
    tools = Tools(run=lambda a, t: (0, "", ""), find_soffice=lambda: None)
    assert cli._doctor(DATA, True, False, tools) == 1  # pyright: ignore[reportPrivateUsage]
    out = capsys.readouterr().out
    assert "FAIL key file: no key at /nonexistent/darzi-test/approval_key" in out
    assert "FAIL renderer settings:" in out
