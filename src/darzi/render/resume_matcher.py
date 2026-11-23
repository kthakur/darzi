"""The Resume Matcher adapter: render a PDF with the Resume Matcher CLI.

The adapter starts the CLI of a pinned Resume Matcher install as a subprocess,
with no shell, a 120 s timeout and the JSON arguments on stdin (docs/SPEC.md
section 9). The Resume Matcher backend and frontend servers must already run
with the same data folder. The adapter does not start them.

The command is one argument list:

    /usr/bin/env -u VIRTUAL_ENV DATA_DIR=<data_dir> FRONTEND_BASE_URL=<url>
        DATABASE_URL= uv run --no-sync --directory <path>/apps/backend
        resume-matcher <tool> -

`env` removes VIRTUAL_ENV and sets the three values in the child process
only. So this module reads no environment variable, and the subprocess gets
the rest of the environment unchanged.

Scratch record: Resume Matcher keeps at most 5 master résumés. The adapter
keeps one master record with the title SCRATCH_TITLE in its own data folder and
overwrites it for each render. It finds the record by its title each time.

Guards (docs/SPEC.md section 9):

- `data_dir` is required and must not be inside the Resume Matcher folder
  `path`, after each symbolic link is followed. So the default data folder of
  Resume Matcher, which can hold the owner's own résumés, is never used.
  `data_dir` must also not be the data root, hold the data root, or hold
  `path`. A folder inside the data root, such as `.rm-data`, is correct.
- The four settings must not hold a NUL character, a line end or a tab.
- The data folder must hold no record other than the scratch record, and at
  most one scratch record. Else the render stops before a write: darzi writes
  nothing in the data folder before the list of records passes this rule.
- `frontend_url` and `backend_url` must be `http` URLs on a loopback address.
- `DATABASE_URL` is empty for the CLI, so Resume Matcher uses SQLite in
  `data_dir` and not a database that a `.env` file names.
- A lock file stops two renders at the same time. It is in the system
  temporary folder and not in the data folder (see `lock_path`).
- `uv run --no-sync` does not change the Resume Matcher install.

Commit: `clone_commit()` reads the commit of the Resume Matcher clone from the
files in its `.git` folder, with no subprocess. `finalize` writes it into
`manifest.json`. It is None when the files do not give a commit.
"""

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from darzi.config import RenderSettings
from darzi.facts.repo import InputError
from darzi.paths import is_dir, is_file
from darzi.render.port import RenderError, RenderResult

ADAPTER = "resume-matcher"
ENV = "/usr/bin/env"
TIMEOUT_SECONDS = 120
SCRATCH_TITLE = "darzi scratch"
# The lock file name is LOCK_PREFIX + a hash of the data folder + LOCK_SUFFIX.
LOCK_PREFIX = "darzi-render-"
LOCK_SUFFIX = ".lock"
LOCK_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
# The keys of [render.resume_matcher] that a render needs.
REQUIRED = ("path", "data_dir", "frontend_url", "backend_url")
SECTION = "[render.resume_matcher]"
# A NUL character, a tab and each line end of str.splitlines().
BAD_CHARACTERS = "\0\t\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"

# A commit id: SHA-1 (40) or SHA-256 (64) hex characters.
COMMIT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# The largest .git file that clone_commit() reads: 4 MiB (packed-refs).
MAX_GIT_FILE = 4 * 1024 * 1024

# A runner takes the argument list and the stdin text. It returns the exit
# code, stdout and stderr. Tests give a fake runner.
Runner = Callable[[Sequence[str], str], tuple[int, str, str]]


@dataclass(frozen=True)
class ResumeMatcherConfig:
    """The checked settings of one Resume Matcher install, with absolute paths."""

    path: Path
    data_dir: Path
    frontend_url: str
    backend_url: str


def loopback_url(name: str, value: str) -> str:
    """Return `value` with no "/" at the end, or raise InputError.

    The URL must be `http` on the host localhost, 127.0.0.1 or ::1, with no
    user name or password. Then the browser of the CLI loads no page from
    another machine.
    """
    problem = f"{SECTION} {name} {value!r} must be an http URL on localhost, "
    fix = "127.0.0.1 or ::1. Use the address of the local Resume Matcher."
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError for a port that is not a number
    except ValueError as exc:
        raise InputError(problem + fix) from exc
    if (
        parts.scheme != "http"
        or parts.hostname not in LOOPBACK_HOSTS
        or parts.username is not None
        or parts.password is not None
    ):
        raise InputError(problem + fix)
    return value.rstrip("/")


