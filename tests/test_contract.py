"""Contract checks for the attendance API. Run: python tests/test_contract.py"""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

os.environ["MONGO_URI"] = os.environ.get("MONGO_URI", "mongodb://127.0.0.1:27017")
os.environ["MONGO_DB"] = "attendance_test_hrone"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bson import json_util
from fastapi.testclient import TestClient

from pymongo.database import Database

import app.main as api
from app.main import app, db, ensure_indexes

IST = timezone(timedelta(hours=5, minutes=30))
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ok  {msg}")
    else:
        FAILS.append(msg)
        print(f"FAIL  {msg}")


def ist_ms(y, m, d, h, mi, s=0) -> int:
    return int(datetime(y, m, d, h, mi, s, tzinfo=IST).timestamp() * 1000)


def working_days(joined: str, month: str) -> int:
    year, mon = (int(p) for p in month.split("-"))
    start = date(year, mon, 1)
    end = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1) - timedelta(days=1)
    day = max(date.fromisoformat(joined), start)
    count = 0
    while day <= end:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def half_up(value: Decimal, places: int) -> float:
    quant = Decimal("1").scaleb(-places)
    return float(value.quantize(quant, rounding=ROUND_HALF_UP))


def expect_pct(present: Decimal, working: int):
    if working == 0:
        return None
    return half_up(present / Decimal(working) * 100, 2)


def expect_rate(present: Decimal, headcount: int):
    if headcount <= 0:
        return None
    return half_up(present / Decimal(headcount), 4)


def expect_avg(values):
    total = sum((Decimal(str(value)) for value in values), Decimal(0))
    return half_up(total / Decimal(len(values)), 2)


def load_sample() -> None:
    opts = json_util.JSONOptions(tz_aware=True)
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((ROOT / "sample_data" / f"{name}.json").read_text(), json_options=opts)
        db[name].delete_many({})
        if docs:
            db[name].insert_many(docs)


def walk_stages(node, found: list[str]) -> None:
    if isinstance(node, dict):
        stage = node.get("stage")
        if stage in ("IXSCAN", "COLLSCAN"):
            found.append(stage)
        for value in node.values():
            walk_stages(value, found)
    elif isinstance(node, list):
        for item in node:
            walk_stages(item, found)


def no_id(payload) -> bool:
    if isinstance(payload, dict):
        if "id" in payload or "_id" in payload:
            return False
        return all(no_id(v) for v in payload.values())
    if isinstance(payload, list):
        return all(no_id(v) for v in payload)
    return True


