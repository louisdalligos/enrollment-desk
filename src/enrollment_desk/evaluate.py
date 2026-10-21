"""Score the deterministic pipeline (parser + rules) against the labeled requests.

Phase 1 baseline: with no AI in the loop yet, every labeled flag should be
caught and every field read exactly. Phase 2 adds AI extraction and the same
script, extended, becomes the CI regression gate.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from .roster_parser import parse_roster
from .rules import validate


def _member_dict(m) -> dict:
    return {
        "row": m.row, "employee_id": m.employee_id, "first_name": m.first_name, "last_name": m.last_name,
        "birth_date": m.birth_date.isoformat() if m.birth_date else None, "sex": m.sex,
        "relationship": m.relationship.value if m.relationship else None,
        "principal_employee_id": m.principal_employee_id,
        "hire_date": m.hire_date.isoformat() if m.hire_date else None, "plan_code": m.plan_code,
    }


def score(data_dir: str | Path) -> dict:
    data = Path(data_dir)
    labels = [json.loads(l) for l in (data / "labels.jsonl").read_text().splitlines() if l.strip()]
    existing_all = json.loads((data / "existing_members.json").read_text())
    fields_total = fields_ok = 0
    tp = fp = fn = 0
    clear_ok = 0
    failures = []
    for lab in labels:
        parsed = parse_roster(data / "requests" / lab["file"])
        flags = validate(parsed.members, lab["company_id"], existing_all[lab["company_id"]],
                         date.fromisoformat(lab["as_of"]), parsed.flags)
        got = {(f.code, f.row) for f in flags}
        want = {(f["code"], f["row"]) for f in lab["expected_flags"]}
        tp += len(got & want)
        fp += len(got - want)
        fn += len(want - got)
        all_clear = not any(f.severity.value == "block" for f in flags)
        clear_ok += all_clear == lab["expected_all_clear"]
        got_members = [_member_dict(m) for m in parsed.members]
        for exp, act in zip(lab["expected_members"], got_members):
            for k, v in exp.items():
                fields_total += 1
                fields_ok += act.get(k) == v
                if act.get(k) != v:
                    failures.append(f"{lab['id']} row {exp['row']} {k}: expected {v!r}, got {act.get(k)!r}")
        if len(got_members) != len(lab["expected_members"]):
            failures.append(f"{lab['id']}: expected {len(lab['expected_members'])} members, got {len(got_members)}")
        for code, row in sorted(want - got):
            failures.append(f"{lab['id']}: missed flag {code} on row {row}")
        for code, row in sorted(got - want):
            failures.append(f"{lab['id']}: unexpected flag {code} on row {row}")
    return {
        "requests": len(labels),
        "field_accuracy": fields_ok / fields_total if fields_total else 0.0,
        "flag_recall": tp / (tp + fn) if tp + fn else 1.0,
        "flag_precision": tp / (tp + fp) if tp + fp else 1.0,
        "case_routing_accuracy": clear_ok / len(labels),
        "failures": failures,
    }


if __name__ == "__main__":
    result = score(sys.argv[1] if len(sys.argv) > 1 else "data/generated")
    failures = result.pop("failures")
    print(json.dumps(result, indent=2))
    for f in failures[:30]:
        print("  -", f)
    sys.exit(1 if failures else 0)