def _resolve(repo: Path, name: str, value: str) -> Path:
    try:
        return (repo / value).resolve()
    except (OSError, RuntimeError) as exc:
        raise InputError(
            f"{SECTION} {name} {value!r} cannot be resolved: {exc}. Fix the path."
        ) from exc


def check_settings(repo: Path, render: RenderSettings | None) -> ResumeMatcherConfig:
    """Return the checked Resume Matcher settings of the data root `repo`.

    Relative paths start at `repo`. Raises InputError when `[render]` is
    absent, the adapter is not "resume-matcher", a required key is absent, a
    value holds a NUL character, a line end or a tab, a URL is not a loopback
    http URL, `path` has no `apps/backend` folder, `data_dir` is inside
    `path`, or `data_dir` is the data root, holds the data root or holds
    `path`. A `data_dir` inside the data root is correct.
    """
    if render is None:
        raise InputError(
            "facts.toml has no [render] section. Add [render] and "
            f"{SECTION} (docs/SPEC.md section 5)."
        )
    if render.adapter != ADAPTER:
        raise InputError(
            f"[render] adapter must be {ADAPTER!r}, not {render.adapter!r}. "
            "Fix facts.toml."
        )
    rm = render.resume_matcher
    values = {name: getattr(rm, name) for name in REQUIRED}
    for name, value in values.items():
        if value is None:
            raise InputError(f"facts.toml has no {SECTION} {name}. Add it.")
        if any(char in BAD_CHARACTERS for char in value):
            raise InputError(
                f"{SECTION} {name} {value!r} holds a NUL character, a line end "
                "or a tab. Remove it from facts.toml."
            )
    frontend = loopback_url("frontend_url", values["frontend_url"])
    backend = loopback_url("backend_url", values["backend_url"])
    path = _resolve(repo, "path", values["path"])
    if not is_dir(path / "apps" / "backend"):
        raise InputError(
            f"{SECTION} path {path} has no apps/backend folder. "
            "Set path to the Resume Matcher folder."
        )
    data_dir = _resolve(repo, "data_dir", values["data_dir"])
    if data_dir == path or path in data_dir.parents:
        raise InputError(
            f"{SECTION} data_dir {data_dir} is inside the Resume Matcher folder "
            f"{path}. Use a folder outside it that only darzi uses."
        )
    root = _resolve(repo, "data root", ".")
    use = "Use a folder that only darzi uses, such as .rm-data in the data root."
    if data_dir == root:
        raise InputError(f"{SECTION} data_dir {data_dir} is the data root. {use}")
    if data_dir in root.parents:
        raise InputError(
            f"{SECTION} data_dir {data_dir} holds the data root {root}. {use}"
        )
    if data_dir in path.parents:
        raise InputError(
            f"{SECTION} data_dir {data_dir} holds the Resume Matcher folder "
            f"{path}. {use}"
        )
    return ResumeMatcherConfig(path, data_dir, frontend, backend)


def lock_path(data_dir: Path) -> Path:
    """Return the lock file of the data folder `data_dir`.

    The lock file is in the system temporary folder, not in the data folder.
    So a render writes nothing in the data folder before the list of records
    passes the scratch-only rule. The name holds the SHA-256 hash of the
    resolved `data_dir` path: the same data folder always gives the same lock
    file, also through a symbolic link. Raises OSError or RuntimeError when
    the path cannot be resolved.
    """
    key = hashlib.sha256(os.fsencode(data_dir.resolve())).hexdigest()[:32]
    return Path(tempfile.gettempdir()) / f"{LOCK_PREFIX}{key}{LOCK_SUFFIX}"


