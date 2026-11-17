"""The renderer interface (docs/SPEC.md section 9).

A renderer makes a PDF from one résumé in the Resume Matcher JSON format. The
Resume Matcher adapter (`darzi.render.resume_matcher`) is the only renderer in
this build.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from darzi.facts.repo import InputError


class RenderError(InputError):
    """The renderer failed or could not run. The command gives exit code 1.

    A plain InputError from a renderer is bad input or a bad setting: exit
    code 2.
    """


@dataclass(frozen=True)
class RenderResult:
    """The result of one render: the page count, the template, the page size."""

    pages: int
    template: str
    page_size: str


class Renderer(Protocol):
    """Make a PDF file at `out` from `resume` with the print `settings`."""

    def render(
        self, resume: dict[str, Any], settings: dict[str, Any], out: Path
    ) -> RenderResult:
        """Write the PDF to `out` and return its RenderResult.

        Raises RenderError when the renderer fails, and InputError for bad
        input or bad settings.
        """
        ...
