"""The 14 edit cases of docs/SPEC.md section 10, as edits of the synthetic base.

Each function takes a copy of the base (the export of the ux selection) and
returns it with one edit. `EXPECTED` gives the error rules of each case, as the
table in docs/SPEC.md section 10 gives them.
"""

from collections.abc import Callable
from typing import Any

from .checking import ACME, GLOBEX

Resume = dict[str, Any]


def case_01_reorder(r: Resume) -> Resume:
    """Change the order of the bullets of one role."""
    r["workExperience"][ACME]["description"].reverse()
    return r


def case_02_reword(r: Resume) -> Resume:
    """Reword one bullet; the facts stay the same."""
    r["workExperience"][ACME]["description"][0] = (
        "Designed a new checkout flow, which cut drop-off by 12%."
    )
    return r


def case_03_title(r: Resume) -> Resume:
    """Change a job title."""
    r["workExperience"][ACME]["title"] = "Lead Product Designer"
    return r


def case_04_employer_of_record(r: Resume) -> Resume:
    """Show the org name with no employer-of-record words."""
    r["workExperience"][GLOBEX]["company"] = "Globex Corporation"
    return r


def case_05_number(r: Resume) -> Resume:
    """Add a number that the source does not have."""
    text = r["workExperience"][ACME]["description"][0]
    r["workExperience"][ACME]["description"][0] = text.replace("12%", "18%")
    return r


def case_06_years(r: Resume) -> Resume:
    """Add a years-of-experience count to the summary."""
    r["summary"] = "Product Designer with ten years of experience."
    return r


def case_07_led_team(r: Resume) -> Resume:
    """Add a lead claim that no source states."""
    r["workExperience"][ACME]["description"][0] = (
        "Led the design team that designed a new checkout flow that cut "
        "drop-off by 12%."
    )
    return r


def case_08_banned_word(r: Resume) -> Resume:
    """Add a banned word."""
    r["workExperience"][ACME]["description"][0] = (
        "Spearheaded a new checkout flow that cut drop-off by 12%."
    )
    return r


def case_09_remove_bullet(r: Resume) -> Resume:
    """Remove a bullet."""
    r["workExperience"][ACME]["description"].pop()
    return r


def case_10_remove_role(r: Resume) -> Resume:
    """Remove a role."""
    del r["workExperience"][GLOBEX]
    return r


def case_11_new_technology(r: Resume) -> Resume:
    """Add a bullet with new technology names."""
    r["workExperience"][ACME]["description"].append(
        "Built the checkout service in Python on Kubernetes."
    )
    return r


def case_12_new_skill(r: Resume) -> Resume:
    """Add a skill that is not in the fact file."""
    r["additional"]["technicalSkills"].append("Sketch")
    return r


def case_13_email(r: Resume) -> Resume:
    """Change the email address to another example.com address."""
    r["personalInfo"]["email"] = "tp@example.com"
    return r


def case_14_dates(r: Resume) -> Resume:
    """Move the dates of a role."""
    r["workExperience"][GLOBEX]["years"] = "Feb 2020 - Jan 2022"
    return r


Case = Callable[[Resume], Resume]

EXPECTED: dict[Case, set[str]] = {
    case_01_reorder: set(),
    case_02_reword: set(),
    case_03_title: {"R-8"},
    case_04_employer_of_record: {"R-9"},
    case_05_number: {"R-13"},
    case_06_years: {"R-2"},
    case_07_led_team: {"R-7"},
    case_08_banned_word: {"R-3"},
    case_09_remove_bullet: {"R-12"},
    case_10_remove_role: {"R-12"},
    case_11_new_technology: {"R-12", "R-20", "R-21"},
    case_12_new_skill: {"R-19"},
    case_13_email: {"R-1"},
    case_14_dates: {"R-10"},
}
