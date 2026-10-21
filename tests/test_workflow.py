import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from enrollment_desk.evaluate import score
from enrollment_desk.insurer_client import InProcessInsurer
from enrollment_desk.insurer_sim.core import InsurerSimulator, SimulatorConfig
from enrollment_desk.insurer_sim.http import route
from enrollment_desk.models import CaseStatus
from enrollment_desk.store import LocalStore
from enrollment_desk.synth import generate
from enrollment_desk.workflow import local_runner, steps

ROOT = Path(__file__).resolve().parents[1]
CLEAN = str(ROOT / "data/samples/clean_roster.csv")
MESSY = str(ROOT / "data/samples/messy_roster.csv")
START = datetime(2026, 10, 15, 9, 0, tzinfo=timezone(timedelta(hours=8)))


def make_runtime(tmp_path, config=None):
    clock = local_runner.FakeClock(START)
    rt = steps.Runtime(store=LocalStore(tmp_path / "store"),
                       insurer=InProcessInsurer(InsurerSimulator(config or SimulatorConfig()), clock),
                       clock=clock, existing_members=lambda cid: {}, sleep=clock.advance)
    return rt, clock


def event_types(rt, case_id):
    return [e.type for e in rt.store.events(case_id)]


def test_clean_roster_goes_active_only_after_ack(tmp_path):
    rt, clock = make_runtime(tmp_path)
    result = local_runner.run(CLEAN, "CMP-DALISAY", rt, clock)
    case = rt.store.get_case(result["final"]["case_id"])
    assert case.status == CaseStatus.ACTIVE
    assert case.reference_number.startswith("END-20261015-")
    assert case.ack_id and len(case.member_numbers) == len(case.members)
    types = event_types(rt, case.case_id)
    assert types.index("insurer.acknowledged") < types.index("case.active")
    assert all(r["days_hire_to_loa"] >= 0 for r in steps.readiness(case))


def test_messy_roster_goes_to_review_without_submitting(tmp_path):
    rt, clock = make_runtime(tmp_path)
    result = local_runner.run(MESSY, "CMP-DALISAY", rt, clock)
    case = rt.store.get_case(result["final"]["case_id"])
    assert case.status == CaseStatus.NEEDS_REVIEW
    assert "insurer.submitted" not in event_types(rt, case.case_id)
    assert {f.code for f in case.blocking_flags} == {"unparseable_date", "dependent_over_age", "duplicate_in_file"}


def test_insurer_down_routes_to_review(tmp_path):
    rt, clock = make_runtime(tmp_path)

    class Down:
        def submit(self, payload, attempt):
            from enrollment_desk.insurer_sim.core import InsurerUnavailable
            raise InsurerUnavailable("down")

        def status(self, sid):
            raise AssertionError("should not poll")

    rt.insurer = Down()
    result = local_runner.run(CLEAN, "CMP-DALISAY", rt, clock)
    case = rt.store.get_case(result["final"]["case_id"])
    assert case.status == CaseStatus.NEEDS_REVIEW
    assert event_types(rt, case.case_id).count("insurer.unavailable") == steps.SUBMIT_ATTEMPTS


def test_rejection_routes_to_review(tmp_path):
    rt, clock = make_runtime(tmp_path, SimulatorConfig(reject_rate=1.0, unavailable_first_attempt=0))
    result = local_runner.run(CLEAN, "CMP-DALISAY", rt, clock)
    case = rt.store.get_case(result["final"]["case_id"])
    assert case.status == CaseStatus.NEEDS_REVIEW and case.review_reason.startswith("Insurer rejected")


def test_cannot_mark_active_without_ack(tmp_path):
    rt, clock = make_runtime(tmp_path)
    state = steps.validate_case(steps.intake({"document_key": CLEAN, "company_id": "CMP-DALISAY"}, rt), rt)
    with pytest.raises(RuntimeError):
        steps.mark_active(state, rt)


def test_simulator_is_stateless_and_deterministic():
    sim = InsurerSimulator(SimulatorConfig(unavailable_first_attempt=0, reject_rate=0))
    payload = {"company_id": "C", "members": [{"employee_id": "1"}, {"employee_id": "2"}]}
    status, body = route(sim, "POST", "/submissions", {"attempt": ["1"]}, json.dumps(payload).encode(), START)
    assert status == 202
    sid = body["submission_id"]
    assert route(sim, "GET", f"/submissions/{sid}", {}, b"", START)[1]["status"] == "pending"
    later = START + timedelta(minutes=5)
    first = route(InsurerSimulator(sim.config), "GET", f"/submissions/{sid}", {}, b"", later)[1]
    second = route(InsurerSimulator(sim.config), "GET", f"/submissions/{sid}", {}, b"", later)[1]
    assert first == second and first["status"] == "acknowledged" and len(first["member_numbers"]) == 2


def test_pending_tracker_lists_hired_members_without_numbers(tmp_path):
    rt, clock = make_runtime(tmp_path)
    local_runner.run(MESSY, "CMP-DALISAY", rt, clock)
    rows = steps.pending_members(rt.store.list_cases(), START.date())
    assert rows[0]["employee_id"] == "DLS-30010" and rows[0]["days_covered_without_loa"] == 58
    assert [r["employee_id"] for r in rows].count("DLS-30011") == 1  # the duplicate row isn't counted twice


def test_generated_labels_are_reproducible_and_pass_baseline(tmp_path):
    generate(tmp_path, count=30)
    result = score(tmp_path)
    assert result["failures"] == []
    assert result["flag_recall"] == 1.0 and result["field_accuracy"] == 1.0
    committed = (ROOT / "data/generated/labels.jsonl").read_text().splitlines()[:30]
    assert (tmp_path / "labels.jsonl").read_text().splitlines() == committed
