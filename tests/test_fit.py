"""Tests of `facts fit`: requirements.json, the rules of SPEC section 14, fit.json.

Each test works on a temporary copy of the synthetic data root in tests/data.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from darzi import cli
from darzi.config import FitSettings
from darzi.facts.fit import FIT_JSON, KEYS, normalize, rating

from .conftest import DATA

RUN = "2026-11-03-example-labs-product-designer"
JOB = (
    "# Product Designer\n\n"
    "We want a designer who can\nrun user research with shop owners.\n\n"
    "What you do:\n\n"
    "- Design flows in Figma.\n"
    "- Build prototypes in React.\n"
    "- Write clear notes for each design decision.\n"
    "- Test new ideas with users.\n"
    "- Keep the design system up to date.\n"
    "- Know Swift for the mobile app.\n"
    "- Work with a front–end team.\n"
    "- Make sign­up forms and the café menu.\n\n"
    "Requirements:\n\n"
    "- Two days each week in the office.\n"
    "- The right to work in the country.\n"
    "- A “users first” attitude.\n"
)
HIDDEN_TEXT = "Ran the hidden pilot QX7 for the board."


def hard(rid: str, quote: str, status: str, basis: str | None = None) -> Any:
    """Return one hard requirement."""
    item: dict[str, Any] = {"id": rid, "kind": "hard", "quote": quote}
    item["status"] = status
    if basis is not None:
        item["basis"] = basis
    return item


def wanted(rid: str, quote: str, *evidence: str) -> Any:
    """Return one wanted requirement."""
    return {"id": rid, "kind": "wanted", "quote": quote, "evidence": list(evidence)}


def good() -> list[Any]:
    """Return a valid list: one hard requirement, 6 of 7 wanted with evidence."""
    return [
        hard(
            "h1", "Two days each week in the office.", "met", "answers:current_location"
        ),
        wanted("w1", "Design flows in Figma.", "acme-design-system", "skill:Figma"),
        wanted("w2", "Build prototypes in React.", "globex-portal", "skill:React"),
        wanted("w3", "run user research with shop owners", "acme-research"),
        wanted("w4", "Test new ideas with users.", "skill:Usability testing"),
        wanted("w5", "Keep the design system up to date.", "acme-design-system"),
        wanted("w6", "Know Swift for the mobile app.", "plant-app-build"),
        wanted("w7", "Write clear notes for each design decision."),
    ]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A copy of the synthetic data root with the test job post in the run."""
    root = tmp_path / "data"
    shutil.copytree(DATA, root)
    (root / "applications" / RUN / "job.md").write_text(JOB, encoding="utf-8")
    return root


def run_dir(repo: Path) -> Path:
    return repo / "applications" / RUN


def write(repo: Path, items: list[Any] | None = None, top: Any = None) -> None:
    data = top if top is not None else {"schema_version": 1, "requirements": items}
    text = data if isinstance(data, str) else json.dumps(data)
    (run_dir(repo) / "requirements.json").write_text(text, encoding="utf-8")


