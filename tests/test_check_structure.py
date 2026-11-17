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
