"""`facts check`: the structure rules R-8 to R-12, R-14, R-15 and R-19."""

import copy
from typing import Any

import pytest

from darzi.facts.check_structure import build_context

from .checking import (
    ACME,
    BASE,
    BASE_MAP,
    FREELANCE,
    GLOBEX,
    MASTER,
    RULES,
    findings,
    found,
    master_with,
    tailored,
)


@pytest.fixture
def base() -> dict[str, Any]:
    return tailored()


def test_each_entry_pairs_with_its_master_entry() -> None:
    ctx = build_context(tailored(), BASE, BASE_MAP, RULES, MASTER)
    assert [(p.location, p.master_id, p.master_kind) for p in ctx.pairs] == [
        ("workExperience[0]", "acme", "roles"),
        ("workExperience[1]", "globex", "roles"),
        ("workExperience[2]", "freelance-design", "roles"),
        ("workExperience[3]", "vandelay", "roles"),
        ("personalProjects[0]", "plant-app", "projects"),
        ("education[0]", "example-u-ba", "education"),
        ("education[1]", "example-state-hci", "education"),
    ]
    assert ctx.pairs[0].bullet_ids == (
        "acme-checkout",
        "acme-design-system",
        "acme-research",
    )


# R-8 titles


def test_r8_alt_title_passes(base: dict[str, Any]) -> None:
    base["workExperience"][ACME]["title"] = "Product Designer"
    assert found(base) == []


def test_r8_position_title_and_dates_pass(base: dict[str, Any]) -> None:
    base["workExperience"][ACME]["years"] = "Jan 2023 - Present"
    assert found(base) == []
    base["workExperience"][ACME]["title"] = "Product Designer"
    base["workExperience"][ACME]["years"] = "Mar 2021 - Dec 2022"
    assert found(base) == []


ALT_ONLY = "Interaction Designer"  # an alt title that is not a position title
POSITION_ONLY = "Design Associate"  # a position title that is not an alt title


def _acme_titles(*, alt: bool, positions: bool) -> dict[str, Any]:
    """Return facts where the Acme alt title and position title differ (R-8)."""

    def change(entries: dict[str, Any]) -> None:
        acme = entries["acme"]
        acme["alt_titles"] = [ALT_ONLY] if alt else []
        acme["positions"][0]["title"] = POSITION_ONLY
        if not positions:
            del acme["positions"]

    master = master_with(change)
    acme = next(r for r in master["roles"] if r["id"] == "acme")
    assert POSITION_ONLY not in acme["alt_titles"]
    assert ALT_ONLY not in [p["title"] for p in acme.get("positions") or []]
    return master


def test_r8_alt_title_alone_passes(base: dict[str, Any]) -> None:
    base["workExperience"][ACME]["title"] = ALT_ONLY
    assert found(base, master=_acme_titles(alt=True, positions=True)) == []
    assert found(base, master=_acme_titles(alt=False, positions=True)) == [
        ("R-8", "workExperience[0].title")
    ]


def test_r8_position_title_alone_passes(base: dict[str, Any]) -> None:
    base["workExperience"][ACME]["title"] = POSITION_ONLY
    assert found(base, master=_acme_titles(alt=True, positions=True)) == []
    assert found(base, master=_acme_titles(alt=True, positions=False)) == [
        ("R-8", "workExperience[0].title")
    ]


def test_r8_credential_without_the_no_degree_suffix(base: dict[str, Any]) -> None:
    """A credential that ends in "(no degree)" may show without those words."""
    degree = base["education"][1]["degree"]
    assert degree == "Coursework, Human-Computer Interaction"

    def credential(suffix: str) -> dict[str, Any]:
        return master_with(
            lambda e: e["example-state-hci"].update(credential=f"{degree} {suffix}")
        )

    assert found(base, master=credential("(no degree)")) == []
    base["education"][1]["degree"] = f"{degree} (no degree)"
    assert found(base, master=credential("(no degree)")) == []
    base["education"][1]["degree"] = degree
    # Control: only this suffix may drop.
    assert found(base, master=credential("(no diploma)")) == [
        ("R-8", "education[1].degree")
    ]


