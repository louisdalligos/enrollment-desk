"""A stand-in for an HMO's enrollment intake.

It is stateless: the submission ID encodes everything needed to answer a status
check, so it runs the same in-process, as a local HTTP server, or as a Lambda
function URL. Outcomes are deterministic for a given payload and seed, which
keeps tests and demo runs reproducible.

Behaviour, all configurable:
- the first submit attempts sometimes fail as "unavailable" (HTTP 503)
- acknowledgments arrive after a delay, so the workflow has to poll
- a share of submissions are rejected with a realistic reason
- each member's number is issued a few business days after submission,
  which is what drives the hire-to-LOA readiness metric
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

REJECT_REASONS = (
    "Member already exists in insurer records",
    "Plan not active for this account",
    "Dependent documents incomplete",
)


class InsurerUnavailable(Exception):
    """The insurer endpoint did not accept the request (maps to HTTP 503)."""


class UnknownSubmission(Exception):
    pass


@dataclass(frozen=True)
class SimulatorConfig:
    seed: int = 7
    unavailable_first_attempt: float = 0.20
    unavailable_second_attempt: float = 0.05
    reject_rate: float = 0.08
    ack_delay_seconds: int = 90
    processing_days_min: int = 1
    processing_days_max: int = 8


def _digest(payload: dict, seed: int) -> str:
    body = json.dumps(payload.get("members", []), sort_keys=True, default=str)
    return hashlib.sha256(f"{seed}:{payload.get('company_id')}:{body}".encode()).hexdigest()


def add_business_days(start: date, days: int) -> date:
    d = start
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


class InsurerSimulator:
    def __init__(self, config: SimulatorConfig | None = None):
        self.config = config or SimulatorConfig()

    def submit(self, payload: dict, now: datetime, attempt: int = 1) -> dict:
        h = _digest(payload, self.config.seed)
        rng = random.Random(f"{h}:attempt:{attempt}")
        p = {1: self.config.unavailable_first_attempt, 2: self.config.unavailable_second_attempt}.get(attempt, 0.0)
        if rng.random() < p:
            raise InsurerUnavailable(f"insurer unavailable on attempt {attempt}")
        count = len(payload.get("members", []))
        submission_id = f"SUB-{h[:16]}-{int(now.timestamp())}-{count}"
        return {"submission_id": submission_id, "status": "received"}

    def status(self, submission_id: str, now: datetime) -> dict:
        try:
            _, h16, ts, count = submission_id.split("-")
            submitted_at = datetime.fromtimestamp(int(ts), tz=now.tzinfo)
            count = int(count)
        except ValueError as exc:
            raise UnknownSubmission(submission_id) from exc
        if (now - submitted_at).total_seconds() < self.config.ack_delay_seconds:
            return {"submission_id": submission_id, "status": "pending"}
        rng = random.Random(f"{h16}:outcome")
        if rng.random() < self.config.reject_rate:
            return {"submission_id": submission_id, "status": "rejected", "reason": rng.choice(REJECT_REASONS)}
        days = rng.randint(self.config.processing_days_min, self.config.processing_days_max)
        issued_on = add_business_days(submitted_at.date(), days)
        return {
            "submission_id": submission_id,
            "status": "acknowledged",
            "ack_id": f"ACK-{h16[:10].upper()}",
            "member_numbers": [
                {"index": i, "member_number": f"HMO-{h16[:6].upper()}-{i + 1:03d}", "issued_on": issued_on.isoformat()}
                for i in range(count)
            ],
        }
