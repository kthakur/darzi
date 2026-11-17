"""A fake Resume Matcher CLI for the render tests. It keeps records in memory.

The fake is a runner for `ResumeMatcher`: it takes the argument list and the
stdin text, and gives the exit code, stdout and stderr. No subprocess starts.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from darzi.render.resume_matcher import ResumeMatcher, ResumeMatcherConfig

MAX_MASTERS = 5


@dataclass
class FakeCli:
    """An in-memory Resume Matcher CLI."""

    records: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    calls: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )
    commands: list[list[str]] = field(default_factory=list[list[str]])
    pages: Any = 1
    write_pdf: bytes | None = b"%PDF-1.7\n%fake\n"
    fail: dict[str, str] = field(default_factory=dict[str, str])

    def add(self, title: str, is_master: bool = True) -> str:
        rid = f"id-{len(self.records) + 1}"
        self.records.append(
            {"resume_id": rid, "title": title, "is_master": is_master, "data": {}}
        )
        return rid

    def tools(self) -> list[str]:
        return [tool for tool, _ in self.calls]

    def __call__(self, cmd: Sequence[str], stdin: str) -> tuple[int, str, str]:
        self.commands.append(list(cmd))
        tool = cmd[-2]
        args: dict[str, Any] = json.loads(stdin)
        self.calls.append((tool, args))
        if tool in self.fail:
            return 1, "", f"log line\nError executing tool {tool}: {self.fail[tool]}\n"
        return 0, json.dumps(self._answer(tool, args)), ""

    def _answer(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "list_resumes":
            return {
                "resumes": [
                    {k: v for k, v in r.items() if k != "data"} for r in self.records
                ]
            }
        if tool == "create_master_resume":
            if sum(1 for r in self.records if r["is_master"]) >= MAX_MASTERS:
                raise AssertionError("the adapter must not pass the limit")
            rid = self.add(args["title"])
            self.records[-1]["data"] = args["resume_data"]
            return {"resume_id": rid, "title": args["title"], "is_master": True}
        if tool == "update_resume":
            record = next(
                r for r in self.records if r["resume_id"] == args["resume_id"]
            )
            record["data"] = args["resume_data"]
            return {"resume_id": record["resume_id"]}
        if tool == "export_resume_pdf":
            if self.write_pdf is not None:
                out = Path(args["output_path"])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(self.write_pdf)
            return {
                "path": args["output_path"],
                "page_count": self.pages,
                "bytes": 10,
                "template": "swiss-single",
                "page_size": args["print_settings"].get("pageSize", "A4"),
            }
        raise AssertionError(f"unexpected tool {tool}")


def fake_install(root: Path) -> ResumeMatcherConfig:
    """Return settings for a fake Resume Matcher folder and a data folder."""
    (root / "rm" / "apps" / "backend").mkdir(parents=True)
    return ResumeMatcherConfig(
        path=root / "rm",
        data_dir=root / "rm-data",
        frontend_url="http://127.0.0.1:3100",
        backend_url="http://127.0.0.1:8100",
    )


def fake_renderer(root: Path, cli: FakeCli) -> ResumeMatcher:
    """Return a ResumeMatcher on a fake install that uses the fake CLI."""
    return ResumeMatcher(fake_install(root), runner=cli)
