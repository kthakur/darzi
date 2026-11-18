"""`facts doctor`: check the set-up of one data root (docs/SPEC.md section 6).

Each check gives one `Check` with the status OK, WARN or FAIL. A check that
fails does not stop the other checks. Only an absent data root or a
`facts.toml` that is not valid stops `doctor` (InputError, exit code 2).

Rules for each check (hard rule 5):

- `doctor` changes no file and never signs: no argument list holds `-Y`.
- It never reads the bytes of the private key. It only takes the file status
  with `lstat`; `ssh-keygen` reads the key file.
- It never prompts. Each program has an absolute path, a list of arguments,
  no shell, stdin from the null device, a new session (so that no terminal is
  there to ask on) and a time limit. See `run_program()`.
- The programs get the environment of this process unchanged. darzi reads no
  variable of it here: `ssh-add -l` finds the agent by itself.

Network (docs/SPEC.md section 6): the release lookup is one GET to the fixed
address RELEASE_URL; `--offline` skips it. The local check is one GET to the
backend and one to the frontend of Resume Matcher, only after the loopback
guard of `loopback_url()` passes and the host name resolves to loopback
addresses only. Each client ignores the proxy settings (`trust_env=False`),
follows no redirect, sends no body, no cookie and no token, and has a time
limit and a size limit. Tests give a mock transport and a fake resolver.
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import hashlib
import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from darzi import __version__
from darzi.config import Config, load_config
from darzi.facts.repo import InputError, find_repo
from darzi.paths import is_dir, is_file
from darzi.render import pages, resume_matcher
from darzi.render.resume_matcher import ResumeMatcher, ResumeMatcherConfig
from darzi.runs.runs import APPLICATIONS
from darzi.runs.signing import (
    SSH_KEYGEN,
    allowed_signers_path,
    key_path,
    settings_problem,
)

OK = "ok"
WARN = "WARN"
FAIL = "FAIL"

SSH = "/usr/bin/ssh"
SSH_ADD = "/usr/bin/ssh-add"
PROGRAM_TIMEOUT = 10.0
AGENT_TIMEOUT = 5.0

USER_AGENT = f"darzi/{__version__}"
# The public tag list of Resume Matcher at GitHub. A constant: it never comes
# from a file, a setting or the git settings of the clone.
RELEASE_URL = "https://api.github.com/repos/srbhr/Resume-Matcher/tags?per_page=100"
RELEASE_TIMEOUT = httpx.Timeout(5.0)
MAX_TAGS_BYTES = 1024 * 1024
HEALTH_PATH = "/api/v1/health"
HEALTH_TIMEOUT = httpx.Timeout(3.0)
MAX_HEALTH_BYTES = 64 * 1024

MAX_SIGNERS_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MANIFEST = "manifest.json"

SEMVER = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
SHA256_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]+")
PIN = re.compile(r"[0-9a-f]{7,64}")
FULL_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
KEY_TYPE_PREFIXES = ("ssh-", "ecdsa-", "sk-")


@dataclass(frozen=True)
class Check:
    """The result of one check: a name, OK, WARN or FAIL, and one detail line."""

    name: str
    status: str
    detail: str


class ProgramFailed(Exception):
    """A program could not start, or it did not end in its time limit."""


# A program runner takes the argument list and the time limit in seconds. It
# returns the exit code, stdout and stderr. Tests give a fake runner.
Program = Callable[[Sequence[str], float], tuple[int, str, str]]
Resolver = Callable[[str], list[str]]


def run_program(args: Sequence[str], timeout: float) -> tuple[int, str, str]:
    """Run one program so that it cannot prompt; return code, stdout, stderr.

    The program has an absolute path and a list of arguments, and runs with
    no shell. stdin is the null device. `start_new_session` gives the program
    a new session with no controlling terminal, so it has no terminal to ask
    on. Raises ProgramFailed when the program cannot start or takes longer
    than `timeout` seconds.
    """
    argv = list(args)
    if not argv or not os.path.isabs(argv[0]):
        raise ProgramFailed(f"{argv[:1]} is not an absolute program path")
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            start_new_session=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProgramFailed(f"{argv[0]} gave no answer in {timeout:g} s") from exc
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ProgramFailed(f"cannot start {argv[0]}: {exc}") from exc
    return done.returncode, done.stdout, done.stderr


def system_resolver(host: str) -> list[str]:
    """Return the addresses of `host` from the system name lookup."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


