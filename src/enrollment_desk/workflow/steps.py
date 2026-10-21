"""Workflow steps. Each step takes the state-machine input and returns the next state.

The same functions run as Lambda tasks in AWS Step Functions (see handlers.py)
and in-process in the local runner. Only the workflow changes a case's status;
no AI step can, and coverage becomes active only on an insurer acknowledgment.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from ..insurer_client import InsurerClient
from ..insurer_sim.core import InsurerUnavailable
from ..models import Case, CaseStatus, Event, MemberNumber, Relationship
from ..roster_parser import parse_roster
from ..rules import RULES_VERSION, validate
from ..store import Store

SUBMIT_ATTEMPTS = 3


@dataclass
class Runtime:
    store: Store
    insurer: InsurerClient
    clock: Callable[[], datetime]
    existing_members: Callable[[str], dict[str, dict]]  # company_id -> {employee_id: {"plan_code": ...}}
    sleep: Callable[[float], None] = time.sleep


def _event(rt: Runtime, case: Case, type_: str, **data) -> None:
    rt.store.append_event(Event(case_id=case.case_id, type=type_, at=rt.clock(), data=data))


def reference_number(company_id: str, document: str, received_at: datetime) -> str:
    h = hashlib.sha256(f"{company_id}|{document}|{received_at.isoformat()}".encode()).hexdigest()[:6].upper()
    return f"END-{received_at:%Y%m%d}-{h}"


def intake(state: dict, rt: Runtime) -> dict:
    """Parse the uploaded roster and open a case. The reference number is issued here, on day one."""
    received_at = rt.clock()
    doc = state["document_key"]
    parsed = parse_roster(Path(doc).name, rt.store.read_document(doc))
    ref = state.get("reference_number") or reference_number(state["company_id"], doc, received_at)
    if not re.fullmatch(r"END-\d{8}-[A-F0-9]{6}(?:[A-F0-9]{26})?", ref):
        raise ValueError("Invalid enrollment reference")
    case = Case(
        case_id=ref.lower(),
        reference_number=ref,
        company_id=state["company_id"],
        source_document=doc,
        received_at=received_at,
        members=parsed.members,
        flags=parsed.flags,
        unmapped_headers=parsed.unmapped_headers,
    )
    rt.store.put_case(case)
    _event(rt, case, "case.received", reference_number=ref, document=doc, members=len(parsed.members),
           header_map=parsed.header_map, unmapped_headers=parsed.unmapped_headers)
    return {"case_id": case.case_id}


def validate_case(state: dict, rt: Runtime) -> dict:
    case = rt.store.get_case(state["case_id"])
    today = rt.clock().date()
    parse_flags = case.flags
    case.flags = validate(case.members, case.company_id, rt.existing_members(case.company_id), today, parse_flags)
    blocking = case.blocking_flags
    case.status = CaseStatus.NEEDS_REVIEW if blocking else CaseStatus.VALIDATED
    if blocking:
        case.review_reason = f"{len(blocking)} validation issue(s)"
    rt.store.put_case(case)
    _event(rt, case, "case.validated", rules_version=RULES_VERSION, all_clear=not blocking,
           flags=[f.model_dump() for f in case.flags])
    return {"case_id": case.case_id, "all_clear": not blocking}


def insurer_payload(case: Case) -> dict:
    return {
        "company_id": case.company_id,
        "reference_number": case.reference_number,
        "members": [
            {
                "employee_id": m.employee_id,
                "first_name": m.first_name,
                "last_name": m.last_name,
                "birth_date": m.birth_date,
                "sex": m.sex,
                "relationship": m.relationship.value if m.relationship else None,
                "principal_employee_id": m.principal_employee_id,
                "hire_date": m.hire_date,
                "plan_code": m.plan_code,
            }
            for m in case.members
        ],
    }


def submit(state: dict, rt: Runtime) -> dict:
    """Send the case to the insurer, retrying with backoff. Raises InsurerUnavailable if every attempt fails."""
    case = rt.store.get_case(state["case_id"])
    payload = insurer_payload(case)
    for attempt in range(1, SUBMIT_ATTEMPTS + 1):
        try:
            result = rt.insurer.submit(payload, attempt)
            break
        except InsurerUnavailable as exc:
            _event(rt, case, "insurer.unavailable", attempt=attempt, error=str(exc))
            if attempt == SUBMIT_ATTEMPTS:
                raise
            rt.sleep(2 ** attempt)
    case.submission_id = result["submission_id"]
    case.status = CaseStatus.SUBMITTED
    rt.store.put_case(case)
    _event(rt, case, "insurer.submitted", submission_id=case.submission_id, attempts=attempt)
    return {"case_id": case.case_id, "polls": 0}


def check_ack(state: dict, rt: Runtime) -> dict:
    case = rt.store.get_case(state["case_id"])
    polls = state.get("polls", 0) + 1
    result = rt.insurer.status(case.submission_id)
    status = result["status"]
    if status == "pending":
        return {"case_id": case.case_id, "polls": polls, "ack_status": "pending"}
    if status == "rejected":
        case.status = CaseStatus.NEEDS_REVIEW
        case.review_reason = f"Insurer rejected: {result.get('reason')}"
        rt.store.put_case(case)
        _event(rt, case, "insurer.rejected", reason=result.get("reason"), polls=polls)
        return {"case_id": case.case_id, "polls": polls, "ack_status": "rejected"}
    case.ack_id = result["ack_id"]
    case.member_numbers = [
        MemberNumber(row=case.members[n["index"]].row, member_number=n["member_number"],
                     issued_on=date.fromisoformat(n["issued_on"]))
        for n in result["member_numbers"]
    ]
    rt.store.put_case(case)
    _event(rt, case, "insurer.acknowledged", ack_id=case.ack_id, polls=polls,
           member_numbers=[n.model_dump(mode="json") for n in case.member_numbers])
    return {"case_id": case.case_id, "polls": polls, "ack_status": "acknowledged"}


def mark_active(state: dict, rt: Runtime) -> dict:
    case = rt.store.get_case(state["case_id"])
    if not case.ack_id:
        raise RuntimeError("refusing to mark a case active without an insurer acknowledgment")
    case.status = CaseStatus.ACTIVE
    rt.store.put_case(case)
    _event(rt, case, "case.active", ack_id=case.ack_id, readiness=readiness(case))
    return {"case_id": case.case_id, "status": case.status.value}


def mark_for_review(state: dict, rt: Runtime) -> dict:
    """Phase 1 parks the case. Phase 3 replaces this with a task-token wait for the reviewer in the console."""
    case = rt.store.get_case(state["case_id"])
    if state.get("reason"):
        case.review_reason = state["reason"]
    case.status = CaseStatus.NEEDS_REVIEW
    rt.store.put_case(case)
    _event(rt, case, "case.needs_review", reason=case.review_reason)
    return {"case_id": case.case_id, "status": case.status.value}


def readiness(case: Case) -> list[dict]:
    """Days from each principal's hire date to the date their member number exists (first possible LOA)."""
    numbers = {n.row: n for n in case.member_numbers}
    out = []
    for m in case.members:
        if m.relationship == Relationship.PRINCIPAL and m.hire_date and m.row in numbers:
            issued = numbers[m.row].issued_on
            out.append({"employee_id": m.employee_id, "hire_date": m.hire_date.isoformat(),
                        "member_number_on": issued.isoformat(), "days_hire_to_loa": (issued - m.hire_date).days})
    return out


def pending_members(cases: list[Case], today: date) -> list[dict]:
    """Day-one gap tracker: principals who are covered (hired) but can't get an LOA yet."""
    rows = []
    for case in cases:
        numbers = {n.row: n for n in case.member_numbers}
        enrolled_elsewhere = {f.row for f in case.flags if f.code in ("already_enrolled", "duplicate_in_file")}
        for m in case.members:
            if m.relationship != Relationship.PRINCIPAL or not m.hire_date or m.hire_date > today:
                continue
            if m.row in enrolled_elsewhere:
                continue
            n = numbers.get(m.row)
            if n is None or n.issued_on > today:
                rows.append({"reference_number": case.reference_number, "employee_id": m.employee_id,
                             "name": f"{m.first_name} {m.last_name}", "hire_date": m.hire_date.isoformat(),
                             "days_covered_without_loa": (today - m.hire_date).days, "case_status": case.status.value})
    return sorted(rows, key=lambda r: -r["days_covered_without_loa"])