def run_command(cmd: Sequence[str], stdin: str) -> tuple[int, str, str]:
    """Run `cmd` with no shell and return the exit code, stdout and stderr."""
    try:
        done = subprocess.run(
            list(cmd),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RenderError(f"cannot start {cmd[0]}: {exc.strerror}.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RenderError(
            f"Resume Matcher gave no answer in {TIMEOUT_SECONDS} s. Make sure "
            "that its backend and frontend servers run."
        ) from exc
    except OSError as exc:
        raise RenderError(f"cannot start {cmd[0]}: {exc}.") from exc
    return done.returncode, done.stdout, done.stderr


@dataclass(frozen=True)
class ResumeMatcher:
    """The Resume Matcher renderer. It implements the Renderer protocol."""

    config: ResumeMatcherConfig
    runner: Runner = run_command

    def environment(self) -> list[str]:
        """Return the NAME=value arguments that `env` sets for the CLI."""
        return [
            f"DATA_DIR={self.config.data_dir}",
            f"FRONTEND_BASE_URL={self.config.frontend_url}",
            "DATABASE_URL=",
        ]

    def command(self, tool: str) -> list[str]:
        """Return the argument list for one tool. The arguments come on stdin."""
        backend = self.config.path / "apps" / "backend"
        return [
            ENV,
            "-u",
            "VIRTUAL_ENV",
            *self.environment(),
            "uv",
            "run",
            "--no-sync",
            "--directory",
            str(backend),
            "resume-matcher",
            tool,
            "-",
        ]

    def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one tool and return its JSON object. Raises RenderError."""
        code, out, err = self.runner(self.command(tool), json.dumps(args or {}))
        if code != 0:
            lines = err.strip().splitlines()
            reason = lines[-1] if lines else f"exit code {code}"
            raise RenderError(f"resume-matcher {tool} failed: {reason}")
        try:
            result: Any = json.loads(out)
        except json.JSONDecodeError as exc:
            raise RenderError(
                f"resume-matcher {tool} gave output that is not JSON."
            ) from exc
        if not isinstance(result, dict):
            raise RenderError(f"resume-matcher {tool} gave no JSON object.")
        return result

    def commit(self) -> str | None:
        """Return the commit of the Resume Matcher clone, or None (see clone_commit)."""
        return clone_commit(self.config.path)

    def scratch_id(self) -> str | None:
        """Return the id of the scratch record, or None when there is none.

        Raises InputError when the data folder holds another record or two
        scratch records. Then nothing is written.
        """
        listed = self.call("list_resumes", {"include_tailored": True})
        records: Any = listed.get("resumes")
        if not isinstance(records, list):
            raise RenderError("resume-matcher list_resumes gave no list of résumés.")
        scratch: list[str] = []
        for record in records:
            ok = isinstance(record, dict) and record.get("title") == SCRATCH_TITLE
            rid: Any = record.get("resume_id") if ok else None
            if (
                not ok
                or record.get("is_master") is not True
                or not isinstance(rid, str)
            ):
                raise InputError(
                    f"The Resume Matcher data folder {self.config.data_dir} holds a "
                    "résumé that darzi did not make. Set data_dir to a folder "
                    "that only darzi uses."
                )
            scratch.append(rid)
        if len(scratch) > 1:
            raise InputError(
                f"The Resume Matcher data folder {self.config.data_dir} holds "
                f"{len(scratch)} records with the title {SCRATCH_TITLE!r}. Keep "
                "one: delete the others in the Resume Matcher web app."
            )
        return scratch[0] if scratch else None

    def render(
        self, resume: dict[str, Any], settings: dict[str, Any], out: Path
    ) -> RenderResult:
        """Write the PDF of `resume` to `out` through the scratch record.

        The order is: take the lock (a write only in the temporary folder),
        make the data folder when it does not exist (one level, no parents),
        list and test the records, create or update the scratch record, export
        the PDF, release the lock. A held lock stops the render at once.

        Raises RenderError when Resume Matcher fails or the lock is held, and
        InputError for bad input, a data folder with other records, or a data
        folder that cannot be made.
        """
        out = out.resolve()
        if out.suffix.lower() != ".pdf":
            raise InputError(f"{out} does not end with .pdf. Give a .pdf path.")
        data_dir = self.config.data_dir
        try:
            lock_file = lock_path(data_dir)
            # O_NOFOLLOW: a symbolic link in the temporary folder is refused.
            # O_NONBLOCK: a named pipe with no reader is refused at once, not
            # waited for. It has no effect on a regular file.
            lock = os.open(lock_file, LOCK_FLAGS, 0o600)
        except (OSError, RuntimeError) as exc:
            raise RenderError(
                f"cannot open the lock file for the data folder {data_dir}: "
                f"{exc}. Check the temporary folder."
            ) from exc
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise RenderError(
                    f"the lock file for the data folder {data_dir} is not a "
                    "regular file. Check the temporary folder."
                )
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RenderError(
                    f"another render uses {data_dir} now. Wait for it to end, "
                    "then try again."
                ) from exc
            self._make_data_dir()
            exported = self._write(resume, settings, out)
        finally:
            os.close(lock)  # This also releases the lock.
        return _result(exported, out)

    def _make_data_dir(self) -> None:
        """Make the data folder when it does not exist: one level, no parents.

        A folder that this makes is empty, so it holds no other record.
        """
        data_dir = self.config.data_dir
        try:
            if not is_dir(data_dir):
                data_dir.mkdir()
        except OSError as exc:
            raise InputError(
                f"cannot use the data folder {data_dir}: {exc.strerror}. Check "
                "data_dir, its parent folder and their permissions."
            ) from exc

    def _write(
        self, resume: dict[str, Any], settings: dict[str, Any], out: Path
    ) -> dict[str, Any]:
        """Test the records, write the scratch record and export. Hold the lock."""
        rid = self.scratch_id()
        if rid is None:
            created = self.call(
                "create_master_resume",
                {"resume_data": resume, "title": SCRATCH_TITLE},
            )
            new_id: Any = created.get("resume_id")
            if not isinstance(new_id, str):
                raise RenderError("resume-matcher create_master_resume gave no id.")
            rid = new_id
        else:
            self.call("update_resume", {"resume_id": rid, "resume_data": resume})
        return self.call(
            "export_resume_pdf",
            {"resume_id": rid, "output_path": str(out), "print_settings": settings},
        )


def _result(exported: dict[str, Any], out: Path) -> RenderResult:
    """Return the RenderResult of an export_resume_pdf answer, or raise."""
    pages: Any = exported.get("page_count")
    if isinstance(pages, bool) or not isinstance(pages, int) or pages < 1:
        raise RenderError(
            f"resume-matcher export_resume_pdf gave the page count {pages!r}, "
            "not a number of 1 or more."
        )
    try:
        with out.open("rb") as f:
            head = f.read(5)
    except OSError as exc:
        raise RenderError(
            f"resume-matcher export_resume_pdf wrote no file at {out}."
        ) from exc
    if not is_file(out) or head != b"%PDF-":
        raise RenderError(f"{out} is not a PDF file.")
    template: Any = exported.get("template")
    page_size: Any = exported.get("page_size")
    return RenderResult(
        pages=pages,
        template=template if isinstance(template, str) else "",
        page_size=page_size if isinstance(page_size, str) else "",
    )


def _git_text(path: Path) -> str | None:
    """Return the text of one small file of a `.git` folder, or None."""
    if not is_file(path):
        return None
    with path.open("rb") as f:
        data = f.read(MAX_GIT_FILE + 1)
    if len(data) > MAX_GIT_FILE:
        return None
    return data.decode("utf-8")


def _git_folder(clone: Path) -> tuple[Path, Path] | None:
    """Return the git folder of `clone` and its common folder, or None.

    `.git` is a folder, or a file with a `gitdir:` line (a worktree). A
    worktree keeps its branches in the folder that its `commondir` file names.
    """
    git = clone / ".git"
    if not is_dir(git):
        text = _git_text(git)
        if text is None or not text.startswith("gitdir:"):
            return None
        git = clone / text[len("gitdir:") :].strip()
        if not is_dir(git):
            return None
    common = git
    text = _git_text(git / "commondir")
    if text is not None:
        common = git / text.strip()
    return git, common


def clone_commit(clone: Path) -> str | None:
    """Return the commit that HEAD of the git clone `clone` names, or None.

    The function reads `.git/HEAD`, then the branch file or `packed-refs`. It
    starts no program. Each problem (no `.git`, a file that cannot be read, a
    value that is not a commit id) gives None.
    """
    try:
        folders = _git_folder(clone)
        if folders is None:
            return None
        git, common = folders
        head = (_git_text(git / "HEAD") or "").strip()
        if COMMIT_ID.fullmatch(head):
            return head
        if not head.startswith("ref:"):
            return None
        ref = head[len("ref:") :].strip()
        if not ref.startswith("refs/") or ".." in ref.split("/"):
            return None
        for folder in (git, common):
            value = _git_text(folder / ref)
            if value is not None:
                value = value.strip()
                return value if COMMIT_ID.fullmatch(value) else None
        for line in (_git_text(common / "packed-refs") or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref and COMMIT_ID.fullmatch(parts[0]):
                return parts[0]
        return None
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def from_settings(repo: Path, render: RenderSettings | None) -> ResumeMatcher:
    """Return the Resume Matcher renderer for the data root `repo`."""
    return ResumeMatcher(check_settings(repo, render))