def test_r8_project_alt_name_passes(base: dict[str, Any]) -> None:
    base["personalProjects"][0]["name"] = "Leafy"
    assert found(base) == []


def test_r8_title_changed(base: dict[str, Any]) -> None:
    base["workExperience"][ACME]["title"] = "Head of Design"
    assert found(base) == [("R-8", "workExperience[0].title")]


def test_r8_project_name_changed(base: dict[str, Any]) -> None:
    base["personalProjects"][0]["name"] = "Plant Doctor App"
    assert found(base) == [("R-8", "personalProjects[0].name")]


def test_r8_education_degree_changed(base: dict[str, Any]) -> None:
    base["education"][0]["degree"] = "B.S. Graphic Design"
    assert found(base) == [("R-8", "education[0].degree")]


# R-9 employer of record


def test_r9_display_names_pass(base: dict[str, Any]) -> None:
    assert base["workExperience"][GLOBEX]["company"] == (
        "Globex (contract through Initech Staffing)"
    )
    assert base["education"][1]["institution"] == "Example State University"
    base["workExperience"][ACME]["company"] = "Acme Corporation"  # the org name
    assert found(base) == []


def test_r9_org_not_in_the_facts(base: dict[str, Any]) -> None:
    base["workExperience"][ACME]["company"] = "A design company"
    assert found(base) == [("R-9", "workExperience[0].company")]


@pytest.mark.parametrize("company", ["Globex Corporation", "Globex"])
def test_r9_required_words(base: dict[str, Any], company: str) -> None:
    """Even a display name must keep the employer of record."""
    master = master_with(lambda e: e["globex"].update(display_org="Globex"))
    base["workExperience"][GLOBEX]["company"] = company
    assert found(base, master=master) == [("R-9", "workExperience[1].company")]
    (finding,) = [f for f in findings(base, master=master) if f.rule == "R-9"]
    assert finding.message == (
        "Must show who employed or placed the owner: one of ['Initech', 'contract']."
    )


def test_r9_institution(base: dict[str, Any]) -> None:
    base["education"][0]["institution"] = "Springfield College"
    assert found(base) == [("R-9", "education[0].institution")]


# R-10 dates and not-working periods


@pytest.mark.parametrize(
    "years",
    [
        "2019 - 2021",
        "Feb 2019 – Jan 2021",
        "Feb 2019 — Jan 2021",
        "Feb 2019 to Jan 2021",
        "February 2019 - January 2021",
    ],
)
def test_r10_same_dates_other_format(base: dict[str, Any], years: str) -> None:
    base["workExperience"][GLOBEX]["years"] = years
    assert found(base) == []


@pytest.mark.parametrize(
    "years", ["Feb 2019 - Jan 2020", "2019 - 2022", "Feb 2019", "soon"]
)
def test_r10_wrong_dates(base: dict[str, Any], years: str) -> None:
    base["workExperience"][GLOBEX]["years"] = years
    assert found(base) == [("R-10", "workExperience[1].years")]


def test_r10_single_date_is_the_end_month_of_a_range(base: dict[str, Any]) -> None:
    """One shown date passes only when the facts have one date."""
    base["workExperience"][GLOBEX]["years"] = "Jan 2021"  # the end month alone
    assert found(base) == [("R-10", "workExperience[1].years")]
    base["workExperience"][GLOBEX]["years"] = "Feb 2019 - Jan 2021"
    master = master_with(
        lambda e: e["freelance-design"].update(start="2017-04", end="2017-04")
    )
    base["workExperience"][FREELANCE]["years"] = "Apr 2017"
    assert found(base, master=master) == []
    base["workExperience"][FREELANCE]["years"] = "May 2017"  # control
    assert found(base, master=master) == [("R-10", "workExperience[2].years")]


