"""Synthetic enrollment requests with labels.

Every file is fake: invented companies, people and plans. Files are deliberately
messy in the ways real HR rosters are (title rows, odd headers, mixed date
formats, all-caps names, extra columns, Excel or CSV), and about half carry
real problems a reviewer should catch. Each request comes with labels: the
correct member values and the exact flags the pipeline should raise. Those
labels are the start of the evaluation suite.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .reference import COMPANIES, PLANS
from .rules import LATE_ENDORSEMENT_DAYS

AS_OF = date(2026, 10, 15)

FIRST_M = ["Juan", "Jose", "Mark", "Paolo", "Rafael", "Miguel", "Carlo", "Gabriel", "Andres", "Noel", "Ramon", "Joshua"]
FIRST_F = ["Maria", "Ana", "Kristine", "Patricia", "Andrea", "Camille", "Bea", "Joanna", "Liza", "Maria Clara", "Rhea", "Grace"]
LAST = ["Santos", "Reyes", "Cruz", "Bautista", "Garcia", "Mendoza", "Dela Cruz", "Villanueva", "Ramos", "Aquino",
        "Castillo", "Navarro", "Torres", "Gonzales", "Flores", "Mercado", "Soriano", "Pascual", "Domingo", "Salazar"]

HEADER_STYLES = [
    {"employee_id": "Employee ID", "first_name": "First Name", "last_name": "Last Name", "birth_date": "Birth Date",
     "sex": "Sex", "relationship": "Relationship", "principal_employee_id": "Principal ID", "hire_date": "Hire Date",
     "plan_code": "Plan"},
    {"employee_id": "Emp #", "last_name": "Surname", "first_name": "Given Name", "birth_date": "DOB", "sex": "Gender",
     "relationship": "Member Type", "principal_employee_id": "Dependent Of", "hire_date": "Date Hired",
     "plan_code": "Plan Type"},
    {"employee_id": "EMP_NO", "full_name": "Name (Last, First)", "birth_date": "Bday", "sex": "Sex",
     "relationship": "Relation", "principal_employee_id": "Principal Emp No", "hire_date": "Start Date",
     "plan_code": "Benefit Plan"},
    {"employee_id": "Staff ID", "first_name": "fname", "last_name": "lname", "birth_date": "Date of Birth",
     "sex": "gender", "relationship": "rel", "principal_employee_id": "principal employee id",
     "hire_date": "employment date", "plan_code": "plan code"},
]
DATE_STYLES = ["%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%B %d, %Y"]
REL_STYLES = [
    {"principal": "Employee", "spouse": "Spouse", "child": "Child", "parent": "Parent"},
    {"principal": "Principal", "spouse": "spouse", "child": "child", "parent": "parent"},
    "by_sex",  # Self / Wife or Husband / Son or Daughter / Mother or Father
]
BY_SEX = {
    ("principal", "M"): "Self", ("principal", "F"): "Self",
    ("spouse", "M"): "Husband", ("spouse", "F"): "Wife",
    ("child", "M"): "Son", ("child", "F"): "Daughter",
    ("parent", "M"): "Father", ("parent", "F"): "Mother",
}


@dataclass
class Person:
    relationship: str
    employee_id: str | None
    first_name: str | None
    last_name: str | None
    birth_date: date | None
    sex: str | None
    principal_employee_id: str | None = None
    hire_date: date | None = None
    plan_code: str | None = None
    display: dict = field(default_factory=dict)  # overrides for what is written to the file
    flags: list = field(default_factory=list)  # (code, field)

    def canonical(self) -> dict:
        return {
            "employee_id": self.employee_id, "first_name": self.first_name, "last_name": self.last_name,
            "birth_date": self.birth_date.isoformat() if self.birth_date else None, "sex": self.sex,
            "relationship": self.relationship, "principal_employee_id": self.principal_employee_id,
            "hire_date": self.hire_date.isoformat() if self.hire_date else None, "plan_code": self.plan_code,
        }


def _birth_for_age(rng: random.Random, age: int) -> date:
    return AS_OF - timedelta(days=age * 365 + rng.randint(30, 330))


class Generator:
    def __init__(self, seed: int = 2026):
        self.seed = seed
        self.counter = 20000
        self.existing = self._existing_members()

    def _name(self, rng: random.Random, sex: str, last: str | None = None) -> tuple[str, str]:
        return rng.choice(FIRST_M if sex == "M" else FIRST_F), last or rng.choice(LAST)

    def _existing_members(self) -> dict[str, dict[str, dict]]:
        rng = random.Random(self.seed)
        out: dict[str, dict[str, dict]] = {}
        for c in COMPANIES.values():
            out[c.company_id] = {}
            for n in range(20):
                sex = rng.choice("MF")
                first, last = self._name(rng, sex)
                eid = f"{c.id_prefix}-{10000 + n:05d}"
                out[c.company_id][eid] = {
                    "plan_code": rng.choice(c.plan_codes), "first_name": first, "last_name": last, "sex": sex,
                    "birth_date": _birth_for_age(rng, rng.randint(25, 55)).isoformat(),
                    "hire_date": (AS_OF - timedelta(days=rng.randint(200, 900))).isoformat(),
                }
        return out

    def _new_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter:05d}"

    def _principal(self, rng: random.Random, company) -> Person:
        sex = rng.choice("MF")
        first, last = self._name(rng, sex)
        return Person("principal", self._new_id(company.id_prefix), first, last,
                      _birth_for_age(rng, rng.randint(21, 55)), sex,
                      hire_date=AS_OF + timedelta(days=rng.randint(-25, 14)),
                      plan_code=rng.choice(company.plan_codes))

    def _dependents(self, rng: random.Random, p: Person, plan_code: str) -> list[Person]:
        plan = PLANS[plan_code]
        deps = []
        if rng.random() < 0.35:
            sex = "F" if p.sex == "M" else "M"
            first, _ = self._name(rng, sex)
            deps.append(Person("spouse", None, first, p.last_name, _birth_for_age(rng, rng.randint(21, 55)), sex,
                               principal_employee_id=p.employee_id))
        for _ in range(rng.choice([0, 0, 1, 2, 3])):
            sex = rng.choice("MF")
            first, _ = self._name(rng, sex)
            deps.append(Person("child", None, first, p.last_name,
                               _birth_for_age(rng, rng.randint(0, plan.child_max_age - 1)), sex,
                               principal_employee_id=p.employee_id))
        if plan.parents_allowed and rng.random() < 0.2:
            sex = rng.choice("MF")
            first, _ = self._name(rng, sex)
            deps.append(Person("parent", None, first, p.last_name,
                               _birth_for_age(rng, rng.randint(48, plan.parent_max_age - 1)), sex,
                               principal_employee_id=p.employee_id))
        return deps

    # ---- defects -------------------------------------------------------------------------
    def _apply_defect(self, rng: random.Random, kind: str, people: list[Person], company, dep_only: bool) -> bool:
        lone = [p for p in people if p.relationship == "principal"
                and not any(d.principal_employee_id == p.employee_id for d in people) and not p.flags]
        principals = [p for p in people if p.relationship == "principal" and p.plan_code in PLANS and not p.flags]

        if kind == "missing_field" and lone:
            p = rng.choice(lone)
            f = rng.choice(["birth_date", "sex", "hire_date", "plan_code", "employee_id"])
            setattr(p, f, None)
            p.display[f] = ""
            p.flags.append(("missing_required", f))
            return True
        if kind == "bad_date" and lone:
            p = rng.choice(lone)
            p.display["birth_date"] = rng.choice(["13/45/1990", "1990-02-30", "31/31/88", "unknown"])
            p.birth_date = None
            p.flags.append(("unparseable_date", "birth_date"))
            return True
        if kind == "duplicate" and lone:
            p = rng.choice(lone)
            copy = Person(**{k: v for k, v in p.__dict__.items() if k not in ("display", "flags")})
            copy.flags.append(("duplicate_in_file", None))
            people.insert(people.index(p) + 1, copy)
            p.flags.append(("_has_duplicate", None))
            return True
        if kind == "unknown_plan" and lone:
            p = rng.choice(lone)
            p.plan_code = rng.choice(["PLATINUM", "DIAMOND"])
            p.flags.append(("unknown_plan", "plan_code"))
            return True
        if kind == "plan_not_in_contract" and lone:
            outside = [c for c in PLANS if c not in company.plan_codes]
            if outside:
                p = rng.choice(lone)
                p.plan_code = rng.choice(outside)
                p.flags.append(("plan_not_in_contract", "plan_code"))
                return True
        if kind == "future_birth" and lone:
            p = rng.choice(lone)
            p.birth_date = AS_OF + timedelta(days=rng.randint(10, 200))
            p.flags.append(("invalid_birth_date", "birth_date"))
            return True
        if kind == "invalid_sex" and lone:
            p = rng.choice(lone)
            p.display["sex"] = rng.choice(["X", "N/A", "?"])
            p.sex = None
            p.flags.append(("invalid_value", "sex"))
            return True
        if kind == "late_endorsement" and lone:
            p = rng.choice(lone)
            p.hire_date = AS_OF - timedelta(days=rng.randint(LATE_ENDORSEMENT_DAYS + 10, 120))
            return True  # info flag added in _label
        if kind == "already_enrolled" and not dep_only:
            eid, rec = rng.choice(sorted(self.existing[company.company_id].items()))
            p = Person("principal", eid, rec["first_name"], rec["last_name"], date.fromisoformat(rec["birth_date"]),
                       rec["sex"], hire_date=date.fromisoformat(rec["hire_date"]), plan_code=rec["plan_code"])
            p.flags.append(("already_enrolled", "employee_id"))
            people.insert(rng.randint(0, len(people)), p)
            return True
        if kind == "overage_child" and principals:
            p = rng.choice(principals)
            plan = PLANS[p.plan_code]
            sex = rng.choice("MF")
            first, _ = self._name(rng, sex)
            child = Person("child", None, first, p.last_name, _birth_for_age(rng, plan.child_max_age + 2), sex,
                           principal_employee_id=p.employee_id)
            child.flags.append(("dependent_over_age", "birth_date"))
            people.append(child)
            return True
        if kind == "parent_not_allowed":
            bronze = [p for p in principals if p.plan_code == "BRONZE"]
            if bronze:
                p = rng.choice(bronze)
                sex = rng.choice("MF")
                first, _ = self._name(rng, sex)
                parent = Person("parent", None, first, p.last_name, _birth_for_age(rng, 58), sex,
                                principal_employee_id=p.employee_id)
                parent.flags.append(("dependent_not_allowed", "relationship"))
                people.append(parent)
                return True
        if kind == "missing_principal":
            sex = rng.choice("MF")
            first, last = self._name(rng, sex)
            orphan = Person("child", None, first, last, _birth_for_age(rng, 6), sex,
                            principal_employee_id=f"{company.id_prefix}-99{rng.randint(100, 999)}")
            orphan.flags.append(("dependent_principal_missing", "principal_employee_id"))
            people.append(orphan)
            return True
        return False

    # ---- one request ---------------------------------------------------------------------
    def request(self, index: int, defects: int | None = None) -> tuple[list[Person], object, dict]:
        rng = random.Random(f"{self.seed}:{index}")
        company = COMPANIES[rng.choice(sorted(COMPANIES))]
        dep_only = rng.random() < 0.2
        people: list[Person] = []
        if dep_only:
            for eid in rng.sample(sorted(self.existing[company.company_id]), rng.randint(1, 3)):
                rec = self.existing[company.company_id][eid]
                holder = Person("principal", eid, rec["first_name"], rec["last_name"], None, rec["sex"])
                deps = self._dependents(rng, holder, rec["plan_code"]) or [
                    Person("child", None, self._name(rng, "F")[0], rec["last_name"], _birth_for_age(rng, 1), "F",
                           principal_employee_id=eid)]
                people.extend(deps)
        else:
            for _ in range(rng.randint(3, 12)):
                p = self._principal(rng, company)
                people.append(p)
                people.extend(self._dependents(rng, p, p.plan_code))

        n_defects = defects if defects is not None else (0 if rng.random() < 0.45 else rng.choice([1, 1, 1, 2]))
        kinds = ["missing_field", "bad_date", "duplicate", "unknown_plan", "plan_not_in_contract", "future_birth",
                 "invalid_sex", "late_endorsement", "already_enrolled", "overage_child", "parent_not_allowed",
                 "missing_principal"]
        applied = []
        tries = 0
        while len(applied) < n_defects and tries < 40:
            tries += 1
            k = rng.choice(kinds)
            if k in ("missing_principal", "already_enrolled") and rng.random() < 0.6:
                continue  # these always apply, so damp them to keep defect types balanced
            if k not in applied and self._apply_defect(rng, k, people, company, dep_only):
                applied.append(k)

        style = {
            "headers": rng.randrange(len(HEADER_STYLES)),
            "date": rng.choice(DATE_STYLES),
            "rel": rng.randrange(len(REL_STYLES)),
            "caps": rng.random() < 0.3,
            "plan_as_name": rng.random() < 0.4,
            "sex_words": rng.random() < 0.4,
            "title_row": rng.random() < 0.4,
            "extra_cols": rng.random() < 0.5,
            "xlsx": rng.random() < 0.4,
            "native_dates": rng.random() < 0.5,
            "defects": applied,
        }
        return people, company, style

    def _label(self, people: list[Person]) -> list[dict]:
        flags = []
        for row, p in enumerate(people, start=1):
            for code, f in p.flags:
                if not code.startswith("_"):
                    flags.append({"code": code, "row": row, "field": f})
            if p.relationship == "principal" and p.hire_date and (AS_OF - p.hire_date).days > LATE_ENDORSEMENT_DAYS:
                flags.append({"code": "late_endorsement", "row": row, "field": "hire_date"})
        return flags

    # ---- writing -------------------------------------------------------------------------
    def write(self, people: list[Person], company, style: dict, path_stem: Path) -> Path:
        headers = HEADER_STYLES[style["headers"]]
        rel_names = REL_STYLES[style["rel"]]
        fields = list(headers)
        extra = ["Department", "Remarks"] if style["extra_cols"] else []

        def fmt_date(d: date | None):
            if d is None:
                return ""
            if style["xlsx"] and style["native_dates"]:
                return datetime(d.year, d.month, d.day)
            return d.strftime(style["date"])

        def name(v):
            return (v.upper() if style["caps"] else v) if v else ""

        rows = []
        for p in people:
            vals = {
                "employee_id": p.employee_id or "",
                "first_name": name(p.first_name), "last_name": name(p.last_name),
                "full_name": f"{name(p.last_name)}, {name(p.first_name)}",
                "birth_date": fmt_date(p.birth_date),
                "sex": ({"M": "Male", "F": "Female"}[p.sex] if style["sex_words"] else p.sex) if p.sex else "",
                "relationship": (BY_SEX[(p.relationship, p.sex)] if rel_names == "by_sex" and p.sex
                                 else (rel_names if rel_names != "by_sex" else REL_STYLES[0])[p.relationship]),
                "principal_employee_id": p.principal_employee_id or "",
                "hire_date": fmt_date(p.hire_date) if p.relationship == "principal" else "",
                "plan_code": ((PLANS[p.plan_code].name if style["plan_as_name"] and p.plan_code in PLANS else p.plan_code)
                              if p.relationship == "principal" and p.plan_code else ""),
            }
            vals.update(p.display)
            if p.relationship != "principal":
                vals["employee_id"] = ""
            row = [vals[f] for f in fields] + (["Operations", ""] if extra else [])
            rows.append(row)

        header_row = [headers[f] for f in fields] + extra
        title = [f"{company.name} - HMO Enrollment List"] if style["title_row"] else None

        if style["xlsx"]:
            from openpyxl import Workbook

            wb = Workbook()
            ws = wb.active
            if title:
                ws.append(title)
                ws.append([])
            ws.append(header_row)
            for r in rows:
                ws.append(r)
            path = path_stem.with_suffix(".xlsx")
            wb.save(path)
        else:
            path = path_stem.with_suffix(".csv")
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if title:
                    w.writerow(title)
                    w.writerow([])
                w.writerow(header_row)
                for r in rows:
                    w.writerow([v.strftime("%Y-%m-%d") if isinstance(v, datetime) else v for v in r])
        return path

    def label(self, req_id: str, path: Path, company, people: list[Person], style: dict) -> dict:
        flags = self._label(people)
        blocking = [f for f in flags if f["code"] != "late_endorsement"]
        members = []
        for row, p in enumerate(people, start=1):
            c = p.canonical()
            if p.relationship != "principal":
                c["employee_id"] = None
                c["hire_date"] = None
                c["plan_code"] = None
            c["row"] = row
            members.append(c)
        return {"id": req_id, "file": path.name, "company_id": company.company_id, "as_of": AS_OF.isoformat(),
                "defects": style["defects"], "expected_all_clear": not blocking,
                "expected_flags": sorted(flags, key=lambda f: (f["row"], f["code"])), "expected_members": members}


def generate(out_dir: str | Path, count: int = 100, seed: int = 2026) -> list[dict]:
    out = Path(out_dir)
    (out / "requests").mkdir(parents=True, exist_ok=True)
    gen = Generator(seed)
    labels = []
    for i in range(1, count + 1):
        people, company, style = gen.request(i)
        req_id = f"req_{i:03d}"
        path = gen.write(people, company, style, out / "requests" / req_id)
        labels.append(gen.label(req_id, path, company, people, style))
    (out / "labels.jsonl").write_text("\n".join(json.dumps(l) for l in labels) + "\n")
    existing = {cid: {eid: {"plan_code": r["plan_code"]} for eid, r in m.items()} for cid, m in gen.existing.items()}
    (out / "existing_members.json").write_text(json.dumps(existing, indent=2))
    return labels


if __name__ == "__main__":
    import sys

    labels = generate(sys.argv[1] if len(sys.argv) > 1 else "data/generated")
    clear = sum(l["expected_all_clear"] for l in labels)
    print(f"wrote {len(labels)} requests: {clear} clear, {len(labels) - clear} needing review")