def main() -> None:
    ensure_indexes()
    load_sample()
    with TestClient(app) as client:
        health = client.get("/health")
        check(health.status_code == 200 and health.json() == {"status": "ok"}, "health 200")

        bad = client.post("/employees", json={
            "emp_code": "nope", "name": "A", "email": "not-an-email",
            "department": "X", "joined_on": "2026-02-31",
            "shift_start": "09:30", "shift_end": "09:30",
        })
        check(bad.status_code == 422, "invalid employee is 422")

        page = client.get("/employees", params={"page": 1, "page_size": 1})
        body = page.json()
        check(page.status_code == 200 and body["items"][0]["emp_code"] == "EMP0001", "page 1 is the first emp_code")
        check(body["total"] == 6 and isinstance(body["items"][0]["created_at"], int), "employee total and created_at millis")
        check(no_id(body), "employee list has no id")
        dept = client.get("/employees", params={"department": "Engineering"})
        check(dept.json()["total"] == 2 and [i["emp_code"] for i in dept.json()["items"]] == ["EMP0001", "EMP0002"],
              "department filter counts and sorts")

        logs = client.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-06"})
        codes = [i["emp_code"] for i in logs.json()["items"]]
        check(codes == ["EMP0001", "EMP0003", "EMP0005"], "same-day attendance sorted by emp_code")
        check(all("id" not in i and isinstance(i["punch_in"], int) for i in logs.json()["items"]), "attendance has millis and no id")
        legacy = client.get("/attendance", params={"emp_code": "EMP0002"})
        leg = legacy.json()["items"][0]
        check(leg["history"] == [] and leg["half_day"] is False and leg["work_hours"] == 8.98, "legacy defaults")
        check(client.get("/attendance", params={"date_from": "2026-07-10", "date_to": "2026-07-01"}).status_code == 422,
              "inverted attendance range is 422")
        check(client.get("/attendance", params={"status": "LEAVE"}).json()["total"] == 1, "status filter total")
        check(client.get("/employees", params={"page_size": 101}).status_code == 422, "page_size cap")

        # Rules on a fresh employee.
        created = client.post("/employees", json={
            "emp_code": "EMP1001", "name": "Rule Check", "email": "rule@example.com",
            "department": "Rules", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-01",
        })
        check(created.status_code == 201 and isinstance(created.json()["created_at"], int), "create employee 201")
        dup = client.post("/employees", json={
            "emp_code": "EMP1001", "name": "Rule Check", "email": "rule@example.com",
            "department": "Rules", "joined_on": "2026-07-01",
        })
        check(dup.status_code == 409, "duplicate emp_code 409")

        def punch(ms, status="PRESENT", code="EMP1001"):
            return client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms, "status": status})

        on_time = punch(ist_ms(2026, 8, 10, 9, 40, 0))
        check(on_time.status_code == 201 and on_time.json()["late_minutes"] == 0 and on_time.json()["date"] == "2026-08-10",
              "09:40:00 is not late")
        boundary = punch(ist_ms(2026, 8, 11, 9, 40, 1))
        check(boundary.status_code == 201 and boundary.json()["late_minutes"] == 10, "09:40:01 is 10 late minutes")
        truncated = punch(ist_ms(2026, 8, 12, 9, 40, 0) + 900)
        check(truncated.status_code == 201 and truncated.json()["late_minutes"] == 0
              and truncated.json()["punch_in"] % 1000 == 0, "sub-second punch truncates and is not late")
        late = punch(ist_ms(2026, 8, 13, 10, 15, 59))
        check(late.json()["late_minutes"] == 45, "10:15:59 is 45 late minutes")
        check(client.post("/attendance/punch-in", json={"emp_code": "EMP4040", "punched_at": ist_ms(2026, 7, 20, 9, 30)}).status_code == 404,
              "unknown punch-in is 404")
        check(client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": 1_700_000_000}).status_code == 422,
              "seconds timestamp is 422")
        check(client.post("/attendance/punch-in", json={
            "emp_code": "EMP1001", "punched_at": ist_ms(2026, 7, 24, 9, 0), "status": "ABSENT"
        }).status_code == 422, "ABSENT is not a punch-in status")
        check(punch(ist_ms(2026, 8, 10, 9, 50)).status_code == 409, "second punch-in same date is 409")

        # 3618 seconds = 1.005 hours -> 1.01, half day.
        pin = punch(ist_ms(2026, 8, 14, 9, 0, 0))
        pout = client.post("/attendance/punch-out", json={
            "emp_code": "EMP1001", "punched_at": ist_ms(2026, 8, 14, 10, 0, 18),
        })
        check(pout.status_code == 200 and pout.json()["work_hours"] == 1.01 and pout.json()["half_day"] is True
              and pout.json()["overtime_minutes"] == 0, "half-up work hours and half day")

        # Overtime threshold and the 24h bound. Shift ends 18:30.
        punch(ist_ms(2026, 8, 17, 9, 0, 0))
        under = client.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": ist_ms(2026, 8, 17, 18, 59, 0)})
        check(under.json()["overtime_minutes"] == 0, "29 minutes overtime is 0")
        punch(ist_ms(2026, 8, 18, 9, 0, 0))
        exact = client.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": ist_ms(2026, 8, 18, 19, 0, 0)})
        check(exact.json()["overtime_minutes"] == 30 and exact.json()["half_day"] is False, "30 minutes overtime counts")
        punch(ist_ms(2026, 8, 19, 9, 0, 0))
        day_bound = client.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": ist_ms(2026, 8, 20, 9, 0, 0)})
        check(day_bound.status_code == 200, "punch-out exactly 24h later is allowed")
        punch(ist_ms(2026, 8, 20, 9, 0, 0))
        too_long = client.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": ist_ms(2026, 8, 21, 9, 0, 1)})
        check(too_long.status_code == 422, "punch-out past 24h is 422")
        still_open = client.get("/attendance", params={"emp_code": "EMP1001", "date_from": "2026-08-20", "date_to": "2026-08-20"})
        check(still_open.json()["items"][0]["punch_out"] is None, "rejected punch-out does not close the record")

        # Overnight: 05:29 IST on the 7th belongs to the 6th and is late from 22:00.
        night = client.post("/employees", json={
            "emp_code": "EMP1002", "name": "Night", "email": "night@example.com",
            "department": "Rules", "shift_start": "22:00", "shift_end": "06:00", "joined_on": "2026-03-01",
        })
        check(night.status_code == 201, "overnight employee")
        early = punch(ist_ms(2026, 7, 7, 5, 29, 0), code="EMP1002")
        check(early.status_code == 201 and early.json()["date"] == "2026-07-06" and early.json()["late_minutes"] == 449,
              "overnight punch before shift_end belongs to the previous day")
        out = client.post("/attendance/punch-out", json={"emp_code": "EMP1002", "punched_at": ist_ms(2026, 7, 7, 6, 40, 0)})
        check(out.status_code == 200 and out.json()["overtime_minutes"] == 40 and out.json()["work_hours"] == 1.18,
              "overnight shift end is the next morning")

        # Regularize.
        same = client.patch("/attendance/EMP1001/2026-08-10", json={"reason": "no actual change", "regularized_by": "hr"})
        check(same.status_code == 422, "no-op regularization is 422")
        cleared = client.patch("/attendance/EMP1001/2026-08-11", json={
            "status": "LEAVE", "reason": "approved leave", "regularized_by": "hr.admin",
        })
        check(cleared.status_code == 200 and cleared.json()["punch_in"] is None and cleared.json()["late_minutes"] == 0
              and cleared.json()["history"][0]["changes"]["status"] == {"from": "PRESENT", "to": "LEAVE"}
              and isinstance(cleared.json()["history"][0]["changes"]["punch_in"]["from"], int)
              and cleared.json()["history"][0]["changes"]["punch_in"]["to"] is None,
              "LEAVE clears punches and appends history with epoch millis")
        stored = db.attendance_logs.find_one({"emp_code": "EMP1001", "date": "2026-08-11"})
        stored_change = stored["history"][0]["changes"]["punch_in"]
        check(isinstance(stored["history"][0]["at"], datetime) and isinstance(stored_change["from"], datetime)
              and stored_change["to"] is None and "id" not in stored and "_id" in stored,
              "history instants are BSON dates and the record id stays internal")
        clash = client.patch("/attendance/EMP1001/2026-08-12", json={
            "status": "ABSENT", "punch_in": ist_ms(2026, 8, 12, 9, 30),
            "reason": "should fail", "regularized_by": "hr",
        })
        check(clash.status_code == 422, "ABSENT plus punch_in is 422")
        moved = client.patch("/attendance/EMP1001/2026-08-12", json={
            "punch_in": ist_ms(2026, 8, 13, 9, 30), "reason": "wrong day", "regularized_by": "hr",
        })
        check(moved.status_code == 422, "punch_in must stay on the attendance date")
        check(client.patch("/attendance/EMP1001/2026-08-01", json={"reason": "missing day", "regularized_by": "hr"}).status_code == 404,
              "missing record is 404")

        # Sample analytics, checked against an independent weekday count and Decimal rounding.
        july = "2026-07"
        wd = working_days("2026-01-05", july)
        check(wd == 23, f"independent July working days is 23 (got {wd})")
        monthly = client.get("/analytics/employees/EMP0001/monthly", params={"month": july}).json()
        check(monthly["working_days"] == 23 and monthly["present_days"] == 2 and monthly["leave_days"] == 0
              and monthly["total_late_minutes"] == 0 and monthly["attendance_pct"] == expect_pct(Decimal(2), 23),
              f"EMP0001 July monthly {monthly}")
        m2 = client.get("/analytics/employees/EMP0002/monthly", params={"month": july}).json()
        check(m2["working_days"] == working_days("2026-07-13", july) == 15 and m2["present_days"] == 1
              and m2["attendance_pct"] == expect_pct(Decimal(1), 15), f"mid-month joiner {m2}")
        m4 = client.get("/analytics/employees/EMP0004/monthly", params={"month": july}).json()
        check(m4["present_days"] == 0.5 and m4["attendance_pct"] == expect_pct(Decimal("0.5"), 23), f"half day present {m4}")
        m3 = client.get("/analytics/employees/EMP0003/monthly", params={"month": july}).json()
        check(m3["leave_days"] == 1 and m3["late_count"] == 1 and m3["total_late_minutes"] == 35
              and m3["total_overtime_minutes"] == 40 and m3["present_days"] == 1
              and m3["attendance_pct"] == expect_pct(Decimal(1), 23), f"leave and late totals {m3}")
        check(client.get("/analytics/employees/EMP9999/monthly", params={"month": july}).status_code == 404, "unknown monthly is 404")

        summary = client.get("/analytics/departments/summary", params={"month": july}).json()
        by_dept = {item["department"]: item for item in summary["items"]}
        check(list(by_dept) == ["Engineering", "Rules", "Sales", "Support"], f"departments sorted {list(by_dept)}")
        eng = by_dept["Engineering"]
        check(eng["headcount"] == 2 and eng["present_days"] == 3 and eng["avg_work_hours"] == 9.04
              and eng["late_count"] == 0, f"engineering summary {eng}")
        sales = by_dept["Sales"]
        check(sales["headcount"] == 2 and sales["present_days"] == 1.5 and sales["avg_work_hours"] == 6.29
              and sales["late_count"] == 1 and sales["total_late_minutes"] == 35
              and sales["leave_count"] == 1 and sales["on_duty_count"] == 0, f"sales summary {sales}")
        support = by_dept["Support"]
        check(support["headcount"] == 2 and support["present_days"] == 2 and support["avg_work_hours"] == 8.75,
              f"support summary ignores open work_hours {support}")

        # Employee with no logs, and one who has not joined yet.
        client.post("/employees", json={
            "emp_code": "EMP1003", "name": "No Logs", "email": "none@example.com",
            "department": "Quiet", "joined_on": "2026-07-01",
        })
        client.post("/employees", json={
            "emp_code": "EMP1004", "name": "Later", "email": "later@example.com",
            "department": "Quiet", "joined_on": "2026-08-01",
        })
        quiet = client.get("/analytics/departments/summary", params={"month": july, "department": "Quiet"}).json()["items"]
        check(len(quiet) == 1 and quiet[0]["headcount"] == 1 and quiet[0]["present_days"] == 0
              and quiet[0]["avg_work_hours"] is None, f"zero-log headcount and future joiner excluded {quiet}")

        board = client.get("/analytics/leaderboard/late", params={"month": july, "limit": 10}).json()["items"]
        night = next(row for row in board if row["emp_code"] == "EMP1002")
        neha = next(row for row in board if row["emp_code"] == "EMP0003")
        check(night["rank"] == 1 and night["total_late_minutes"] == 449 and night["department"] == "Rules",
              f"stored/computed overnight late leads {night}")
        check(neha["total_late_minutes"] == 35 and neha["name"] == "Neha Iyer" and neha["late_count"] == 1,
              f"sample late row {neha}")

        # Ties at the cutoff. Stored late_minutes are authoritative, even if they disagree with the punch.
        for code, late_minutes in (("EMP2001", 100), ("EMP2003", 50), ("EMP2002", 50), ("EMP2004", 10)):
            client.post("/employees", json={
                "emp_code": code, "name": code, "email": f"{code.lower()}@example.com",
                "department": "Rank", "joined_on": "2026-01-01",
            })
            db.attendance_logs.insert_one({
                "emp_code": code, "date": "2026-07-06", "status": "PRESENT",
                "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
                "punch_out": datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
                "work_hours": 9.0, "late_minutes": late_minutes, "overtime_minutes": 0, "half_day": False, "history": [],
            })
        tied = client.get("/analytics/leaderboard/late", params={"month": july, "department": "Rank", "limit": 2}).json()["items"]
        check([row["emp_code"] for row in tied] == ["EMP2001", "EMP2002", "EMP2003"]
              and [row["rank"] for row in tied] == [1, 2, 2], f"tie at cutoff {tied}")
        wider = client.get("/analytics/leaderboard/late", params={"month": july, "department": "Rank", "limit": 4}).json()["items"]
        check([row["rank"] for row in wider] == [1, 2, 2, 4], f"skipped rank {wider}")

        # Weekend late still counts; weekday-only present_days does not include it.
        db.attendance_logs.insert_one({
            "emp_code": "EMP0001", "date": "2026-07-11", "status": "PRESENT",
            "punch_in": datetime(2026, 7, 11, 5, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 11, 13, 0, tzinfo=timezone.utc),
            "work_hours": 8.0, "late_minutes": 7, "overtime_minutes": 15, "half_day": False, "history": [],
        })
        weekend = client.get("/analytics/employees/EMP0001/monthly", params={"month": july}).json()
        check(weekend["present_days"] == 2 and weekend["late_count"] == 1 and weekend["total_late_minutes"] == 7
              and weekend["total_overtime_minutes"] == 15, f"weekend contributes to late/ot only {weekend}")

        trend = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-06", "to": "2026-07-12"}).json()
        rows = {item["date"]: item for item in trend["items"]}
        check(list(rows) == [f"2026-07-{d:02d}" for d in range(6, 13)], "trend has every calendar day")
        check(rows["2026-07-06"]["headcount"] == 1 and rows["2026-07-06"]["present_count"] == 1
              and rows["2026-07-06"]["attendance_rate"] == 1.0, f"monday trend {rows['2026-07-06']}")
        check(rows["2026-07-08"]["present_count"] == 0 and rows["2026-07-08"]["attendance_rate"] == 0.0,
              f"empty working day rate is 0 {rows['2026-07-08']}")
        check(rows["2026-07-08"]["moving_avg_7d"] == expect_rate(Decimal(2), 3), f"moving avg {rows['2026-07-08']}")
        check(rows["2026-07-11"]["is_working_day"] is False and rows["2026-07-11"]["attendance_rate"] is None
              and rows["2026-07-11"]["present_count"] == 1 and rows["2026-07-11"]["late_count"] == 1,
              f"weekend row {rows['2026-07-11']}")
        # Window is Fri 10 through Sun 12 plus the earlier working rates 1,1,0,0,0. Non-null mean is 0.4.
        check(rows["2026-07-12"]["moving_avg_7d"] == 0.4, f"null rates are left out of the average {rows['2026-07-12']}")
        joined = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-13", "to": "2026-07-14"}).json()["items"]
        check(joined[0]["headcount"] == 2 and joined[0]["attendance_rate"] == 0.0
              and joined[1]["present_count"] == 1 and joined[1]["attendance_rate"] == 0.5
              and joined[1]["moving_avg_7d"] == 0.25, f"joiner changes headcount mid-range {joined}")
        check(client.get("/analytics/departments/Missing/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404,
              "unknown department is 404")
        check(client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-10", "to": "2026-07-01"}).status_code == 422,
              "inverted trend range is 422")
        check(client.get("/analytics/departments/Engineering/trend", params={"from": "2026-01-01", "to": "2026-04-03"}).status_code == 422,
              "trend over 92 days is 422")
        check(client.get("/analytics/departments/Engineering/trend", params={"from": "2026-01-01", "to": "2026-04-02"}).status_code == 200,
              "trend of 92 days is allowed")
        weekend_only = client.get(
            "/analytics/departments/Engineering/trend", params={"from": "2026-07-11", "to": "2026-07-12"}
        ).json()["items"]
        check(all(row["attendance_rate"] is None and row["moving_avg_7d"] is None for row in weekend_only)
              and weekend_only[0]["present_count"] == 1,
              f"weekend-only window has null rates {weekend_only}")

        # Mean of stored work_hours, then half-up. 1.005 must not become 1.00, and
        # 0.015 with 0.025 must stay 0.02 (rounding each row first would make 0.03).
        def seed_hours(code, department, rows):
            client.post("/employees", json={
                "emp_code": code, "name": code, "email": f"{code.lower()}@example.com",
                "department": department, "joined_on": "2026-07-01",
            })
            for day, hours in rows:
                db.attendance_logs.insert_one({
                    "emp_code": code, "date": day, "status": "PRESENT",
                    "punch_in": datetime(2026, 7, int(day[-2:]), 4, 0, tzinfo=timezone.utc),
                    "punch_out": datetime(2026, 7, int(day[-2:]), 12, 0, tzinfo=timezone.utc),
                    "work_hours": hours, "late_minutes": 0, "overtime_minutes": 0,
                    "half_day": False, "history": [],
                })

        seed_hours("EMP4101", "AvgOne", [("2026-07-06", 1.005)])
        one = client.get("/analytics/departments/summary", params={"month": july, "department": "AvgOne"}).json()["items"]
        check(len(one) == 1 and one[0]["avg_work_hours"] == expect_avg([1.005]) == 1.01, f"single 1.005 averages to 1.01 {one}")
        seed_hours("EMP4102", "AvgTwo", [("2026-07-06", 0.015), ("2026-07-07", 0.025)])
        two = client.get("/analytics/departments/summary", params={"month": july, "department": "AvgTwo"}).json()["items"]
        check(len(two) == 1 and two[0]["avg_work_hours"] == expect_avg([0.015, 0.025]) == 0.02,
              f"mean then half-up is 0.02 {two}")

        # Rounded work_hours of 4.50 is not a half day; 4.49 is.
        client.post("/employees", json={
            "emp_code": "EMP4103", "name": "Boundary", "email": "bound@example.com",
            "department": "Bounds", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-09-01",
        })
        client.post("/attendance/punch-in", json={"emp_code": "EMP4103", "punched_at": ist_ms(2026, 9, 1, 9, 30, 0)})
        full_half = client.post("/attendance/punch-out", json={"emp_code": "EMP4103", "punched_at": ist_ms(2026, 9, 1, 14, 0, 0)})
        check(full_half.status_code == 200 and full_half.json()["work_hours"] == 4.5 and full_half.json()["half_day"] is False,
              f"4.50 hours is not a half day {full_half.json()}")
        client.post("/attendance/punch-in", json={"emp_code": "EMP4103", "punched_at": ist_ms(2026, 9, 2, 9, 30, 0)})
        under_half = client.post("/attendance/punch-out", json={"emp_code": "EMP4103", "punched_at": ist_ms(2026, 9, 2, 13, 59, 41)})
        check(under_half.status_code == 200 and under_half.json()["work_hours"] == 4.49 and under_half.json()["half_day"] is True,
              f"4.49 hours is a half day {under_half.json()}")
        client.post("/attendance/punch-in", json={"emp_code": "EMP4103", "punched_at": ist_ms(2026, 9, 3, 9, 0, 0)})
        same_instant = client.post("/attendance/punch-out", json={"emp_code": "EMP4103", "punched_at": ist_ms(2026, 9, 3, 9, 0, 0)})
        check(same_instant.status_code == 422, "punch-out equal to punch-in is 422")
        still = client.get("/attendance", params={"emp_code": "EMP4103", "date_from": "2026-09-03", "date_to": "2026-09-03"}).json()
        check(still["items"][0]["punch_out"] is None, "rejected equal punch-out leaves the record open")
        client.post("/attendance/punch-in", json={"emp_code": "EMP4103", "punched_at": ist_ms(2026, 9, 4, 9, 0, 0)})
        floor_ot = client.post("/attendance/punch-out", json={"emp_code": "EMP4103", "punched_at": ist_ms(2026, 9, 4, 18, 59, 59)})
        check(floor_ot.status_code == 200 and floor_ot.json()["overtime_minutes"] == 0, "29 minutes 59 seconds of overtime is 0")

        # Most recent punch_in at or before punched_at, even if an older record is still open.
        client.post("/employees", json={
            "emp_code": "EMP4104", "name": "Closer", "email": "closer@example.com",
            "department": "Bounds", "joined_on": "2026-09-01",
        })
        client.post("/attendance/punch-in", json={"emp_code": "EMP4104", "punched_at": ist_ms(2026, 9, 8, 9, 0, 0)})
        client.post("/attendance/punch-in", json={"emp_code": "EMP4104", "punched_at": ist_ms(2026, 9, 9, 9, 0, 0)})
        earlier = client.post("/attendance/punch-out", json={"emp_code": "EMP4104", "punched_at": ist_ms(2026, 9, 8, 18, 0, 0)})
        check(earlier.status_code == 200 and earlier.json()["date"] == "2026-09-08", "punch-out before the later punch closes the earlier day")
        later = client.post("/attendance/punch-out", json={"emp_code": "EMP4104", "punched_at": ist_ms(2026, 9, 9, 18, 0, 0)})
        check(later.status_code == 200 and later.json()["date"] == "2026-09-09", "later punch-out closes the later day")
        client.post("/attendance/punch-in", json={"emp_code": "EMP4104", "punched_at": ist_ms(2026, 9, 10, 9, 0, 0)})
        client.post("/attendance/punch-in", json={"emp_code": "EMP4104", "punched_at": ist_ms(2026, 9, 11, 9, 0, 0)})
        closed_latest = client.post("/attendance/punch-out", json={"emp_code": "EMP4104", "punched_at": ist_ms(2026, 9, 11, 18, 0, 0)})
        again = client.post("/attendance/punch-out", json={"emp_code": "EMP4104", "punched_at": ist_ms(2026, 9, 11, 19, 0, 0)})
        older = client.get("/attendance", params={"emp_code": "EMP4104", "date_from": "2026-09-10", "date_to": "2026-09-10"}).json()
        check(closed_latest.status_code == 200 and closed_latest.json()["date"] == "2026-09-11"
              and again.status_code == 409 and older["items"][0]["punch_out"] is None,
              "a second punch-out is 409 and does not close the older open record")
        check(client.post("/attendance/punch-out", json={"emp_code": "EMP4040", "punched_at": ist_ms(2026, 9, 1, 18, 0)}).status_code == 404,
              "unknown punch-out is 404")
        check(client.post("/attendance/punch-out", json={"emp_code": "EMP4101", "punched_at": ist_ms(2026, 6, 1, 9, 0)}).status_code == 404,
              "punch-out with no earlier punch-in is 404")

        # Exactly shift_end is not "earlier than shift_end", so it stays on that IST date.
        client.post("/employees", json={
            "emp_code": "EMP4105", "name": "Edge Night", "email": "edge-night@example.com",
            "department": "Bounds", "shift_start": "22:00", "shift_end": "06:00", "joined_on": "2026-03-01",
        })
        at_end = client.post("/attendance/punch-in", json={"emp_code": "EMP4105", "punched_at": ist_ms(2026, 7, 8, 6, 0, 0)})
        check(at_end.status_code == 201 and at_end.json()["date"] == "2026-07-08" and at_end.json()["late_minutes"] == 0,
              f"punch exactly at overnight shift_end stays on that date {at_end.json()}")
        just_before = client.post("/attendance/punch-in", json={"emp_code": "EMP4105", "punched_at": ist_ms(2026, 7, 10, 5, 59, 59)})
        check(just_before.status_code == 201 and just_before.json()["date"] == "2026-07-09"
              and just_before.json()["late_minutes"] == 479,
              f"one second before shift_end belongs to the previous day {just_before.json()}")

        # Explain plans: IXSCAN, no COLLSCAN.
        explains = [
            ("/admin/explain/attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31", "page": 1, "page_size": 20}),
            ("/admin/explain/attendance_list", {"emp_code": "EMP0001"}),
            ("/admin/explain/attendance_list", {"status": "PRESENT"}),
            ("/admin/explain/employee_monthly", {"emp_code": "EMP0001", "month": july}),
            ("/admin/explain/department_summary", {"month": july}),
            ("/admin/explain/department_summary", {"month": july, "department": "Engineering"}),
            ("/admin/explain/late_leaderboard", {"month": july, "limit": 10}),
            ("/admin/explain/department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"}),
        ]
        for path, params in explains:
            res = client.get(path, params=params)
            stages: list[str] = []
            if res.status_code == 200:
                walk_stages(res.json()["explain"], stages)
            check(res.status_code == 200 and "IXSCAN" in stages and "COLLSCAN" not in stages,
                  f"explain {path} {params} -> {res.status_code} stages={set(stages)}")
        check(client.get("/admin/explain/employee_monthly").status_code == 422, "explain missing params is 422")
        legacy_fix = client.patch("/attendance/EMP0002/2026-07-14", json={
            "status": "WFH", "reason": "legacy row", "regularized_by": "hr",
        })
        legacy_body = legacy_fix.json()
        check(legacy_fix.status_code == 200 and legacy_body["half_day"] is False and len(legacy_body["history"]) == 1
              and "half_day" not in legacy_body["history"][0]["changes"] and legacy_body["work_hours"] == 8.98,
              f"legacy record with no history can be corrected {legacy_body.get('history')}")

        # Races. Unique indexes plus compare-and-swap, so one winner.
        race_emp = {
            "emp_code": "EMP3001", "name": "Race", "email": "race@example.com",
            "department": "Race", "joined_on": "2026-07-01",
        }

        def create_same():
            return client.post("/employees", json=race_emp).status_code

        codes = list(ThreadPoolExecutor(max_workers=8).map(lambda _: create_same(), range(8)))
        check(codes.count(201) == 1 and codes.count(409) == 7, f"parallel creates {codes}")

        def punch_same():
            return client.post("/attendance/punch-in", json={
                "emp_code": "EMP3001", "punched_at": ist_ms(2026, 8, 3, 9, 31),
            }).status_code

        pcodes = list(ThreadPoolExecutor(max_workers=8).map(lambda _: punch_same(), range(8)))
        check(pcodes.count(201) == 1 and pcodes.count(409) == 7, f"parallel punch-ins {pcodes}")

        def punch_out_same():
            return client.post("/attendance/punch-out", json={
                "emp_code": "EMP3001", "punched_at": ist_ms(2026, 8, 3, 18, 40),
            }).status_code

        ocodes = list(ThreadPoolExecutor(max_workers=8).map(lambda _: punch_out_same(), range(8)))
        check(ocodes.count(200) == 1 and ocodes.count(409) == 7, f"parallel punch-outs {ocodes}")

        def patch_same(i):
            return client.patch("/attendance/EMP3001/2026-08-03", json={
                "status": "WFH" if i % 2 == 0 else "ON_DUTY",
                "reason": f"race edit {i}", "regularized_by": "hr",
            }).status_code

        rcodes = list(ThreadPoolExecutor(max_workers=8).map(patch_same, range(8)))
        saved = client.get("/attendance", params={"emp_code": "EMP3001", "date_from": "2026-08-03", "date_to": "2026-08-03"}).json()
        hist = saved["items"][0]["history"]
        # Overlapping edits 409. Edits that land one after another each append.
        # A repeat of the status already stored is 422. History length must equal the winners.
        check(rcodes.count(200) == len(hist) >= 1 and set(rcodes) <= {200, 409, 422},
              f"parallel regularize {rcodes} history={len(hist)}")

        original_command = Database.command

        def fail_ping(self, command, *args, **kwargs):
            if command == "ping":
                raise RuntimeError("down")
            return original_command(self, command, *args, **kwargs)

        Database.command = fail_ping
        try:
            down = client.get("/health")
            check(down.status_code == 503 and isinstance(down.json().get("detail"), str),
                  f"health 503 when ping fails {down.status_code} {down.text}")
        finally:
            Database.command = original_command

        # A failed ping during startup must not prevent the process from serving.
        previous_wait = api.STARTUP_WAIT_SECONDS
        api.STARTUP_WAIT_SECONDS = 0.4

        def fail_startup_ping(self, command, *args, **kwargs):
            if command == "ping":
                raise RuntimeError("down")
            return original_command(self, command, *args, **kwargs)

        Database.command = fail_startup_ping
        try:
            with TestClient(app) as started:
                startup = started.get("/health")
                check(startup.status_code == 503 and isinstance(startup.json().get("detail"), str),
                      f"failed startup ping keeps the app up {startup.status_code} {startup.text}")
        except Exception as exc:
            check(False, f"failed startup ping killed the app: {exc}")
        finally:
            Database.command = original_command
            api.STARTUP_WAIT_SECONDS = previous_wait

    if FAILS:
        print(f"\n{len(FAILS)} failed")
        for item in FAILS:
            print(" -", item)
        sys.exit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