def test_r10_year_only_bounds_are_the_narrowest_span(base: dict[str, Any]) -> None:
    """A year-only start is its last month; a year-only end is its first month.

    The first period ends in Jan 2019 and the second period is Feb 2021. A start
    in 2019 and an end in 2021 can be clear of both, so neither is an overlap.
    """
    master = master_with(lambda e: e["globex"].update(start="2019"))
    base["workExperience"][GLOBEX]["years"] = "2019 - Jan 2021"
    assert found(base, master=master) == []
    master = master_with(lambda e: e["globex"].update(end="2021"))
    base["workExperience"][GLOBEX]["years"] = "Feb 2019 - 2021"
    assert found(base, master=master) == []
    # Control: a year-only start in the first period is an overlap.
    master = master_with(lambda e: e["globex"].update(start="2018"))
    base["workExperience"][GLOBEX]["years"] = "2018 - Jan 2021"
    assert found(base, master=master) == [("R-10", "workExperience[1]")]


def test_r10_overlap(base: dict[str, Any]) -> None:
    master = master_with(lambda e: e["globex"].update(start="2018-06"))
    base["workExperience"][GLOBEX]["years"] = "Jun 2018 - Jan 2021"
    assert found(base, master=master) == [("R-10", "workExperience[1]")]
    base["workExperience"][GLOBEX]["years"] = "Feb 2019 - Jan 2021"
    assert found(base) == []  # control: the facts have no overlap


def test_r10_reads_each_period(base: dict[str, Any]) -> None:
    """The Globex end moves into the second period; the first period is no match."""
    periods = RULES["not_working_periods"]["periods"]
    assert [(p["from"], p["to"]) for p in periods] == [
        ("2018-01", "2019-01"),
        ("2021-02", "2021-02"),
    ]
    master = master_with(lambda e: e["globex"].update(end="2021-02"))
    base["workExperience"][GLOBEX]["years"] = "Feb 2019 - Feb 2021"
    assert found(base, master=master) == [("R-10", "workExperience[1]")]
    for order in (periods[::-1], periods[:1]):
        rules = copy.deepcopy(RULES)
        rules["not_working_periods"]["periods"] = copy.deepcopy(order)
        expected = [("R-10", "workExperience[1]")] if len(order) == 2 else []
        assert found(base, master=master, rules=rules) == expected


def test_r10_year_only_end_overlaps(base: dict[str, Any]) -> None:
    """A role that ends in a year that a period fills overlaps it."""
    master = master_with(lambda e: e["freelance-design"].update(end="2018"))
    base["workExperience"][FREELANCE]["years"] = "2016 - 2018"
    assert found(base, master=master) == [("R-10", "workExperience[2]")]


def test_r10_unmapped_entry(base: dict[str, Any]) -> None:
    the_map = copy.deepcopy(BASE_MAP)
    the_map["entries"] = [e for e in the_map["entries"] if e["id"] != "globex"]
    assert found(base, the_map=the_map) == [("R-10", "workExperience[1]")]


# R-11 exclude_from


def test_r11_excluded_for_other_type_passes(base: dict[str, Any]) -> None:
    master = master_with(lambda e: e["acme"].update(exclude_from=["corporate-tech"]))
    assert found(base, master=master) == []


def test_r11_entry_excluded(base: dict[str, Any]) -> None:
    master = master_with(lambda e: e["acme"].update(exclude_from=["ux"]))
    assert found(base, master=master) == [("R-11", "workExperience[0]")]


def test_r11_bullet_excluded_for_all(base: dict[str, Any]) -> None:
    def exclude(entries: dict[str, Any]) -> None:
        for bullet in entries["acme"]["bullets"]:
            if bullet["id"] == "acme-checkout":
                bullet["exclude_from"] = ["all"]

    assert found(base, master=master_with(exclude)) == [("R-11", "workExperience[0]")]


# R-12 same shape