@dataclass(frozen=True)
class Tools:
    """The programs, the HTTP transport, the resolver and the LibreOffice search.

    Production uses the defaults. Tests give fakes, so that no test starts a
    real program or makes a network call by accident.
    """

    run: Program = run_program
    transport: httpx.BaseTransport | None = None
    resolver: Resolver = system_resolver
    find_soffice: Callable[[], Path | None] = pages.find_soffice


@dataclass
class _State:
    """What one check finds and a later check needs."""

    repo: Path
    config: Config
    signer_keys: set[str] = field(default_factory=lambda: set[str]())
    key: Path | None = None
    fingerprint: str | None = None
    renderer: ResumeMatcherConfig | None = None


def has_failure(checks: Iterable[Check]) -> bool:
    """Return True when one or more checks are FAIL."""
    return any(c.status == FAIL for c in checks)


def run_doctor(
    repo_arg: Path | None, offline: bool = False, tools: Tools | None = None
) -> list[Check]:
    """Run each check of `facts doctor` in order and return the results.

    Raises InputError when there is no data root or `facts.toml` is not valid.
    """
    tools = tools or Tools()
    repo = find_repo(repo_arg)
    config = load_config(repo)
    state = _State(repo, config)
    checks = [
        _versions(),
        Check("data root", OK, str(repo)),
        Check("facts.toml", OK, "valid"),
    ]
    steps: list[Callable[[_State, Tools], Check]] = [
        _approval_settings,
        _allowed_signers,
        _ssh_keygen,
        _key_file,
        _key_fingerprint,
        _key_passphrase,
        _agent,
        _signer_history,
        _renderer_settings,
        _renderer_commit,
        lambda s, t: _release(s, t, offline),
        _renderer_records,
        lambda s, t: _health(s, t, "backend", "backend_url"),
        lambda s, t: _health(s, t, "frontend", "frontend_url"),
        _libreoffice,
        _form,
    ]
    for step in steps:
        checks.append(step(state, tools))
    return checks


def _versions() -> Check:
    python = sys.version.split()[0]
    return Check("versions", OK, f"darzi {__version__}, Python {python}")


# --- The approval key ----------------------------------------------------------


def _approval_settings(state: _State, tools: Tools) -> Check:
    problem = settings_problem(state.repo, state.config.approval)
    if problem:
        return Check("approval settings", FAIL, f"{problem}. Correct facts.toml.")
    approval = state.config.approval
    return Check(
        "approval settings",
        OK,
        f"signer {approval.signer}, namespace {approval.namespace}",
    )


@dataclass(frozen=True)
class SignerEntry:
    """One line of `allowed_signers`: principals, namespaces and the key."""

    principals: tuple[str, ...]
    namespaces: tuple[str, ...] | None
    fingerprint: str | None

    def names(self, signer: str) -> bool:
        """Return True when a principal pattern of this line matches `signer`."""
        return any(fnmatch.fnmatchcase(signer, p) for p in self.principals)

    def permits(self, namespace: str) -> bool:
        """Return True when this line is valid for `namespace`."""
        if self.namespaces is None:
            return True
        return any(fnmatch.fnmatchcase(namespace, n) for n in self.namespaces)


def _split(text: str, separators: str) -> list[str]:
    """Split `text` at each separator that is not inside double quotes."""
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
            current.append(char)
        elif char in separators and not quoted:
            if current:
                parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    return parts


def key_fingerprint(blob: str) -> str | None:
    """Return the OpenSSH SHA-256 fingerprint of a base64 public key, or None."""
    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError):
        return None
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii")
    return "SHA256:" + digest.rstrip("=")


def parse_allowed_signers(text: str) -> list[SignerEntry]:
    """Return each entry of an `allowed_signers` text. Comments are skipped."""
    entries: list[SignerEntry] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = _split(line, " \t")
        principals = tuple(fields[0].split(","))
        rest = fields[1:]
        namespaces: tuple[str, ...] | None = None
        if rest and not rest[0].startswith(KEY_TYPE_PREFIXES):
            for option in _split(rest[0], ","):
                name, _, value = option.partition("=")
                if name.lower() == "namespaces":
                    namespaces = tuple(value.strip('"').split(","))
            rest = rest[1:]
        fingerprint = key_fingerprint(rest[1]) if len(rest) >= 2 else None
        entries.append(SignerEntry(principals, namespaces, fingerprint))
    return entries


