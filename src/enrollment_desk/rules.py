"""Deterministic validation rules. These decide whether a case is clear; the AI never does."""

from __future__ import annotations

from datetime import date

from .models import Flag, MemberRecord, Relationship, Severity
from .reference import COMPANIES, PLANS

RULES_VERSION = "rules-2026.10.1"

LATE_ENDORSEMENT_DAYS = 30  # hire date this far in the past: the member has been covered but unusable
FUTURE_HIRE_LIMIT_DAYS = 60
MIN_PRINCIPAL_AGE = 18
MAX_AGE = 100

PRINCIPAL_REQUIRED = ("employee_id", "first_name", "last_name", "birth_date", "sex", "hire_date", "plan_code")
DEPENDENT_REQUIRED = ("first_name", "last_name", "birth_date", "sex", "principal_employee_id")


def age_on(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def validate(
    members: list[MemberRecord],
    company_id: str,
    existing: dict[str, dict],
    today: date,
    parse_flags: list[Flag] | None = None,
) -> list[Flag]:
    """Return every flag for a roster.

    `existing` maps employee_id -> {"plan_code": ...} for principals already enrolled with this company.
    Fields that failed to parse are not reported again as missing.
    """
    flags: list[Flag] = list(parse_flags or [])
    unreadable = {(f.row, f.field) for f in flags if f.field}
    company = COMPANIES.get(company_id)
    if company is None:
        return flags + [Flag(code="unknown_company", message=f"Unknown company {company_id}")]

    def add(code: str, m: MemberRecord, message: str, field: str | None = None, severity=Severity.BLOCK):
        flags.append(Flag(code=code, row=m.row, field=field, message=message, severity=severity))

    principals_in_file = {m.employee_id: m for m in members if m.relationship == Relationship.PRINCIPAL and m.employee_id}
    seen_ids: set[str] = set()
    seen_people: set[tuple] = set()

    for m in members:
        if m.relationship is None and (m.row, "relationship") not in unreadable:
            add("missing_required", m, "Relationship is missing", "relationship")
            continue
        if m.relationship is None:
            continue
        required = PRINCIPAL_REQUIRED if m.relationship == Relationship.PRINCIPAL else DEPENDENT_REQUIRED
        for field in required:
            if getattr(m, field) in (None, "") and (m.row, field) not in unreadable:
                add("missing_required", m, f"{field.replace('_', ' ').capitalize()} is missing", field)

        # duplicates within the file
        person = (m.first_name, m.last_name, m.birth_date, m.relationship)
        dup = (m.relationship == Relationship.PRINCIPAL and m.employee_id and m.employee_id in seen_ids) or (
            None not in person and person in seen_people
        )
        if dup:
            add("duplicate_in_file", m, "This person appears earlier in the same file")
        if m.relationship == Relationship.PRINCIPAL and m.employee_id:
            seen_ids.add(m.employee_id)
        if None not in person:
            seen_people.add(person)

        # dates
        valid_birth = False
        if m.birth_date:
            age = age_on(m.birth_date, today)
            if m.birth_date > today or age > MAX_AGE:
                add("invalid_birth_date", m, f"Birth date {m.birth_date.isoformat()} is not plausible", "birth_date")
            else:
                valid_birth = True
                if m.relationship == Relationship.PRINCIPAL and age < MIN_PRINCIPAL_AGE:
                    add("principal_under_age", m, f"Employee would be {age}, under {MIN_PRINCIPAL_AGE}", "birth_date")

        if m.relationship == Relationship.PRINCIPAL:
            if m.hire_date:
                days = (today - m.hire_date).days
                if -days > FUTURE_HIRE_LIMIT_DAYS:
                    add("future_hire_date", m, f"Hire date is more than {FUTURE_HIRE_LIMIT_DAYS} days ahead", "hire_date")
                elif days > LATE_ENDORSEMENT_DAYS:
                    add("late_endorsement", m,
                        f"Hired {days} days ago: covered since day one but not yet usable at clinics",
                        "hire_date", Severity.INFO)
            if m.employee_id and m.employee_id in existing:
                add("already_enrolled", m, f"Employee {m.employee_id} is already enrolled", "employee_id")
            if m.plan_code:
                if m.plan_code not in PLANS:
                    add("unknown_plan", m, f"Plan '{m.plan_code}' does not exist", "plan_code")
                elif m.plan_code not in company.plan_codes:
                    add("plan_not_in_contract", m, f"{company.name} has no {m.plan_code} plan", "plan_code")
            continue

        # dependents
        pid = m.principal_employee_id
        if not pid:
            continue
        if pid in principals_in_file:
            plan_code = principals_in_file[pid].plan_code
        elif pid in existing:
            plan_code = existing[pid].get("plan_code")
        else:
            add("dependent_principal_missing", m, f"Principal {pid} is not in this file or already enrolled",
                "principal_employee_id")
            continue
        plan = PLANS.get(plan_code or "")
        if plan is None:
            continue
        if m.relationship == Relationship.SPOUSE and not plan.spouse_allowed:
            add("dependent_not_allowed", m, f"{plan.name} plan does not cover spouses", "relationship")
        elif m.relationship == Relationship.PARENT and not plan.parents_allowed:
            add("dependent_not_allowed", m, f"{plan.name} plan does not cover parents", "relationship")
        elif valid_birth:
            age = age_on(m.birth_date, today)
            limit = plan.child_max_age if m.relationship == Relationship.CHILD else (
                plan.parent_max_age if m.relationship == Relationship.PARENT else None)
            if limit is not None and age > limit:
                add("dependent_over_age", m, f"{m.relationship.value.capitalize()} is {age}; {plan.name} limit is {limit}",
                    "birth_date")
    return flags
