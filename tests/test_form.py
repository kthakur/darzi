"""`facts answers`, the `[form]` settings and the host compare (W-9).

Synthetic data only: the answers in tests/data/answers.yaml and copies of them.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from darzi.cli import main
from darzi.config import FormSettings, host_name, load_config
from darzi.facts.repo import InputError, load_rules
from darzi.runs.form import apply_url, check_answers, load_answers, run_companies
from darzi.schemas import ANSWERS, load_schema

from .conftest import DATA

Json = dict[str, Any]
COMPANIES = {"Acme", "Globex"}


@pytest.fixture
def answers() -> Json:
    return load_answers(DATA)


def _check(data: Json, companies: set[str] | None = None) -> list[Any]:
    rules: Json = load_rules(DATA)
    return check_answers(
        data, load_schema(ANSWERS), rules, COMPANIES if companies is None else companies
    )


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    shutil.copytree(DATA, root)
    return root


# --- facts answers ----------------------------------------------------------------


def test_the_synthetic_answers_pass(answers: Json) -> None:
    assert _check(answers) == []


def test_the_schema_refuses_an_extra_field(answers: Json) -> None:
    data = copy.deepcopy(answers)
    data["answers"][0]["note"] = "x"
    findings = _check(data)
    assert findings
    assert all(f.rule == "D-40" and f.severity == "error" for f in findings)


def test_an_id_two_times(answers: Json) -> None:
    data = copy.deepcopy(answers)
    data["answers"].append(dict(data["answers"][0]))
    assert [f.message for f in _check(data)] == ["This id is in the file twice."]


@pytest.mark.parametrize("text", ["I like Acme.", "acme, mostly", "ACME"])
def test_an_answer_that_names_a_run_company(answers: Json, text: str) -> None:
    data = copy.deepcopy(answers)
    data["answers"][2]["answer"] = text
    findings = _check(data)
    assert len(findings) == 1
    assert "'Acme'" in findings[0].message


def test_a_company_name_inside_a_word_is_not_a_finding(answers: Json) -> None:
    data = copy.deepcopy(answers)
    data["answers"][2]["answer"] = "Acmeville"
    assert _check(data) == []


@pytest.mark.parametrize(
    "text", ["other.person@example.org", "212-555-0123", "call 312-555-0199"]
)
def test_only_public_contact_data(answers: Json, text: str) -> None:
    data = copy.deepcopy(answers)
    data["answers"][4]["answer"] = text
    findings = _check(data)
    assert findings
    assert all(f.location.startswith("answers: ") for f in findings)


def test_run_companies(tmp_path: Path) -> None:
    root = _root(tmp_path)
    names = run_companies(root)
    assert names == {
        yaml.safe_load(p.read_text(encoding="utf-8"))["company"]
        for p in (DATA / "applications").glob("*/run.yaml")
    }
    assert run_companies(tmp_path / "empty") == set()


def _answers(root: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> int:
    code = main(["answers", "--repo", str(root), *extra])
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err
    return code


def test_the_answers_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    assert _answers(root, capsys) == 0
    path = root / "answers.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("Springfield, ST", "Near Globex"), encoding="utf-8")
    assert main(["answers", "--repo", str(root), "--json"]) == 1
    report = yaml.safe_load(capsys.readouterr().out)
    assert report["schema_version"] == 1
    assert report["errors"] == 1


@pytest.mark.parametrize(
    "text",
    [
        None,
        "schema_version: 1\nanswers: [\n",
        "- a list\n",
        "a: &x [1]\nb: *x\n",
        "x" * (300 * 1024),
    ],
)
def test_answers_that_cannot_be_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str | None
) -> None:
    root = _root(tmp_path)
    path = root / "answers.yaml"
    if text is None:
        path.unlink()
    else:
        path.write_text(text, encoding="utf-8")
    assert _answers(root, capsys) == 2


# --- [form] and the host compare ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("jobs.example.com", "jobs.example.com"),
        ("Jobs.Example.COM.", "jobs.example.com"),
        ("\uff4a\uff4f\uff42\uff53.example.com", "jobs.example.com"),
        ("jobs\uff0eexample.com", "jobs.example.com"),
        ("b\u00fccher.example", "xn--bcher-kva.example"),
        ("xn--bcher-kva.example", "xn--bcher-kva.example"),
        ("a..b", None),
        ("example.com..", None),
        ("-a.example", None),
        ("192.0.2.1", None),
        ("", None),
        ("a_b.example", None),
    ],
)
def test_host_name(text: str, expected: str | None) -> None:
    assert host_name(text) == expected


# A browser reads each of these hosts as an IP address: the last label must start
# with a letter, not only be "not all digits".
IP_LIKE = [
    "0x7f000001",
    "192.0.2.0x1",
    "192.0.2.0X1",
    "0177.0.0.1",
    "example.0177",
    "example.1a",
    "16909060",
]


@pytest.mark.parametrize("host", IP_LIKE)
def test_a_host_that_a_browser_reads_as_an_ip_address(host: str) -> None:
    assert host_name(host) is None


@pytest.mark.parametrize("host", IP_LIKE)
def test_an_ip_like_link_is_refused(tmp_path: Path, host: str) -> None:
    form = FormSettings(())
    with pytest.raises(InputError, match="plain http or https link"):
        apply_url({"apply_url": f"https://{host}/x"}, tmp_path / "run", form)


@pytest.mark.parametrize("host", IP_LIKE)
def test_an_ip_like_blocked_entry_is_refused(tmp_path: Path, host: str) -> None:
    with pytest.raises(InputError, match="not a plain"):
        load_config(_toml(tmp_path, f'[form]\nblocked_hosts = ["{host}"]\n'))


# The German sharp s and the Greek final sigma: the older IDNA rules (the Python
# codec) and the newer rules (httpx, and a browser) give different ASCII names.
SHARP_S = "stra\u00dfe.example"
SHARP_S_PUNYCODE = "xn--strae-oqa.example"
FINAL_SIGMA = "\u03c3\u03bf\u03c2.example"
FINAL_SIGMA_PUNYCODE = "xn--0xagb.example"


@pytest.mark.parametrize("host", [SHARP_S, FINAL_SIGMA, "STRA\u1e9eE.example"])
def test_a_name_that_the_two_idna_rules_read_differently(host: str) -> None:
    assert host_name(host) is None


@pytest.mark.parametrize(
    ("entry", "link"),
    [
        (SHARP_S, SHARP_S_PUNYCODE),
        (SHARP_S_PUNYCODE, SHARP_S),
        (FINAL_SIGMA, FINAL_SIGMA_PUNYCODE),
        (FINAL_SIGMA_PUNYCODE, FINAL_SIGMA),
    ],
)
def test_an_idna_mismatch_is_never_passed(
    tmp_path: Path, entry: str, link: str
) -> None:
    root = _toml(tmp_path, f'[form]\nblocked_hosts = ["{entry}"]\n')
    url = {"apply_url": f"https://{link}/apply"}
    if not entry.isascii():
        # The entry in Unicode is refused: no blocked list can hold it.
        with pytest.raises(InputError, match="not a plain"):
            load_config(root)
        return
    form = load_config(root).form
    assert form == FormSettings((entry,))
    # The link in Unicode is refused as an invalid link.
    with pytest.raises(InputError, match="plain http or https link"):
        apply_url(url, tmp_path / "run", form)


def test_a_punycode_link_of_a_punycode_entry_is_blocked(tmp_path: Path) -> None:
    form = FormSettings((SHARP_S_PUNYCODE,))
    url = {"apply_url": f"https://jobs.{SHARP_S_PUNYCODE.upper()}/apply"}
    with pytest.raises(InputError, match="blocks"):
        apply_url(url, tmp_path / "run", form)


def test_a_name_that_both_idna_rules_read_the_same_passes() -> None:
    assert host_name("b\u00fccher.example") == "xn--bcher-kva.example"


def _toml(tmp_path: Path, form: str) -> Path:
    (tmp_path / "facts.toml").write_text(form, encoding="utf-8")
    return tmp_path


def test_the_form_settings(tmp_path: Path) -> None:
    assert load_config(DATA).form == FormSettings(
        ("jobs.example.com", "board.example.com")
    )
    root = _toml(tmp_path, '[form]\nblocked_hosts = ["Jobs.Example.com."]\n')
    assert load_config(root).form == FormSettings(("jobs.example.com",))
    assert load_config(_toml(tmp_path, "[form]\nblocked_hosts = []\n")).form == (
        FormSettings(())
    )
    assert load_config(_toml(tmp_path, "[owner]\n")).form is None


@pytest.mark.parametrize(
    "form",
    [
        "form = 1\n",
        "[form]\n",
        '[form]\nblocked_hosts = "jobs.example.com"\n',
        "[form]\nblocked_hosts = [1]\n",
        '[form]\nblocked_hosts = ["https://jobs.example.com"]\n',
        '[form]\nblocked_hosts = ["jobs.example.com/apply"]\n',
        '[form]\nblocked_hosts = ["*.example.com"]\n',
        '[form]\nblocked_hosts = ["jobs.example.com:443"]\n',
        '[form]\nblocked_hosts = [" jobs.example.com"]\n',
        '[form]\nblocked_hosts = [""]\n',
    ],
)
def test_a_bad_form_table(tmp_path: Path, form: str) -> None:
    with pytest.raises(InputError):
        load_config(_toml(tmp_path, form))


def test_the_compare_is_on_whole_labels(tmp_path: Path) -> None:
    form = FormSettings(("jobs.example.com",))
    run = tmp_path / "run"
    ok = "https://notjobs.example.com/x"
    assert apply_url({"apply_url": ok}, run, form) == ok
    with pytest.raises(InputError, match="blocks"):
        apply_url({"apply_url": "https://x.jobs.example.com/"}, run, form)
    with pytest.raises(InputError, match=r"no \[form\] table"):
        apply_url({"apply_url": ok}, run, None)