def _allowed_signers(state: _State, tools: Tools) -> Check:
    name = "allowed_signers"
    approval = state.config.approval
    path = allowed_signers_path(state.repo, approval)
    if path is None or approval.signer is None or approval.namespace is None:
        return Check(
            name,
            FAIL,
            "no allowed_signers file inside the data root. Make it as the README "
            "says, and set [approval] allowed_signers in facts.toml.",
        )
    try:
        with path.open("rb") as f:
            data = f.read(MAX_SIGNERS_BYTES + 1)
        if len(data) > MAX_SIGNERS_BYTES:
            return Check(name, FAIL, f"{path} is too large. Keep one line a key.")
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return Check(name, FAIL, f"cannot read {path}: {exc}")
    entries = [e for e in parse_allowed_signers(text) if e.names(approval.signer)]
    usable = [e for e in entries if e.permits(approval.namespace) and e.fingerprint]
    other = len(entries) - len(usable)
    state.signer_keys = {e.fingerprint for e in usable if e.fingerprint}
    who = f"{approval.signer!r} in the namespace {approval.namespace!r}"
    if not usable:
        return Check(
            name,
            FAIL,
            f"no key for {who}. Add the line of the approval key (see the README).",
        )
    keys = ", ".join(sorted(state.signer_keys))
    if len(usable) > 1:
        return Check(
            name, WARN, f"{len(usable)} keys for {who}: {keys}. Keep only one."
        )
    if other:
        return Check(
            name,
            WARN,
            f"one key for {who}: {keys}. {other} other line(s) for the signer "
            "are not valid in this namespace.",
        )
    return Check(name, OK, f"one key for {who}: {keys}")


def _ssh_keygen(state: _State, tools: Tools) -> Check:
    if not is_file(Path(SSH_KEYGEN)) or not os.access(SSH_KEYGEN, os.X_OK):
        return Check(
            "ssh-keygen", FAIL, f"no program at {SSH_KEYGEN}. Install OpenSSH."
        )
    try:
        _code, out, err = tools.run([SSH, "-V"], PROGRAM_TIMEOUT)
    except ProgramFailed:
        return Check("ssh-keygen", OK, f"{SSH_KEYGEN} (version not known)")
    version = (err.strip() or out.strip()).splitlines()
    return Check(
        "ssh-keygen", OK, f"{SSH_KEYGEN}, {version[0] if version else 'version?'}"
    )


