from datetime import date, datetime

import pytest
from openpyxl import Workbook

from enrollment_desk.models import Relationship, Severity
from enrollment_desk.roster_parser import map_header, parse_date, parse_roster
from enrollment_desk.rules import validate

TODAY = date(2026, 10, 15)


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def codes(flags):
    return sorted((f.code, f.row) for f in flags)


@pytest.mark.parametrize("header,field", [
    ("Emp #", "employee_id"), ("EMP_NO", "employee_id"), ("Bday", "birth_date"), ("Date of Birth", "birth_date"),
    ("Name (Last, First)", "full_name"), ("Member Type", "relationship"), ("Dependent Of", "principal_employee_id"),
    ("Date Hired", "hire_date"), ("Benefit Plan", "plan_code"), ("Department", None),
])
def test_header_mapping(header, field):
    assert map_header(header) == field


@pytest.mark.parametrize("text,expected", [
    ("1990-04-08", date(1990, 4, 8)), ("04/08/1990", date(1990, 4, 8)), ("08-Apr-1990", date(1990, 4, 8)),
    ("April 08, 1990", date(1990, 4, 8)), (datetime(1990, 4, 8), date(1990, 4, 8)), ("", None),
])
def test_dates(text, expected):
    assert parse_date(text) == expected


def test_bad_date_raises():
    with pytest.raises(ValueError):
        parse_date("1990-13-01")


def test_title_rows_caps_and_full_names(tmp_path):
    p = write(tmp_path, "r.csv",
              "Acme - HMO list\n\nEMP_NO,\"Name (Last, First)\",Bday,Sex,Relation,Principal Emp No,Start Date,Benefit Plan,Remarks\n"
              "DLS-1,\"DELA CRUZ, MARIA CLARA\",04/08/1990,Female,Self,,10/01/2026,Gold,x\n")
    r = parse_roster(p)
    m = r.members[0]
    assert (m.first_name, m.last_name, m.sex, m.relationship, m.plan_code) == (
        "Maria Clara", "Dela Cruz", "F", Relationship.PRINCIPAL, "GOLD")
    assert r.unmapped_headers == ["Remarks"]


def test_xlsx_native_dates(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["Employee ID", "First Name", "Last Name", "Birth Date", "Sex", "Relationship", "Hire Date", "Plan"])
    ws.append(["DLS-1", "Ana", "Cruz", datetime(1995, 1, 2), "F", "Employee", datetime(2026, 10, 1), "SILVER"])
    path = tmp_path / "r.xlsx"
    wb.save(path)
    m = parse_roster(path).members[0]
    assert m.birth_date == date(1995, 1, 2) and m.hire_date == date(2026, 10, 1)


HEADER = "Employee ID,First Name,Last Name,Birth Date,Sex,Relationship,Principal ID,Hire Date,Plan\n"


def run_rules(tmp_path, rows, existing=None, company="CMP-DALISAY"):
    r = parse_roster(write(tmp_path, "r.csv", HEADER + rows))
    return validate(r.members, company, existing or {}, TODAY, r.flags)


def test_clean_roster_has_no_flags(tmp_path):
    flags = run_rules(tmp_path, "D-1,Ana,Cruz,1990-01-01,F,Employee,,2026-10-01,SILVER\n"
                                ",Ben,Cruz,2015-01-01,M,Child,D-1,,\n")
    assert flags == []


def test_each_rule(tmp_path):
    rows = (
        "D-1,Ana,Cruz,1990-01-01,F,Employee,,2026-10-01,SILVER\n"  # 1 ok
        "D-1,Ana,Cruz,1990-01-01,F,Employee,,2026-10-01,SILVER\n"  # 2 duplicate
        "D-2,Bo,Reyes,,M,Employee,,2026-10-01,SILVER\n"  # 3 missing birth date
        "D-3,Cy,Lim,1990-01-01,M,Employee,,2026-10-01,PLATINUM\n"  # 4 unknown plan
        "D-4,Di,Tan,1990-01-01,F,Employee,,2026-10-01,BRONZE\n"  # 5 plan not in contract
        "D-5,Ed,Go,2027-01-01,M,Employee,,2026-10-01,SILVER\n"  # 6 future birth
        "D-6,Fe,Uy,1990-01-01,F,Employee,,2026-06-01,SILVER\n"  # 7 late endorsement (info)
        ",Gi,Cruz,2000-01-01,F,Child,D-1,,\n"  # 8 child 26 > 21
        ",Ho,Cruz,1950-01-01,M,Parent,D-1,,\n"  # 9 parent 76 > 65
        ",Iy,Ong,2010-01-01,F,Child,D-99,,\n"  # 10 principal missing
        "D-7,Jo,Sy,1990-01-01,X,Employee,,2026-10-01,SILVER\n"  # 11 bad sex
        "D-8,Ka,Lu,1990-01-01,F,Employee,,2026-10-01,SILVER\n"  # 12 already enrolled
    )
    flags = run_rules(tmp_path, rows, existing={"D-8": {"plan_code": "SILVER"}})
    assert codes(flags) == sorted([
        ("duplicate_in_file", 2), ("missing_required", 3), ("unknown_plan", 4), ("plan_not_in_contract", 5),
        ("invalid_birth_date", 6), ("late_endorsement", 7), ("dependent_over_age", 8), ("dependent_over_age", 9),
        ("dependent_principal_missing", 10), ("invalid_value", 11), ("already_enrolled", 12),
    ])
    assert [f.severity for f in flags if f.code == "late_endorsement"] == [Severity.INFO]


def test_parents_not_allowed_on_bronze(tmp_path):
    flags = run_rules(tmp_path, "M-1,Ana,Cruz,1990-01-01,F,Employee,,2026-10-01,BRONZE\n"
                                ",Ben,Cruz,1965-01-01,M,Parent,M-1,,\n", company="CMP-MAYON")
    assert codes(flags) == [("dependent_not_allowed", 2)]


def test_dependents_of_existing_principals_use_their_plan(tmp_path):
    flags = run_rules(tmp_path, ",Ben,Cruz,2003-06-01,M,Child,D-8,,\n", existing={"D-8": {"plan_code": "GOLD"}})
    assert flags == []  # 23 is within the Gold limit


def test_unreadable_date_is_not_also_reported_missing(tmp_path):
    flags = run_rules(tmp_path, "D-1,Ana,Cruz,1990-13-01,F,Employee,,2026-10-01,SILVER\n")
    assert codes(flags) == [("unparseable_date", 1)]
