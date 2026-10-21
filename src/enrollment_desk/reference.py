"""Synthetic reference data: plans and client companies.

These plans and dependent rules are invented for development and testing. They are shaped like
typical Philippine corporate HMO plans but are not any insurer's actual terms.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    code: str
    name: str
    spouse_allowed: bool
    child_max_age: int
    parents_allowed: bool
    parent_max_age: int | None


@dataclass(frozen=True)
class Company:
    company_id: str
    name: str
    id_prefix: str
    plan_codes: tuple[str, ...]


PLANS: dict[str, Plan] = {
    "BRONZE": Plan("BRONZE", "Bronze", spouse_allowed=True, child_max_age=21, parents_allowed=False, parent_max_age=None),
    "SILVER": Plan("SILVER", "Silver", spouse_allowed=True, child_max_age=21, parents_allowed=True, parent_max_age=65),
    "GOLD": Plan("GOLD", "Gold", spouse_allowed=True, child_max_age=23, parents_allowed=True, parent_max_age=70),
}

COMPANIES: dict[str, Company] = {
    c.company_id: c
    for c in [
        Company("CMP-ALON", "Alon Logistics Inc.", "ALN", ("BRONZE", "SILVER")),
        Company("CMP-BAYAN", "Bayanihan Foods Corp.", "BYN", ("SILVER",)),
        Company("CMP-DALISAY", "Dalisay Software Labs", "DLS", ("SILVER", "GOLD")),
        Company("CMP-MAYON", "Mayon Builders Co.", "MYN", ("BRONZE",)),
        Company("CMP-TALA", "Tala Retail Group", "TLA", ("BRONZE", "GOLD")),
    ]
}


def plan_by_code_or_name(value: str | None) -> str | None:
    """Return a plan code for a code or a plan name such as 'Gold'. Unknown values pass through upper-cased."""
    if not value:
        return None
    v = value.strip().upper()
    if v in PLANS:
        return v
    for plan in PLANS.values():
        if v in (plan.name.upper(), f"{plan.name.upper()} PLAN"):
            return plan.code
    return v