def _inside(path: Path, folder: Path) -> bool:
    try:
        return path.resolve().is_relative_to(folder.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def _key_file(state: _State, tools: Tools) -> Check:
    name = "key file"
    if state.config.approval.key is None:
        return Check(name, FAIL, "facts.toml has no [approval] key. Set the key path.")
    path = key_path(state.config.approval)
    if path is None:
        return Check(name, FAIL, "[approval] key must be an absolute path or ~/...")
    try:
        link = stat.S_ISLNK(os.lstat(path).st_mode)
        info = os.stat(path) if link else os.lstat(path)
    except FileNotFoundError:
        return Check(name, FAIL, f"no key at {path}. Make it as the README says.")
    except (OSError, ValueError) as exc:
        return Check(name, FAIL, f"cannot read the status of {path}: {exc}")
    if not stat.S_ISREG(info.st_mode):
        return Check(name, FAIL, f"{path} is not a regular file.")
    if info.st_uid != os.getuid():
        return Check(name, FAIL, f"{path} belongs to another user.")
    if stat.S_IMODE(info.st_mode) & 0o077:
        return Check(
            name,
            FAIL,
            f"other users can use {path}. Run: chmod 600 {path}",
        )
    if _inside(path, state.repo):
        return Check(
            name, FAIL, f"{path} is inside the data root. Move it out of the data root."
        )
    state.key = path
    if link:
        return Check(name, WARN, f"{path} is a symbolic link. Use the file itself.")
    return Check(name, OK, str(path))


def _key_fingerprint(state: _State, tools: Tools) -> Check:
    name = "key fingerprint"
    if state.key is None:
        return Check(name, WARN, "not checked: no usable key file")
    try:
        code, out, err = tools.run(
            [SSH_KEYGEN, "-l", "-f", str(state.key)], PROGRAM_TIMEOUT
        )
    except ProgramFailed as exc:
        return Check(name, FAIL, f"{exc}")
    found = SHA256_FINGERPRINT.search(out)
    if code != 0 or found is None:
        reason = (err.strip().splitlines() or [f"exit code {code}"])[-1]
        return Check(name, FAIL, f"ssh-keygen cannot read the key: {reason}")
    state.fingerprint = found.group(0)
    if state.fingerprint not in state.signer_keys:
        return Check(
            name,
            FAIL,
            f"{state.fingerprint}: allowed_signers has no line for this key and "
            "the signer. Add the line of this key.",
        )
    return Check(name, OK, f"{state.fingerprint}, in allowed_signers")


def _key_passphrase(state: _State, tools: Tools) -> Check:
    name = "key passphrase"
    if state.key is None:
        return Check(name, WARN, "not checked: no usable key file")
    try:
        # -P "" tries an empty passphrase. stdout (the public key) is dropped.
        code, _out, err = tools.run(
            [SSH_KEYGEN, "-y", "-P", "", "-f", str(state.key)], PROGRAM_TIMEOUT
        )
    except ProgramFailed as exc:
        return Check(name, WARN, f"not checked: {exc}")
    if code == 0:
        return Check(
            name,
            FAIL,
            "the key has no passphrase, so each process that can read it can "
            f"approve. Run: ssh-keygen -p -f {state.key}",
        )
    if "incorrect passphrase" in err.lower():
        return Check(name, OK, "the key has a passphrase")
    reason = (err.strip().splitlines() or [f"exit code {code}"])[-1]
    return Check(name, WARN, f"not checked: {reason}")


def _agent(state: _State, tools: Tools) -> Check:
    name = "ssh-agent"
    if not is_file(Path(SSH_ADD)):
        return Check(name, OK, f"no {SSH_ADD}, so no agent check")
    if state.fingerprint is None:
        return Check(name, WARN, "not checked: the key fingerprint is not known")
    try:
        code, out, _err = tools.run([SSH_ADD, "-l"], AGENT_TIMEOUT)
    except ProgramFailed as exc:
        return Check(name, WARN, f"not checked: {exc}")
    if code == 2:
        return Check(name, OK, "no agent runs")
    if code == 1:
        return Check(name, OK, "the agent holds no key")
    if code != 0:
        return Check(name, WARN, f"not checked: ssh-add gave exit code {code}")
    loaded = set(SHA256_FINGERPRINT.findall(out))
    if state.fingerprint in loaded:
        return Check(
            name,
            FAIL,
            "the approval key is loaded in the agent, so a process can sign with "
            f"no passphrase. Run: ssh-add -d {state.key}",
        )
    return Check(name, OK, "the approval key is not loaded")


def _signer_history(state: _State, tools: Tools) -> Check:
    name = "signer history"
    if state.fingerprint is None:
        return Check(name, WARN, "not checked: the key fingerprint is not known")
    folder = state.repo / APPLICATIONS
    signed = 0
    other: list[str] = []
    try:
        runs = sorted(folder.iterdir()) if is_dir(folder) else []
    except OSError as exc:
        return Check(name, WARN, f"not checked: {exc}")
    for run in runs:
        data = _manifest(run / MANIFEST)
        if data is None or data.get("signed") is not True:
            continue
        signed += 1
        if data.get("signer_fingerprint") != state.fingerprint:
            other.append(run.name)
    if other:
        return Check(
            name,
            WARN,
            f"{len(other)} signed run(s) name a different key: {', '.join(other)}. "
            "Make sure that you approved them.",
        )
    return Check(name, OK, f"{signed} signed run(s), each with this key")


def _manifest(path: Path) -> dict[str, Any] | None:
    """Return a manifest as a dict, or None (a link, too large, not valid)."""
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode) or not is_file(path):
            return None
        with path.open("rb") as f:
            data = f.read(MAX_MANIFEST_BYTES + 1)
        if len(data) > MAX_MANIFEST_BYTES:
            return None
        value: Any = json.loads(data.decode("utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# --- The renderer ----------------------------------------------------------------


def _renderer_settings(state: _State, tools: Tools) -> Check:
    name = "renderer settings"
    try:
        state.renderer = resume_matcher.check_settings(state.repo, state.config.render)
    except InputError as exc:
        return Check(name, FAIL, str(exc))
    return Check(name, OK, f"Resume Matcher at {state.renderer.path}")


def _pin(state: _State) -> str | None:
    render = state.config.render
    return render.resume_matcher.commit if render is not None else None


def _renderer_commit(state: _State, tools: Tools) -> Check:
    name = "renderer commit"
    if state.renderer is None:
        return Check(name, WARN, "not checked: the renderer settings are not valid")
    commit = resume_matcher.clone_commit(state.renderer.path)
    pin = _pin(state)
    if commit is None:
        return Check(
            name, FAIL, f"cannot read the commit of the clone {state.renderer.path}"
        )
    if pin is None:
        return Check(
            name,
            WARN,
            f"the clone is at {commit[:12]}. Set [render.resume_matcher] commit.",
        )
    if not PIN.fullmatch(pin):
        return Check(name, FAIL, "the facts.toml commit must be 7 or more hex digits")
    if not commit.startswith(pin):
        return Check(
            name,
            FAIL,
            f"the clone is at {commit[:12]}, facts.toml pins {pin}. "
            "Check out the pinned commit.",
        )
    return Check(name, OK, f"the clone is at the pinned commit {pin}")


def _version(tag: str) -> tuple[int, int, int]:
    found = SEMVER.fullmatch(tag)
    if found is None:
        return (-1, -1, -1)
    return (int(found.group(1)), int(found.group(2)), int(found.group(3)))


def newest_tag(names: Iterable[str]) -> str | None:
    """Return the newest semantic-version tag (vX.Y.Z or X.Y.Z), or None."""
    tags = [n for n in names if SEMVER.fullmatch(n)]
    return max(tags, key=_version) if tags else None


class _TooLarge(Exception):
    pass


def _client(tools: Tools, timeout: httpx.Timeout) -> httpx.Client:
    """Return a client with no proxy settings, no redirects and no cookie."""
    return httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": USER_AGENT},
        transport=tools.transport,
    )


def _get(client: httpx.Client, url: str, limit: int) -> tuple[int, bytes]:
    """GET `url` and return the status and at most `limit` bytes of the body."""
    with client.stream("GET", url) as response:
        body = bytearray()
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > limit:
                raise _TooLarge(f"the answer is larger than {limit} bytes")
        return response.status_code, bytes(body)


def _tag_list(body: bytes) -> dict[str, str]:
    """Return {tag name: commit} from the GitHub tag list, or raise ValueError."""
    value: Any = json.loads(body.decode("utf-8"))
    if not isinstance(value, list):
        raise ValueError("the tag list is not a JSON list")
    tags: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        tag: Any = item.get("name")
        commit: Any = item.get("commit")
        sha: Any = commit.get("sha") if isinstance(commit, dict) else None
        if isinstance(tag, str) and isinstance(sha, str) and FULL_COMMIT.fullmatch(sha):
            tags[tag] = sha
    return tags


def _release(state: _State, tools: Tools, offline: bool) -> Check:
    name = "release tags"
    if offline:
        return Check(name, WARN, "not checked: --offline")
    pin = _pin(state)
    if pin is None or not PIN.fullmatch(pin):
        return Check(name, WARN, "not checked: facts.toml pins no commit")
    try:
        with _client(tools, RELEASE_TIMEOUT) as client:
            status, body = _get(client, RELEASE_URL, MAX_TAGS_BYTES)
        if status != 200:
            return Check(name, WARN, f"not checked: GitHub gave HTTP {status}")
        tags = _tag_list(body)
    except (httpx.HTTPError, _TooLarge, ValueError) as exc:
        return Check(name, WARN, f"not checked: {type(exc).__name__}: {exc}")
    newest = newest_tag(tags)
    if newest is None:
        return Check(name, WARN, "not checked: GitHub lists no release tag")
    sha = tags[newest]
    if sha.startswith(pin):
        return Check(name, OK, f"the pin {pin} is the newest release {newest}")
    return Check(
        name,
        WARN,
        f"the newest release is {newest} ({sha[:7]}), not the pin {pin}. "
        f"Read the release notes of {newest} before you change the pin.",
    )


def _renderer_records(state: _State, tools: Tools) -> Check:
    name = "renderer records"
    config = state.renderer
    if config is None:
        return Check(name, WARN, "not checked: the renderer settings are not valid")
    if not is_dir(config.data_dir):
        return Check(
            name, OK, f"{config.data_dir} does not exist yet; the first render makes it"
        )
    renderer = ResumeMatcher(config, runner=_list_runner(tools))
    try:
        scratch = renderer.scratch_id()
    except InputError as exc:
        return Check(name, FAIL, str(exc))
    if scratch is None:
        return Check(name, OK, "the data folder holds no record")
    return Check(name, OK, "the data folder holds only the darzi scratch record")


def _list_runner(tools: Tools) -> resume_matcher.Runner:
    """Return a runner for the Resume Matcher CLI that runs only `list_resumes`.

    The adapter gives the JSON arguments on stdin (the last argument is "-").
    This runner gives them as the last argument, so that stdin can be the null
    device. Each other tool is refused, so `doctor` writes no record.
    """

    def run(cmd: Sequence[str], stdin: str) -> tuple[int, str, str]:
        argv = list(cmd)
        if len(argv) < 2 or argv[-2] != "list_resumes" or argv[-1] != "-":
            raise resume_matcher.RenderError("facts doctor runs only list_resumes.")
        try:
            return tools.run([*argv[:-1], stdin], float(resume_matcher.TIMEOUT_SECONDS))
        except ProgramFailed as exc:
            raise resume_matcher.RenderError(str(exc)) from exc

    return run


def _loopback_only(host: str, tools: Tools) -> str:
    """Return "" when each address of `host` is a loopback address, else why not."""
    try:
        addresses = tools.resolver(host)
    except (OSError, UnicodeError) as exc:
        return f"cannot resolve {host}: {exc}"
    if not addresses:
        return f"{host} has no address"
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return f"{host} has the address {address!r}, which is not an IP address"
        if not ip.is_loopback:
            return f"{host} resolves to {address}, which is not a loopback address"
    return ""


def _health(state: _State, tools: Tools, label: str, key: str) -> Check:
    name = f"renderer {label}"
    config = state.renderer
    if config is None:
        return Check(name, WARN, "not checked: the renderer settings are not valid")
    value: str = getattr(config, key)
    try:
        url = resume_matcher.loopback_url(key, value)
    except InputError as exc:
        return Check(name, FAIL, str(exc))
    host = urlsplit(url).hostname or ""
    problem = _loopback_only(host, tools)
    if problem:
        return Check(name, FAIL, f"not requested: {problem}")
    target = url + HEALTH_PATH
    try:
        with _client(tools, HEALTH_TIMEOUT) as client:
            status, body = _get(client, target, MAX_HEALTH_BYTES)
    except (httpx.HTTPError, _TooLarge) as exc:
        return Check(
            name,
            FAIL,
            f"no answer from {target} ({type(exc).__name__}). Start Resume Matcher.",
        )
    if status != 200 or b"healthy" not in body:
        return Check(name, FAIL, f"{target} gave HTTP {status} and no healthy status")
    return Check(name, OK, f"{target} is healthy")


# --- LibreOffice and the form ---------------------------------------------------


def _libreoffice(state: _State, tools: Tools) -> Check:
    found = tools.find_soffice()
    if found is None:
        return Check("LibreOffice", FAIL, pages.INSTALL_HINT)
    return Check("LibreOffice", OK, str(found))


def _form(state: _State, tools: Tools) -> Check:
    form = state.config.form
    if form is None:
        return Check(
            "form settings",
            FAIL,
            "facts.toml has no [form] table, and fill-plan refuses that. Add "
            "[form] with a blocked_hosts list.",
        )
    if not form.blocked_hosts:
        return Check("form settings", WARN, "[form] blocked_hosts is empty")
    return Check("form settings", OK, f"{len(form.blocked_hosts)} blocked host(s)")
