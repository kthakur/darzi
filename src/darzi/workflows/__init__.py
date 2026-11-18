"""The agent workflows, stored as package data (`facts workflow NAME`).

Each workflow is a Markdown file in this package. `facts workflow NAME` prints
the file exactly, so an agent reads the same text that this package holds.
"""

from importlib.resources import files

NAMES = ("apply",)


def load_workflow(name: str) -> str:
    """Return the text of the workflow `name`. Raises KeyError for another name."""
    if name not in NAMES:
        raise KeyError(name)
    return files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8")