def facts_fit(
    repo: Path, capsys: pytest.CaptureFixture[str], *extra: str
) -> tuple[int, str, str]:
    code = cli.main(["fit", str(run_dir(repo)), "--repo", str(repo), *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def fit_json(repo: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((run_dir(repo) / FIT_JSON).read_bytes())
    return data


def no_fit_json(repo: Path) -> bool:
    return not (run_dir(repo) / FIT_JSON).exists()


# --- A good run ----------------------------------------------------------------


def test_a_good_run_writes_fit_json(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(repo, good())
    code, out, err = facts_fit(repo, capsys)
    assert (code, err) == (0, "")
    data = fit_json(repo)
    assert tuple(data) == KEYS
    assert data["schema_version"] == 1
    assert data["run"] == RUN
    assert data["rating"] == "green"
    assert data["limits"] == {"green": 0.7, "yellow": 0.4}
    assert data["counts"] == {"wanted": 7, "wanted_with_evidence": 6}
    assert data["coverage"] == round(6 / 7, 4)
    folder = run_dir(repo)
    assert data["inputs"] == {
        "job.md": hashlib.sha256((folder / "job.md").read_bytes()).hexdigest(),
        "requirements.json": hashlib.sha256(
            (folder / "requirements.json").read_bytes()
        ).hexdigest(),
    }
    first, second = data["requirements"][:2]
    assert first == {
        "id": "h1",
        "kind": "hard",
        "quote": "Two days each week in the office.",
        "status": "met",
        "counted_as": "met",
        "basis": {
            "ref": "answers:current_location",
            "question": "Where are you located (city, state)?",
            "answer": "Springfield, ST",
        },
    }
    assert second["evidence"] == [
        {
            "ref": "acme-design-system",
            "text": "Built a design system in Figma with 40 shared components.",
        },
        {"ref": "skill:Figma", "text": "Figma"},
    ]
    gap = {"id": "w7", "quote": "Write clear notes for each design decision."}
    assert data["gaps"] == [gap]
    assert data["hard_not_met"] == data["hard_unknown"] == data["warnings"] == []
    assert out.splitlines() == [
        f"facts fit: {RUN} is green. 6 of 7 wanted requirements have evidence.",
        f'gap: w7 "{gap["quote"]}"',
        f"facts fit: wrote {folder / FIT_JSON}.",
    ]


def test_fit_json_is_byte_equal_on_a_second_run(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(repo, good())
    assert facts_fit(repo, capsys)[0] == 0
    first = (run_dir(repo) / FIT_JSON).read_bytes()
    assert facts_fit(repo, capsys)[0] == 0
    second = (run_dir(repo) / FIT_JSON).read_bytes()
    assert first == second
    assert first.endswith(b"}\n") and first.startswith(b'{\n  "schema_version": 1,')


def test_fit_json_is_utf_8_with_the_characters_as_they_are(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(repo, [*good(), wanted("w8", "the café menu", "skill:Figma")])
    assert facts_fit(repo, capsys)[0] == 0
    raw = (run_dir(repo) / FIT_JSON).read_bytes()
    assert "the café menu" in raw.decode("utf-8")


def test_json_output(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(repo, [*good(), wanted("w8", "the café menu", "skill:Figma")])
    code, out, _ = facts_fit(repo, capsys, "--json")
    assert code == 0
    assert out.isascii()
    data = json.loads(out)
    assert data["schema_version"] == 1
    assert data["command"] == "fit"
    assert data["fit"] == fit_json(repo)


# --- Rule 1: the quote ---------------------------------------------------------


def test_a_quote_that_is_not_in_the_post_is_an_error(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items = good()
    items[2]["quote"] = "Build prototypes in Vue."
    write(repo, items)
    code, out, err = facts_fit(repo, capsys)
    assert (code, out) == (1, "")
    assert "facts fit: w2: the quote is not in job.md." in err
    assert "fit.json did not change" in err
    assert no_fit_json(repo)


@pytest.mark.parametrize(
    "quote",
    [
        "We want a designer who can run user research",  # across a line break
        "BUILD PROTOTYPES IN REACT",  # letter case
        "two days each week",  # a no-break space in job.md
        "make signup forms",  # a soft hyphen in job.md
        "  design   flows\nin figma ",  # white space in the quote
        "the Ｆigma",  # NFKC: a full-width letter
    ],
)
def test_the_quote_match_ignores_white_space_case_and_soft_hyphens(
    repo: Path, capsys: pytest.CaptureFixture[str], quote: str
) -> None:
    job = JOB.replace("Design flows in Figma.", "Design flows in Figma. Use the Figma.")
    (run_dir(repo) / "job.md").write_text(job, encoding="utf-8")
    write(repo, [*good(), wanted("w9", quote)])
    assert facts_fit(repo, capsys)[0] == 0


@pytest.mark.parametrize(
    "quote",
    [
        'A "users first" attitude',  # straight quote marks; the post has curly
        "Work with a front-end team",  # a hyphen; the post has an en dash
        "flows in Figma. Build",  # across two list items, without the mark
    ],
)
def test_the_quote_match_does_not_fold_quote_marks_or_dashes(
    repo: Path, capsys: pytest.CaptureFixture[str], quote: str
) -> None:
    write(repo, [*good(), wanted("w9", quote)])
    code, _, err = facts_fit(repo, capsys)
    assert code == 1
    assert "w9: the quote is not in job.md" in err


def test_normalize() -> None:
    assert normalize("  A B\r\n\tC­D  ") == "a b cd"
    assert normalize("“X” – Y") == "“x” – y"


# --- Rule 2: the ids -----------------------------------------------------------


def _message(rid: str, what: str, ref: str) -> str:
    return (
        f'facts fit: {rid}: {what} id "{ref}" is not in the public copy of the '
        "facts. Use an id that facts public prints."
    )


def test_an_evidence_id_that_does_not_exist_is_an_error(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items = good()
    items[1]["evidence"] = ["acme-nothing", "skill:figma"]
    write(repo, items)
    code, out, err = facts_fit(repo, capsys)
    assert (code, out) == (1, "")
    assert _message("w1", "evidence", "acme-nothing") in err.splitlines()
    assert _message("w1", "evidence", "skill:figma") in err.splitlines()
    assert no_fit_json(repo)


def _add_private_facts(repo: Path) -> None:
    """Add an excluded bullet, a removed skill and a removed bullet."""
    path = repo / "master.yaml"
    master: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    bullets: list[Any] = master["roles"][0]["bullets"]
    hidden = dict(bullets[0], id="acme-hidden-pilot", text=HIDDEN_TEXT)
    hidden["exclude_from"] = ["all"]
    removed = dict(bullets[0], id="acme-crane", text="Led the Paper Crane Club.")
    bullets += [hidden, removed]
    master["skills"].append(dict(master["skills"][0], name="Flash"))
    path.write_text(yaml.safe_dump(master, allow_unicode=True), encoding="utf-8")


def test_a_private_id_gives_the_same_message_as_an_unknown_id(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add_private_facts(repo)
    items = good()
    items[1]["evidence"] = ["acme-hidden-pilot", "acme-crane", "skill:Flash"]
    items[2]["evidence"] = ["acme-unknown-one", "skill:Unknown Tool"]
    items[0]["basis"] = "acme-hidden-pilot"
    write(repo, items)
    code, out, err = facts_fit(repo, capsys)
    assert (code, out) == (1, "")
    lines = err.splitlines()
    for ref in ("acme-hidden-pilot", "acme-crane", "skill:Flash"):
        assert _message("w1", "evidence", ref) in lines
    for ref in ("acme-unknown-one", "skill:Unknown Tool"):
        assert _message("w2", "evidence", ref) in lines
    assert _message("h1", "basis", "acme-hidden-pilot") in lines
    assert "QX7" not in err and "Paper Crane" not in err
    assert no_fit_json(repo)


def test_private_data_in_the_public_copy_refuses_like_facts_public(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = repo / "master.yaml"
    master: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    bullet = master["roles"][0]["bullets"][0]
    bullet["text"] = "Wrote to other.person@example.com each week."
    path.write_text(yaml.safe_dump(master, allow_unicode=True), encoding="utf-8")
    write(repo, good())
    code, out, err = facts_fit(repo, capsys)
    assert cli.main(["public", "--repo", str(repo)]) == 1
    public_err = capsys.readouterr().err
    assert (code, out) == (1, "")
    assert err == public_err.replace("facts public:", "facts fit:")
    assert no_fit_json(repo)


@pytest.mark.parametrize(
    ("basis", "text"),
    [
        ("example-u-ba", "B.A. Graphic Design, Example University"),
        ("acme-checkout", "Designed a new checkout flow that cut drop-off by 12%."),
        ("plant-app", "Plant Care App"),
    ],
)
def test_a_fact_id_basis_shows_the_fact_text(
    repo: Path, capsys: pytest.CaptureFixture[str], basis: str, text: str
) -> None:
    items = good()
    items[0]["basis"] = basis
    write(repo, items)
    assert facts_fit(repo, capsys)[0] == 0
    shown = fit_json(repo)["requirements"][0]["basis"]
    assert shown["ref"] == basis
    assert shown["text"].startswith(text)


def test_a_skill_is_refused_as_a_basis(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items = good()
    items[0]["basis"] = "skill:Figma"
    write(repo, items)
    code, _, err = facts_fit(repo, capsys)
    assert code == 2
    assert "requirement h1: a basis cannot be a skill." in err
    assert no_fit_json(repo)


def test_an_unknown_answer_id_is_an_error(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items = good()
    items[0]["basis"] = "answers:no_such_answer"
    write(repo, items)
    code, _, err = facts_fit(repo, capsys)
    assert code == 1
    assert 'h1: basis "answers:no_such_answer" is not an answer id' in err


def test_no_answers_yaml_makes_each_answer_id_unknown(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "answers.yaml").unlink()
    write(repo, good())
    code, _, err = facts_fit(repo, capsys)
    assert code == 1
    assert 'h1: basis "answers:current_location" is not an answer id' in err


def test_answers_yaml_is_read_only_when_a_basis_needs_it(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "answers.yaml").write_text("schema_version: 2\n", encoding="utf-8")
    items = good()
    items[0]["basis"] = "example-u-ba"
    write(repo, items)
    assert facts_fit(repo, capsys)[0] == 0
    write(repo, good())
    code, _, err = facts_fit(repo, capsys)
    assert code == 2
    assert "answers.yaml is not valid against the answers schema" in err


# --- Rules 3 to 7 and the rating -------------------------------------------------


def test_met_with_no_basis_counts_as_unknown_with_a_warning(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items = good()
    items[0] = hard("h1", "Two days each week in the office.", "not_met")
    write(repo, items)
    code, out, _ = facts_fit(repo, capsys)
    assert code == 0
    data = fit_json(repo)
    assert data["requirements"][0]["counted_as"] == "unknown"
    assert data["requirements"][0]["basis"] is None
    assert data["rating"] == "green"
    assert data["hard_not_met"] == []
    assert data["hard_unknown"] == [{"id": "h1", "quote": items[0]["quote"]}]
    assert data["warnings"][0].startswith("h1: the status not_met has no basis")
    assert 'hard unknown: h1 "Two days each week in the office."' in out


def test_an_unknown_hard_requirement_does_not_change_the_colour(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items = good()[1:]
    write(repo, items)
    assert facts_fit(repo, capsys)[0] == 0
    without = fit_json(repo)["rating"]
    write(repo, [hard("h2", "The right to work in the country.", "unknown"), *items])
    assert facts_fit(repo, capsys)[0] == 0
    data = fit_json(repo)
    assert data["rating"] == without == "green"
    assert data["hard_unknown"] == [
        {"id": "h2", "quote": "The right to work in the country."}
    ]


def test_a_hard_requirement_that_is_not_met_gives_red(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items = good()
    items[0]["status"] = "not_met"
    items[-1]["evidence"] = ["acme-checkout"]  # full coverage
    write(repo, items)
    code, out, _ = facts_fit(repo, capsys)
    assert code == 0
    data = fit_json(repo)
    assert data["coverage"] == 1
    assert data["rating"] == "red"
    assert data["hard_not_met"] == [{"id": "h1", "quote": items[0]["quote"]}]
    assert out.splitlines()[:2] == [
        f"facts fit: {RUN} is red. 7 of 7 wanted requirements have evidence.",
        'hard not met: h1 "Two days each week in the office."',
    ]


DEFAULT = FitSettings()


@pytest.mark.parametrize(
    ("count", "total", "settings", "colour"),
    [
        (7, 10, DEFAULT, "green"),  # exactly at green
        (6, 10, DEFAULT, "yellow"),  # one step below green
        (4, 10, DEFAULT, "yellow"),  # exactly at yellow
        (3, 10, DEFAULT, "red"),  # one step below yellow
        (2, 10, FitSettings(0.7, 0.2), "yellow"),  # 0.2 as a float is above 2/10
        (1, 10, FitSettings(0.7, 0.2), "red"),
        (1, 10, FitSettings(0.1, 0.1), "green"),  # 0.1 as a float is above 1/10
        (5, 10, FitSettings(0.5, 0.5), "green"),  # yellow equal to green
        (4, 10, FitSettings(0.5, 0.5), "red"),
        (10, 10, FitSettings(1.0, 0.0), "green"),
        (0, 10, FitSettings(1.0, 0.0), "yellow"),
    ],
)
def test_the_limits_of_the_colours(
    count: int, total: int, settings: FitSettings, colour: str
) -> None:
    assert rating(False, count, total, settings) == colour
    assert rating(True, count, total, settings) == "red"


def _ten(with_evidence: int) -> list[Any]:
    quotes = [
        "Design flows in Figma.",
        "Build prototypes in React.",
        "Write clear notes for each design decision.",
        "Test new ideas with users.",
        "Keep the design system up to date.",
        "Know Swift for the mobile app.",
        "the café menu",
        "make signup forms",
        "run user research with shop owners",
        "The right to work in the country.",
    ]
    return [
        wanted(f"w{i}", q, *(["skill:Figma"] if i < with_evidence else []))
        for i, q in enumerate(quotes)
    ]


@pytest.mark.parametrize(
    ("with_evidence", "colour"),
    [(7, "green"), (6, "yellow"), (4, "yellow"), (3, "red")],
)
def test_the_command_uses_the_limits_of_facts_toml(
    repo: Path, capsys: pytest.CaptureFixture[str], with_evidence: int, colour: str
) -> None:
    write(repo, _ten(with_evidence))
    assert facts_fit(repo, capsys)[0] == 0
    assert fit_json(repo)["rating"] == colour


def test_fewer_than_5_wanted_requirements_gives_a_warning(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(repo, good()[:5])
    code, out, _ = facts_fit(repo, capsys)
    assert code == 0
    assert fit_json(repo)["warnings"][-1].startswith("only 4 wanted requirements.")
    assert "warning: only 4 wanted requirements." in out
    write(repo, good()[:6])
    assert facts_fit(repo, capsys)[0] == 0
    assert fit_json(repo)["warnings"] == []


def test_zero_wanted_requirements_is_invalid_input(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(repo, good()[:1])
    code, out, err = facts_fit(repo, capsys)
    assert (code, out) == (2, "")
    assert "has no wanted requirement. List each skill and each experience" in err
    assert no_fit_json(repo)


# --- Invalid input: the schema ------------------------------------------------------


def _change(change: str) -> Any:
    items = good()
    top: dict[str, Any] = {"schema_version": 1, "requirements": items}
    h, w = items[0], items[1]
    if change == "top key":
        top["extra"] = 1
    elif change == "requirement key":
        w["note"] = "x"
    elif change == "kind":
        w["kind"] = "nice"
    elif change == "status":
        h["status"] = "maybe"
    elif change == "evidence on hard":
        h["evidence"] = []
    elif change == "basis on wanted":
        w["basis"] = "acme"
    elif change == "status on wanted":
        w["status"] = "met"
    elif change == "no status on hard":
        del h["status"]
    elif change == "no evidence on wanted":
        del w["evidence"]
    elif change == "empty quote":
        w["quote"] = ""
    elif change == "space quote":
        w["quote"] = " \n\t "
    elif change == "soft hyphen quote":
        w["quote"] = "­"
    elif change == "long quote":
        w["quote"] = "a" * 2001
    elif change == "schema version":
        top["schema_version"] = 2
    elif change == "no requirements":
        top["requirements"] = []
    elif change == "bad id":
        w["id"] = "w 1"
    elif change == "id with a line end":
        w["id"] = "w1\n"
    elif change == "duplicate id":
        w["id"] = "h1"
    elif change == "evidence twice":
        w["evidence"] = ["skill:Figma", "skill:Figma"]
    elif change == "bad evidence":
        w["evidence"] = ["Acme Checkout"]
    elif change == "bad basis":
        h["basis"] = "answers:Current Location"
    elif change == "lone surrogate":
        return json.dumps(top).replace("Design flows", "\\ud800 Design flows")
    elif change == "duplicate key":
        return json.dumps(top).replace(
            '"kind": "hard",', '"kind": "hard", "kind": "hard",'
        )
    elif change == "not json":
        return "{"
    return top


@pytest.mark.parametrize(
    "change",
    [
        "top key",
        "requirement key",
        "kind",
        "status",
        "evidence on hard",
        "basis on wanted",
        "status on wanted",
        "no status on hard",
        "no evidence on wanted",
        "empty quote",
        "space quote",
        "soft hyphen quote",
        "long quote",
        "schema version",
        "no requirements",
        "bad id",
        "id with a line end",
        "duplicate id",
        "evidence twice",
        "bad evidence",
        "bad basis",
        "lone surrogate",
        "duplicate key",
        "not json",
    ],
)
def test_each_schema_refusal_is_invalid_input(
    repo: Path, capsys: pytest.CaptureFixture[str], change: str
) -> None:
    write(repo, top=_change(change))
    code, out, err = facts_fit(repo, capsys)
    assert (code, out) == (2, ""), err
    assert err.startswith("facts fit: ")
    assert no_fit_json(repo)


def test_a_key_that_is_not_permitted_names_the_kind(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(repo, top=_change("evidence on hard"))
    _, _, err = facts_fit(repo, capsys)
    assert "requirement h1: evidence is not permitted on a hard requirement." in err


def test_a_missing_requirements_json_is_invalid_input(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = facts_fit(repo, capsys)
    assert code == 2
    assert "requirements.json does not exist. Read job.md and write" in err


def test_a_large_requirements_json_is_invalid_input(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(repo, top=" " * (256 * 1024 + 1))
    code, _, err = facts_fit(repo, capsys)
    assert code == 2
    assert "is larger than 256 KiB" in err


# --- The run folder -----------------------------------------------------------------


def test_a_failed_run_leaves_an_old_fit_json(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(repo, good())
    assert facts_fit(repo, capsys)[0] == 0
    old = (run_dir(repo) / FIT_JSON).read_bytes()
    items = good()
    items[1]["quote"] = "Not in the post."
    write(repo, items)
    assert facts_fit(repo, capsys)[0] == 1
    write(repo, top=_change("kind"))
    assert facts_fit(repo, capsys)[0] == 2
    assert (run_dir(repo) / FIT_JSON).read_bytes() == old
    assert sorted(p.name for p in run_dir(repo).iterdir()) == [
        "fit.json",
        "job.md",
        "requirements.json",
        "run.yaml",
    ]


def test_a_run_outside_the_data_root_is_refused(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(repo, good())
    other = tmp_path / "elsewhere" / RUN
    shutil.copytree(run_dir(repo), other)
    code = cli.main(["fit", str(other), "--repo", str(repo)])
    err = capsys.readouterr().err
    assert code == 2
    assert "is not a run folder of the data root" in err
    assert not (other / FIT_JSON).exists()


def test_a_linked_run_folder_is_refused(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(repo, good())
    link = repo / "applications" / "2026-12-01-link-run"
    link.symlink_to(run_dir(repo), target_is_directory=True)
    assert cli.main(["fit", str(link), "--repo", str(repo)]) == 2
    assert no_fit_json(repo)


@pytest.mark.parametrize("name", ["job.md", "requirements.json", "fit.json"])
def test_a_symbolic_link_in_the_run_folder_is_refused(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    write(repo, good())
    path = run_dir(repo) / name
    target = tmp_path / f"real-{name}"
    if path.exists():
        shutil.move(path, target)
    else:
        target.write_text("{}", encoding="utf-8")
    os.symlink(target, path)
    code, _, err = facts_fit(repo, capsys)
    assert code == 2
    assert f"{name} is a symbolic link" in err
    assert path.is_symlink()


def test_a_submitted_run_is_refused(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(repo, good())
    path = run_dir(repo) / "run.yaml"
    meta: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    meta["status"] = "submitted"
    path.write_text(yaml.safe_dump(meta), encoding="utf-8")
    code, out, err = facts_fit(repo, capsys)
    assert (code, out) == (1, "")
    assert "is already submitted" in err
    assert no_fit_json(repo)


@pytest.mark.parametrize(
    "table",
    [
        "[fit]\ngreen = true\n",
        '[fit]\ngreen = "0.7"\n',
        "[fit]\ngreen = nan\n",
        "[fit]\ngreen = 1.5\n",
        "[fit]\nyellow = -0.1\n",
        "[fit]\ngreen = 0.3\n",  # the default yellow 0.4 is above it
        "[fit]\ngreen = 0.5\nyellow = 0.6\n",
        "fit = 1\n",
    ],
)
def test_bad_fit_settings_are_invalid_input(
    repo: Path, capsys: pytest.CaptureFixture[str], table: str
) -> None:
    path = repo / "facts.toml"
    text = path.read_text(encoding="utf-8")
    text = text.replace("[fit]\ngreen = 0.7\nyellow = 0.4\n", "")
    path.write_text(table + text, encoding="utf-8")
    write(repo, good())
    code, _, err = facts_fit(repo, capsys)
    assert code == 2
    assert "fit" in err
    assert no_fit_json(repo)
