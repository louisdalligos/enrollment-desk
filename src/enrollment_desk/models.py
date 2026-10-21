"""Core data models for enrollment and endorsement cases."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class Relationship(str, Enum):
    PRINCIPAL = "principal"
    SPOUSE = "spouse"
    CHILD = "child"
    PARENT = "parent"


class Severity(str, Enum):
    BLOCK = "block"  # case must go to human review
    INFO = "info"  # recorded and shown, but does not stop processing


class CaseStatus(str, Enum):
    RECEIVED = "received"
    VALIDATED = "validated"
    NEEDS_REVIEW = "needs_review"
    SUBMITTED = "submitted"
    ACTIVE = "active"  # only after the insurer acknowledges


class MemberRecord(BaseModel):
    """One person on a roster, after deterministic parsing and normalization."""

    row: int = Field(description="1-based index of the data row in the source file")
    employee_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    birth_date: date | None = None
    sex: str | None = None  # "M" or "F"
    relationship: Relationship | None = None
    principal_employee_id: str | None = None  # set for dependents
    hire_date: date | None = None
    plan_code: str | None = None
    raw: dict[str, str] = Field(default_factory=dict, description="Original cell values by source header")

    @property
    def is_dependent(self) -> bool:
        return self.relationship not in (None, Relationship.PRINCIPAL)


class Flag(BaseModel):
    code: str
    severity: Severity = Severity.BLOCK
    row: int | None = None
    field: str | None = None
    message: str


class ParseResult(BaseModel):
    members: list[MemberRecord]
    flags: list[Flag]  # parse-level issues (unreadable dates, unknown values)
    header_map: dict[str, str]  # source header -> canonical field
    unmapped_headers: list[str]  # left for the AI column-mapping step in phase 2


class MemberNumber(BaseModel):
    row: int
    member_number: str
    issued_on: date


class Case(BaseModel):
    case_id: str
    reference_number: str  # given to HR, the HR nurse and the member at intake
    company_id: str
    source_document: str
    received_at: datetime
    status: CaseStatus = CaseStatus.RECEIVED
    members: list[MemberRecord] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    unmapped_headers: list[str] = Field(default_factory=list)
    submission_id: str | None = None
    ack_id: str | None = None
    member_numbers: list[MemberNumber] = Field(default_factory=list)
    review_reason: str | None = None

    @property
    def blocking_flags(self) -> list[Flag]:
        return [f for f in self.flags if f.severity == Severity.BLOCK]


class Event(BaseModel):
    case_id: str
    type: str
    at: datetime
    data: dict = Field(default_factory=dict)
