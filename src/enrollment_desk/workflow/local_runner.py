"""Runs the same steps in the same order as the Step Functions state machine, with a fake clock.

Keep this in step with infra/terraform/state_machine.asl.json.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..insurer_sim.core import InsurerUnavailable
from . import steps
from .steps import Runtime

ACK_WAIT_SECONDS = 60
MAX_ACK_POLLS = 10


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def run(document_key: str, company_id: str, rt: Runtime, clock: FakeClock | None = None) -> dict:
    """Execute one case end to end. Returns the final state and the path of states visited."""
    path = ["Intake"]
    state = steps.intake({"document_key": document_key, "company_id": company_id}, rt)
    path.append("Validate")
    state = steps.validate_case(state, rt)
    if not state["all_clear"]:
        path.append("NeedsReview")
        return {"final": steps.mark_for_review(state, rt), "path": path}

    path.append("SubmitToInsurer")
    try:
        state = steps.submit(state, rt)
    except InsurerUnavailable:
        path.append("NeedsReview")
        return {"final": steps.mark_for_review({**state, "reason": "Insurer unavailable after retries"}, rt), "path": path}

    while True:
        path.append("WaitForAck")
        if clock:
            clock.advance(ACK_WAIT_SECONDS)
        path.append("CheckAck")
        state = steps.check_ack(state, rt)
        if state["ack_status"] == "acknowledged":
            path.append("MarkActive")
            return {"final": steps.mark_active(state, rt), "path": path}
        if state["ack_status"] == "rejected":
            path.append("NeedsReview")
            return {"final": steps.mark_for_review(state, rt), "path": path}
        if state["polls"] >= MAX_ACK_POLLS:
            path.append("NeedsReview")
            return {"final": steps.mark_for_review({**state, "reason": "No insurer reply after polling"}, rt),
                    "path": path}
