"""Command line for local runs.

  python -m enrollment_desk.cli run data/samples/clean_roster.csv --company CMP-DALISAY
  python -m enrollment_desk.cli run-all data/generated
  python -m enrollment_desk.cli pending
  python -m enrollment_desk.cli generate data/generated
  python -m enrollment_desk.cli evaluate data/generated
  python -m enrollment_desk.cli simulator --port 8081
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .insurer_client import HttpInsurer, InProcessInsurer
from .insurer_sim.core import InsurerSimulator
from .store import LocalStore
from .synth import AS_OF
from .workflow import local_runner, steps

PH = timezone(timedelta(hours=8))
START = datetime(AS_OF.year, AS_OF.month, AS_OF.day, 9, 0, tzinfo=PH)


def _runtime(args, clock) -> steps.Runtime:
    store = LocalStore(args.store)
    existing_path = Path(args.existing)
    existing_all = json.loads(existing_path.read_text()) if existing_path.exists() else {}
    insurer = HttpInsurer(args.insurer_url) if args.insurer_url else InProcessInsurer(InsurerSimulator(), clock)
    return steps.Runtime(store=store, insurer=insurer, clock=clock,
                         existing_members=lambda cid: existing_all.get(cid, {}), sleep=lambda s: clock.advance(s))


def _print_case(rt: steps.Runtime, case_id: str, path: list[str]) -> None:
    case = rt.store.get_case(case_id)
    print(f"\n{case.reference_number}  {case.company_id}  {Path(case.source_document).name}")
    print("  path:   " + " -> ".join(path))
    print(f"  status: {case.status.value}" + (f" ({case.review_reason})" if case.review_reason else ""))
    for f in case.flags:
        print(f"  [{f.severity.value}] row {f.row}: {f.code} - {f.message}")
    for r in steps.readiness(case):
        print(f"  {r['employee_id']}: hired {r['hire_date']}, member number on {r['member_number_on']} "
              f"-> {r['days_hire_to_loa']} day(s) to first possible LOA")
    for e in rt.store.events(case_id):
        print(f"    {e.at:%H:%M:%S}  {e.type}")


def cmd_run(args) -> None:
    clock = local_runner.FakeClock(START)
    rt = _runtime(args, clock)
    result = local_runner.run(str(Path(args.file)), args.company, rt, clock)
    _print_case(rt, result["final"]["case_id"], result["path"])


def cmd_run_all(args) -> None:
    data = Path(args.data)
    labels = [json.loads(l) for l in (data / "labels.jsonl").read_text().splitlines() if l.strip()]
    args.existing = str(data / "existing_members.json")
    clock = local_runner.FakeClock(START)
    rt = _runtime(args, clock)
    statuses: dict[str, int] = {}
    days: list[int] = []
    for lab in labels:
        clock.advance(600)
        result = local_runner.run(str(data / "requests" / lab["file"]), lab["company_id"], rt, clock)
        case = rt.store.get_case(result["final"]["case_id"])
        statuses[case.status.value] = statuses.get(case.status.value, 0) + 1
        days += [r["days_hire_to_loa"] for r in steps.readiness(case)]
    print(json.dumps({"cases": len(labels), "by_status": statuses}, indent=2))
    if days:
        days.sort()
        p90 = days[min(len(days) - 1, int(0.9 * len(days)))]
        print(f"hire-to-LOA readiness for activated principals: median {statistics.median(days)} days, p90 {p90} days")


def cmd_pending(args) -> None:
    store = LocalStore(args.store)
    rows = steps.pending_members(store.list_cases(), AS_OF)
    print(f"{len(rows)} hired members who can't get an LOA yet (as of {AS_OF}):")
    for r in rows[: args.limit]:
        print(f"  {r['days_covered_without_loa']:>4} days  {r['employee_id'] or '-':<10} {r['name']:<28} "
              f"{r['reference_number']}  {r['case_status']}")


def main() -> None:
    p = argparse.ArgumentParser(prog="enrollment-desk")
    p.add_argument("--store", default=".local/store")
    p.add_argument("--existing", default="data/generated/existing_members.json")
    p.add_argument("--insurer-url", default=None, help="use a running simulator instead of the in-process one")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("file")
    r.add_argument("--company", required=True)
    ra = sub.add_parser("run-all")
    ra.add_argument("data", nargs="?", default="data/generated")
    pe = sub.add_parser("pending")
    pe.add_argument("--limit", type=int, default=20)
    g = sub.add_parser("generate")
    g.add_argument("out", nargs="?", default="data/generated")
    ev = sub.add_parser("evaluate")
    ev.add_argument("data", nargs="?", default="data/generated")
    s = sub.add_parser("simulator")
    s.add_argument("--port", type=int, default=8081)
    args = p.parse_args()

    if args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "run-all":
        cmd_run_all(args)
    elif args.cmd == "pending":
        cmd_pending(args)
    elif args.cmd == "generate":
        from .synth import generate

        labels = generate(args.out)
        print(f"wrote {len(labels)} requests to {args.out}")
    elif args.cmd == "evaluate":
        from .evaluate import score

        result = score(args.data)
        failures = result.pop("failures")
        print(json.dumps(result, indent=2))
        for f in failures[:30]:
            print("  -", f)
        raise SystemExit(1 if failures else 0)
    elif args.cmd == "simulator":
        from .insurer_sim.http import serve

        serve(args.port)


if __name__ == "__main__":
    main()