def test_r12_reorder_passes(base: dict[str, Any]) -> None:
    for entry in base["workExperience"]:
        entry["description"].reverse()
    base["workExperience"].reverse()
    assert found(base) == []


def test_r12_added_entry(base: dict[str, Any]) -> None:
    extra = copy.deepcopy(base["workExperience"][ACME])
    extra["id"] = 99
    base["workExperience"].append(extra)
    assert found(base) == [("R-12", "workExperience[4]")]


def test_r12_removed_bullet(base: dict[str, Any]) -> None:
    base["workExperience"][GLOBEX]["description"].pop()
    assert found(base) == [("R-12", "workExperience[1].description")]


def test_r12_removed_entry(base: dict[str, Any]) -> None:
    del base["personalProjects"][0]
    assert found(base) == [("R-12", "personalProjects[id=1]")]


# R-14 never a company, never salaried


def test_r14_freelance_entry_passes(base: dict[str, Any]) -> None:
    assert base["workExperience"][FREELANCE]["company"] == "Freelance design"
    assert found(base, only="R-14") == []


def test_r14_never_a_company(base: dict[str, Any]) -> None:
    base["summary"] += " Runs Test Person Design LLC."
    assert found(base, only="R-14") == [("R-14", "summary")]


def test_r14_freelance_group_shown_as_job(base: dict[str, Any]) -> None:
    base["workExperience"][FREELANCE]["company"] = "Shop Sites Studio"
    assert found(base, only="R-14") == [("R-14", "workExperience[2]")]
    base["workExperience"][FREELANCE]["company"] = "Shop Sites Studio (freelance)"
    assert found(base, only="R-14") == []


# R-15 unpaid projects


def test_r15_independent_role_may_be_work(base: dict[str, Any]) -> None:
    """freelance-design is a role of type independent, not an unpaid project."""
    assert base["workExperience"][FREELANCE]["company"] == "Freelance design"
    assert found(base, only="R-15") == []


def test_r15_paid_word(base: dict[str, Any]) -> None:
    base["personalProjects"][0]["role"] = "Independent (client work)"
    assert found(base, only="R-15") == [("R-15", "personalProjects[0]")]


def test_r15_unpaid_project_as_work() -> None:
    base = copy.deepcopy(BASE)
    project = base["personalProjects"].pop(0)
    base["workExperience"].append(
        {
            "id": 50,
            "title": project["name"],
            "company": project["role"],
            "years": project["years"],
            "description": project["description"],
        }
    )
    the_map = copy.deepcopy(BASE_MAP)
    the_map["entries"] = [
        {**e, "section": "workExperience", "rm_id": 50} if e["id"] == "plant-app" else e
        for e in the_map["entries"]
    ]
    resume = copy.deepcopy(base)
    assert found(resume, base=base, the_map=the_map, only="R-15") == [
        ("R-15", "workExperience[4]")
    ]


# R-19 skills


def test_r19_reordered_skills_pass(base: dict[str, Any]) -> None:
    base["additional"]["technicalSkills"].reverse()
    assert found(base) == []


def test_r19_unknown_skill(base: dict[str, Any]) -> None:
    base["additional"]["technicalSkills"].append("Sketch")
    assert found(base) == [("R-19", "additional.technicalSkills[4]")]


def test_r19_removed_skill(base: dict[str, Any]) -> None:
    master = copy.deepcopy(MASTER)
    master["skills"].append(
        {"name": "Flash", "tags": ["web"], "sources": ["confirmed"]}
    )
    base["additional"]["technicalSkills"].append("Flash")
    for facts in (master, MASTER):  # also when it is a skill in the facts
        errors = [f for f in findings(base, master=facts) if f.severity == "error"]
        assert [(f.rule, f.location, f.text) for f in errors] == [
            ("R-19", "additional.technicalSkills[4]", "Flash")
        ]
        assert errors[0].message == "Skill was removed by the owner. Remove it."
