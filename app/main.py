"""
Employee Attendance & Analytics API.

Run from the repository root:
    uvicorn app.main:app --port 8000

MONGO_URI and MONGO_DB come from the environment. A local .env is loaded only
for variables that are not already set. Indexes are created once MongoDB
answers. If it does not, the process still serves and /health returns 503.
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from bson.son import SON
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB")

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
ABSENCE = ["ABSENT", "LEAVE"]
CHANGE_FIELDS = [
    "status",
    "punch_in",
    "punch_out",
    "work_hours",
    "late_minutes",
    "overtime_minutes",
    "half_day",
]

IDX_EMP_CODE = "emp_code_unique"
IDX_DEPT_EMP = "department_emp_code"
IDX_DEPT_JOINED = "department_joined_on"
IDX_JOINED = "joined_on"
IDX_EMP_DATE = "emp_code_date_unique"
IDX_DATE_EMP = "date_emp_code"
IDX_STATUS_DATE = "status_date_emp_code"
IDX_EMP_PUNCH = "emp_code_punch_in"

# One attempt must fit several tries inside the ready window. Startup stops
# retrying after this budget and keeps serving; it does not exit.
PING_TIMEOUT_MS = 4000
STARTUP_WAIT_SECONDS = 12.0

client: MongoClient | None = None
db = None
_indexes_ready = False
if MONGO_URI and MONGO_DB_NAME:
    client = MongoClient(
        MONGO_URI,
        tz_aware=True,
        tzinfo=timezone.utc,
        serverSelectionTimeoutMS=PING_TIMEOUT_MS,
    )
    db = client[MONGO_DB_NAME]


# --------------------------------------------------------------------------- #
# Time and business rules (R1-R5, R8)
# --------------------------------------------------------------------------- #
def utc_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def coerce_dt(value: datetime | None) -> datetime | None:
    """Treat a stored instant as UTC and drop sub-second precision (R1)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def dt_to_millis(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def millis_to_dt(ms: int) -> datetime:
    ms = (int(ms) // 1000) * 1000
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def at_ist(day: str, hhmm: str) -> datetime:
    year, month, day_n = (int(part) for part in day.split("-"))
    hour, minute = parse_hhmm(hhmm)
    return datetime(year, month, day_n, hour, minute, tzinfo=IST)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """IST calendar date of punch_in. Early overnight punches belong to the previous day (R1)."""
    local = coerce_dt(punch_in).astimezone(IST)
    if is_overnight(shift_start, shift_end):
        end_h, end_m = parse_hhmm(shift_end)
        if local.hour * 3600 + local.minute * 60 + local.second < end_h * 3600 + end_m * 60:
            local -= timedelta(days=1)
    return local.date().isoformat()


def compute_late_minutes(punch_in: datetime, day: str, shift_start: str) -> int:
    """Late only after a strict 10:00 grace; the count is from shift start, not from grace end (R2)."""
    delta = coerce_dt(punch_in) - at_ist(day, shift_start)
    if delta <= timedelta(minutes=10):
        return 0
    return int(delta.total_seconds() // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((coerce_dt(punch_out) - coerce_dt(punch_in)).total_seconds())
    quantized = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(quantized)


def compute_overtime_minutes(punch_out: datetime, day: str, shift_start: str, shift_end: str) -> int:
    end = at_ist(day, shift_end)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    minutes = int((coerce_dt(punch_out) - end).total_seconds() // 60)
    if minutes < 30:
        return 0
    return minutes


def is_half_day(work_hours: float) -> bool:
    cents = (Decimal(str(work_hours)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return cents < 450


def derive_fields(
    status: str,
    punch_in: datetime | None,
    punch_out: datetime | None,
    day: str,
    shift_start: str,
    shift_end: str,
) -> tuple[float | None, int, int, bool]:
    if status in ABSENCE:
        return None, 0, 0, False
    late = compute_late_minutes(punch_in, day, shift_start) if punch_in else 0
    if punch_in is None or punch_out is None:
        return None, late, 0, False
    hours = compute_work_hours(punch_in, punch_out)
    return hours, late, compute_overtime_minutes(punch_out, day, shift_start, shift_end), is_half_day(hours)


def span_ok(punch_in: datetime, punch_out: datetime) -> bool:
    delta = coerce_dt(punch_out) - coerce_dt(punch_in)
    return timedelta(0) < delta <= timedelta(hours=24)


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = (int(part) for part in month.split("-"))
    start = date(year, mon, 1)
    end = date(year + 1, 1, 1) - timedelta(days=1) if mon == 12 else date(year, mon + 1, 1) - timedelta(days=1)
    return start, end


def present_days_number(present_twice: int) -> int | float:
    if present_twice % 2 == 0:
        return present_twice // 2
    return present_twice / 2


def attendance_pct(present_twice: int, working_days: int) -> float | None:
    """present_days / working_days * 100, 2 decimals half-up. Null when there are no working days."""
    if working_days <= 0:
        return None
    basis = (present_twice * 5000 + working_days // 2) // working_days
    return float(Decimal(basis) / Decimal(100))


def avg_work_hours(total: Any, count: int) -> float | None:
    """Mean of the stored hours, then 2 decimals half-up. Null when nothing was summed."""
    if count <= 0:
        return None
    if isinstance(total, Decimal128):
        amount = total.to_decimal()
    elif isinstance(total, Decimal):
        amount = total
    else:
        amount = Decimal(str(total))
    mean = amount / Decimal(count)
    return float(mean.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def bps_to_rate(bps: int) -> float:
    return float(Decimal(int(bps)) / Decimal(10000))


def as_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, float):
        return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return int(value)


def hours_cents(value: Any) -> int | None:
    if value is None:
        return None
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def _instant(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return dt_to_millis(value)
    return value


def serialize_employee(doc: dict) -> dict:
    created = doc["created_at"]
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_millis(created) if isinstance(created, datetime) else created,
    }


def serialize_attendance(doc: dict) -> dict:
    history = []
    for entry in doc.get("history") or []:
        changes = {}
        for key, pair in (entry.get("changes") or {}).items():
            old, new = pair.get("from"), pair.get("to")
            if key in ("punch_in", "punch_out"):
                old, new = _instant(old), _instant(new)
            changes[key] = {"from": old, "to": new}
        at = entry.get("at")
        history.append({
            "at": dt_to_millis(at) if isinstance(at, datetime) else at,
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes,
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _instant(doc.get("punch_in")),
        "punch_out": _instant(doc.get("punch_out")),
        "work_hours": doc["work_hours"] if "work_hours" in doc else None,
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc["half_day"]) if "half_day" in doc else False,
        "history": history,
    }


def validation_error(message: str, loc: list[str]) -> None:
    raise HTTPException(status_code=422, detail=[{"loc": loc, "msg": message, "type": "value_error"}])


def parse_iso_date(value: str, loc: list[str]) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        validation_error("invalid date", loc)
        raise AssertionError("unreachable")


def require_db():
    if db is None:
        raise HTTPException(status_code=503, detail="database is not configured")
    return db


# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    global _indexes_ready
    database = require_db()
    database.employees.create_index([("emp_code", 1)], unique=True, name=IDX_EMP_CODE)
    database.employees.create_index([("department", 1), ("emp_code", 1)], name=IDX_DEPT_EMP)
    database.employees.create_index([("department", 1), ("joined_on", 1)], name=IDX_DEPT_JOINED)
    database.employees.create_index([("joined_on", 1)], name=IDX_JOINED)
    database.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name=IDX_EMP_DATE
    )
    database.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name=IDX_DATE_EMP)
    database.attendance_logs.create_index(
        [("status", 1), ("date", -1), ("emp_code", 1)], name=IDX_STATUS_DATE
    )
    database.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name=IDX_EMP_PUNCH)
    _indexes_ready = True


def prepare_database() -> None:
    """Ping and create indexes. Give up when the budget ends; do not raise."""
    if client is None or db is None:
        return
    deadline = time.monotonic() + STARTUP_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            client.admin.command("ping")
            ensure_indexes()
            return
        except Exception:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    prepare_database()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_real(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be a real YYYY-MM-DD date") from exc
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Query and pipeline builders (shared with /admin/explain)
# --------------------------------------------------------------------------- #
def attendance_list_spec(
    emp_code: str | None,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
    page: int,
    page_size: int,
) -> tuple[dict, SON, int, int, str]:
    start = parse_iso_date(date_from, ["query", "date_from"]) if date_from else None
    end = parse_iso_date(date_to, ["query", "date_to"]) if date_to else None
    if start and end and start > end:
        validation_error("date_from must be on or before date_to", ["query", "date_from"])
    filt: dict[str, Any] = {}
    if emp_code is not None:
        filt["emp_code"] = emp_code
    if start or end:
        clause: dict[str, str] = {}
        if start:
            clause["$gte"] = start.isoformat()
        if end:
            clause["$lte"] = end.isoformat()
        filt["date"] = clause
    if status is not None:
        filt["status"] = status
    if emp_code is not None:
        hint = IDX_EMP_DATE
    elif status is not None:
        hint = IDX_STATUS_DATE
    else:
        hint = IDX_DATE_EMP
    sort = SON([("date", -1), ("emp_code", 1)])
    return filt, sort, (page - 1) * page_size, page_size, hint


def _weekday_expr(date_field: str) -> dict:
    return {
        "$lte": [
            {
                "$isoDayOfWeek": {
                    "$dateFromString": {
                        "dateString": {"$ifNull": [date_field, "1970-01-05"]},
                        "onError": utc_midnight(date(1970, 1, 5)),
                        "onNull": utc_midnight(date(1970, 1, 5)),
                    }
                }
            },
            5,
        ]
    }


def _present_twice_expr(status: str, half: str, *, weekdays: bool, date_field: str) -> dict:
    amount = {"$cond": [{"$eq": [{"$ifNull": [half, False]}, True]}, 1, 2]}
    checks: list[dict] = [{"$in": [status, PRESENCE]}]
    if weekdays:
        checks.append(_weekday_expr(date_field))
    return {"$cond": [{"$and": checks}, amount, 0]}


def _late_count_expr(field: str) -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": [field, 0]}, 0]}, 1, 0]}


def _work_sum_expr(status: str, hours: str) -> dict:
    """Sum stored work_hours as decimals. $round is half-to-even, so the mean is rounded later."""
    numeric = {"$in": [{"$type": hours}, ["double", "int", "long", "decimal"]]}
    return {
        "$sum": {
            "$cond": [
                {"$and": [{"$in": [status, PRESENCE]}, numeric]},
                {"$toDecimal": hours},
                None,
            ]
        }
    }


def _working_days_expr(joined_field: str, month_start: datetime, n_days: int) -> dict:
    return {
        "$size": {
            "$filter": {
                "input": {"$range": [0, n_days]},
                "as": "i",
                "cond": {
                    "$let": {
                        "vars": {
                            "d": {
                                "$dateAdd": {
                                    "startDate": month_start,
                                    "unit": "day",
                                    "amount": "$$i",
                                }
                            }
                        },
                        "in": {
                            "$and": [
                                {"$lte": [{"$isoDayOfWeek": "$$d"}, 5]},
                                {
                                    "$gte": [
                                        {"$dateToString": {"format": "%Y-%m-%d", "date": "$$d"}},
                                        joined_field,
                                    ]
                                },
                            ]
                        },
                    }
                },
            }
        }
    }


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_twice": {
                                "$sum": _present_twice_expr(
                                    "$status", "$half_day", weekdays=True, date_field="$date"
                                )
                            },
                            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "late_count": {"$sum": _late_count_expr("$late_minutes")},
                            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "joined_on": 1,
                "working_days": _working_days_expr("$joined_on", utc_midnight(start), (end - start).days + 1),
                "stats": {
                    "$ifNull": [
                        {"$first": "$stats"},
                        {
                            "present_twice": 0,
                            "leave_days": 0,
                            "late_count": 0,
                            "total_late_minutes": 0,
                            "total_overtime_minutes": 0,
                        },
                    ]
                },
            }
        },
    ]


def department_summary_pipeline(month: str, department: str | None) -> tuple[list[dict], str]:
    start, end = month_bounds(month)
    filt: dict[str, Any] = {"joined_on": {"$lte": end.isoformat()}}
    hint = IDX_JOINED
    if department is not None:
        filt["department"] = department
        hint = IDX_DEPT_JOINED
    pipeline = [
        {"$match": filt},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}}
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "emps": {"$addToSet": "$emp_code"},
                "present_twice": {
                    "$sum": _present_twice_expr(
                        "$logs.status", "$logs.half_day", weekdays=True, date_field="$logs.date"
                    )
                },
                "wh_sum": _work_sum_expr("$logs.status", "$logs.work_hours"),
                "wh_n": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", PRESENCE]},
                                    {"$in": [{"$type": "$logs.work_hours"}, ["double", "int", "long", "decimal"]]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": _late_count_expr("$logs.late_minutes")},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {"$match": {"emps.0": {"$exists": True}}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emps"},
                "present_twice": 1,
                "wh_sum": 1,
                "wh_n": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]
    return pipeline, hint


def leaderboard_pipeline(month: str, limit: int, department: str | None) -> list[dict]:
    start, end = month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": _late_count_expr("$late_minutes")},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    if department is not None:
        pipeline.append({"$match": {"department": department}})
    # $rank is competition ranking (1, 2, 2, 4). Filter on rank, never $limit:
    # a tie sitting on the cutoff must all be returned.
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": SON([("total_late_minutes", -1), ("emp_code", 1)])},
    ])
    return pipeline


def _rate_bps_expr() -> dict:
    # round_half_up(present_count / headcount, 4) * 10000, in integer arithmetic.
    return {
        "$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {
                "$toInt": {
                    "$floor": {
                        "$divide": [
                            {
                                "$add": [
                                    {"$multiply": ["$present_twice", 5000]},
                                    {"$floor": {"$divide": ["$headcount", 2]}},
                                ]
                            },
                            "$headcount",
                        ]
                    }
                }
            },
            None,
        ]
    }


def _moving_bps_expr() -> dict:
    return {
        "$cond": [
            {"$gt": ["$n_rates", 0]},
            {
                "$toInt": {
                    "$floor": {
                        "$divide": [
                            {
                                "$add": [
                                    "$sum_bps",
                                    {"$floor": {"$divide": ["$n_rates", 2]}},
                                ]
                            },
                            "$n_rates",
                        ]
                    }
                }
            },
            None,
        ]
    }


def department_trend_pipeline(department: str, start: date, end: date) -> list[dict]:
    """One row per calendar day, including weekends and days with no logs.

    The day spine is built in this pipeline ($range / $dateAdd / $unwind), not in Python.
    $densify would drop every field except the date, including the roster each day's
    headcount is counted from, so the spine is generated explicitly instead.
    """
    n_days = (end - start).days + 1
    start_dt = utc_midnight(start)
    start_s, end_s = start.isoformat(), end.isoformat()
    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": {"$gte": start_s, "$lte": end_s}}}],
                "as": "logs",
            }
        },
        {
            "$facet": {
                "people": [
                    {
                        "$group": {
                            "_id": None,
                            "people": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
                        }
                    }
                ],
                "daily": [
                    {"$unwind": "$logs"},
                    {"$match": {"$expr": {"$lte": ["$joined_on", "$logs.date"]}}},
                    {
                        "$group": {
                            "_id": "$logs.date",
                            "present_twice": {
                                "$sum": _present_twice_expr(
                                    "$logs.status", "$logs.half_day", weekdays=False, date_field="$logs.date"
                                )
                            },
                            "late_count": {"$sum": _late_count_expr("$logs.late_minutes")},
                        }
                    },
                ],
            }
        },
        {
            "$project": {
                "people": {"$ifNull": [{"$first": "$people.people"}, []]},
                "daily": 1,
            }
        },
        {
            "$project": {
                "rows": {
                    "$map": {
                        "input": {"$range": [0, n_days]},
                        "as": "i",
                        "in": {
                            "$let": {
                                "vars": {
                                    "day": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$i"}},
                                },
                                "in": {
                                    "day": "$$day",
                                    "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$$day"}},
                                    "people": "$people",
                                    "stats": {
                                        "$first": {
                                            "$filter": {
                                                "input": "$daily",
                                                "as": "d",
                                                "cond": {
                                                    "$eq": [
                                                        "$$d._id",
                                                        {"$dateToString": {"format": "%Y-%m-%d", "date": "$$day"}},
                                                    ]
                                                },
                                            }
                                        }
                                    },
                                },
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {
            "$addFields": {
                "present_twice": {"$ifNull": ["$stats.present_twice", 0]},
                "late_count": {"$ifNull": ["$stats.late_count", 0]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$people",
                            "as": "p",
                            "cond": {"$lte": ["$$p.joined_on", "$date"]},
                        }
                    }
                },
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            }
        },
        {"$addFields": {"rate_bps": _rate_bps_expr()}},
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "sum_bps": {
                        "$sum": "$rate_bps",
                        "window": {"documents": [-6, "current"]},
                    },
                    "n_rates": {
                        "$sum": {"$cond": [{"$isNumber": "$rate_bps"}, 1, 0]},
                        "window": {"documents": [-6, "current"]},
                    },
                },
            }
        },
        {"$addFields": {"moving_bps": _moving_bps_expr()}},
        {"$sort": {"day": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_twice": 1,
                "late_count": 1,
                "rate_bps": 1,
                "moving_bps": 1,
            }
        },
    ]


def explain_find(collection: str, filt: dict, sort: SON, skip: int, limit: int, hint: str) -> dict:
    raw = require_db().command(
        {
            "explain": {
                "find": collection,
                "filter": filt,
                "sort": sort,
                "skip": skip,
                "limit": limit,
                "hint": hint,
                "projection": {"_id": 0},
            },
            "verbosity": "executionStats",
        }
    )
    return json_util.loads(json_util.dumps(raw))


def explain_aggregate(collection: str, pipeline: list, hint: str) -> dict:
    raw = require_db().command(
        {
            "explain": {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
                "hint": hint,
            },
            "verbosity": "executionStats",
        }
    )
    return json_util.loads(json_util.dumps(raw))


def _run(collection: str, pipeline: list, hint: str) -> list[dict]:
    return list(require_db()[collection].aggregate(pipeline, hint=hint))


def shape_monthly(row: dict, month: str) -> dict:
    stats = row.get("stats") or {}
    twice = as_int(stats.get("present_twice"))
    working = as_int(row.get("working_days"))
    return {
        "emp_code": row["emp_code"],
        "month": month,
        "working_days": working,
        "present_days": present_days_number(twice),
        "leave_days": as_int(stats.get("leave_days")),
        "late_count": as_int(stats.get("late_count")),
        "total_late_minutes": as_int(stats.get("total_late_minutes")),
        "total_overtime_minutes": as_int(stats.get("total_overtime_minutes")),
        "attendance_pct": attendance_pct(twice, working),
    }


def shape_summary(month: str, rows: list[dict]) -> dict:
    items = []
    for row in rows:
        items.append({
            "department": row["department"],
            "headcount": as_int(row.get("headcount")),
            "present_days": present_days_number(as_int(row.get("present_twice"))),
            "avg_work_hours": avg_work_hours(row.get("wh_sum"), as_int(row.get("wh_n"))),
            "late_count": as_int(row.get("late_count")),
            "total_late_minutes": as_int(row.get("total_late_minutes")),
            "leave_count": as_int(row.get("leave_count")),
            "on_duty_count": as_int(row.get("on_duty_count")),
        })
    return {"month": month, "items": items}


def shape_board(month: str, rows: list[dict]) -> dict:
    return {
        "month": month,
        "items": [
            {
                "rank": as_int(row["rank"]),
                "emp_code": row["emp_code"],
                "name": row["name"],
                "department": row["department"],
                "total_late_minutes": as_int(row["total_late_minutes"]),
                "late_count": as_int(row["late_count"]),
            }
            for row in rows
        ],
    }


def shape_trend(department: str, rows: list[dict]) -> dict:
    items = []
    for row in rows:
        rate, moving = row.get("rate_bps"), row.get("moving_bps")
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": as_int(row.get("headcount")),
            "present_count": present_days_number(as_int(row.get("present_twice"))),
            "late_count": as_int(row.get("late_count")),
            "attendance_rate": None if rate is None else bps_to_rate(as_int(rate)),
            "moving_avg_7d": None if moving is None else bps_to_rate(as_int(moving)),
        })
    return {"department": department, "items": items}


def _require(value: str | None, name: str) -> str:
    if value is None or value == "":
        validation_error(f"{name} is required", ["query", name])
    return value


def _trend_dates(date_from: str | None, date_to: str | None) -> tuple[date, date]:
    start = parse_iso_date(_require(date_from, "from"), ["query", "from"])
    end = parse_iso_date(_require(date_to, "to"), ["query", "to"])
    if end < start:
        validation_error("to must be on or after from", ["query", "to"])
    if (end - start).days + 1 > 92:
        validation_error("date range must not exceed 92 days", ["query", "to"])
    return start, end


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    if client is None or db is None:
        raise HTTPException(status_code=503, detail="database is not configured")
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    database = require_db()
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        database.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists") from None
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    database = require_db()
    filt = {} if department is None else {"department": department}
    hint = IDX_EMP_CODE if department is None else IDX_DEPT_EMP
    skip = (page - 1) * page_size
    total = database.employees.count_documents(filt, hint=hint)
    found = (
        database.employees.find(filt, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
        .hint(hint)
    )
    return {
        "items": [serialize_employee(doc) for doc in found],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    database = require_db()
    emp = database.employees.find_one({"emp_code": body.emp_code}, hint=IDX_EMP_CODE)
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched = millis_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date_for(punched, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(punched, day, emp["shift_start"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        database.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date") from None
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    database = require_db()
    emp = database.employees.find_one({"emp_code": body.emp_code}, hint=IDX_EMP_CODE)
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched = millis_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    doc = database.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched}},
        sort=[("punch_in", -1), ("date", -1)],
        hint=IDX_EMP_PUNCH,
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")
    punch_in = coerce_dt(doc["punch_in"])
    if not span_ok(punch_in, punched):
        validation_error("punched_at must be after punch_in and within 24 hours", ["body", "punched_at"])
    hours, _late, overtime, half = derive_fields(
        doc["status"], punch_in, punched, doc["date"], emp["shift_start"], emp["shift_end"]
    )
    updated = database.attendance_logs.find_one_and_update(
        {"_id": doc["_id"], "punch_out": None},
        {"$set": {"punch_out": punched, "work_hours": hours, "overtime_minutes": overtime, "half_day": half}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    database = require_db()
    filt, sort, skip, limit, hint = attendance_list_spec(emp_code, date_from, date_to, status, page, page_size)
    total = database.attendance_logs.count_documents(filt, hint=hint)
    found = (
        database.attendance_logs.find(filt, {"_id": 0})
        .sort(sort)
        .skip(skip)
        .limit(limit)
        .hint(hint)
    )
    return {
        "items": [serialize_attendance(doc) for doc in found],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _logical(doc: dict, field: str) -> Any:
    if field == "punch_in" or field == "punch_out":
        return coerce_dt(doc.get(field)) if isinstance(doc.get(field), datetime) else None
    if field == "work_hours":
        return doc["work_hours"] if "work_hours" in doc else None
    if field == "half_day":
        return bool(doc["half_day"]) if "half_day" in doc else False
    if field in ("late_minutes", "overtime_minutes"):
        return int(doc[field]) if field in doc and doc[field] is not None else 0
    return doc.get(field)


def _different(field: str, old: Any, new: Any) -> bool:
    if field in ("punch_in", "punch_out"):
        old_ms = dt_to_millis(old) if isinstance(old, datetime) else old
        new_ms = dt_to_millis(new) if isinstance(new, datetime) else new
        return old_ms != new_ms
    if field == "work_hours":
        return hours_cents(old) != hours_cents(new)
    if field == "half_day":
        return bool(old) != bool(new)
    if field in ("late_minutes", "overtime_minutes"):
        return int(old or 0) != int(new or 0)
    return old != new


def _cas_filter(doc: dict) -> dict:
    filt: dict[str, Any] = {"_id": doc["_id"]}
    for field in ("status", "punch_in", "punch_out", "history"):
        if field not in doc:
            filt[field] = {"$exists": False}
        else:
            filt[field] = doc[field]
    return filt


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    database = require_db()
    day = date
    parse_iso_date(day, ["path", "date"])
    emp = database.employees.find_one({"emp_code": emp_code}, hint=IDX_EMP_CODE)
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    doc = database.attendance_logs.find_one({"emp_code": emp_code, "date": day}, hint=IDX_EMP_DATE)
    if doc is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    provided = body.model_fields_set
    if "punch_in" in provided and body.punch_in is None:
        validation_error("punch_in cannot be null", ["body", "punch_in"])
    if "punch_out" in provided and body.punch_out is None:
        validation_error("punch_out cannot be null", ["body", "punch_out"])
    if body.status in ABSENCE and ({"punch_in", "punch_out"} & provided):
        validation_error("punch times cannot be supplied with ABSENT or LEAVE", ["body", "status"])

    new_status = body.status if "status" in provided else doc["status"]
    if new_status in ABSENCE:
        new_in = None
        new_out = None
    else:
        new_in = millis_to_dt(body.punch_in) if "punch_in" in provided else _logical(doc, "punch_in")
        new_out = millis_to_dt(body.punch_out) if "punch_out" in provided else _logical(doc, "punch_out")
        if new_in is None:
            validation_error("a presence status requires punch_in", ["body", "punch_in"])
        if attendance_date_for(new_in, emp["shift_start"], emp["shift_end"]) != day:
            validation_error("punch_in must stay on this attendance date", ["body", "punch_in"])
        if new_out is not None and not span_ok(new_in, new_out):
            validation_error("punch_out must be after punch_in and within 24 hours", ["body", "punch_out"])

    hours, late, overtime, half = derive_fields(
        new_status, new_in, new_out, day, emp["shift_start"], emp["shift_end"]
    )
    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half,
    }
    changes = {}
    for field in CHANGE_FIELDS:
        old = _logical(doc, field)
        new = new_values[field]
        if _different(field, old, new):
            changes[field] = {"from": old, "to": new}
    if not changes:
        validation_error("request changes nothing", ["body"])

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    new_history = (doc.get("history") or []) + [entry]
    updated = database.attendance_logs.find_one_and_update(
        _cas_filter(doc),
        {"$set": {**new_values, "history": new_history}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="concurrent modification")
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    rows = _run("employees", employee_monthly_pipeline(emp_code, month), IDX_EMP_CODE)
    if not rows:
        raise HTTPException(status_code=404, detail="employee not found")
    return shape_monthly(rows[0], month)


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline, hint = department_summary_pipeline(month, department)
    return shape_summary(month, _run("employees", pipeline, hint))


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    rows = _run("attendance_logs", leaderboard_pipeline(month, limit, department), IDX_DATE_EMP)
    return shape_board(month, rows)


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(alias="from"),
    date_to: str = Query(alias="to"),
):
    database = require_db()
    start, end = _trend_dates(date_from, date_to)
    if database.employees.count_documents({"department": department}, hint=IDX_DEPT_JOINED, limit=1) == 0:
        raise HTTPException(status_code=404, detail="department not found")
    rows = _run("employees", department_trend_pipeline(department, start, end), IDX_DEPT_JOINED)
    return shape_trend(department, rows)


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    date_from_alias: Optional[str] = Query(None, alias="from"),
    date_to_alias: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    require_db()
    if endpoint == "attendance_list":
        filt, sort, skip, page_limit, hint = attendance_list_spec(
            emp_code, date_from, date_to, status, page, page_size
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_find("attendance_logs", filt, sort, skip, page_limit, hint),
        }
    if endpoint == "employee_monthly":
        code = _require(emp_code, "emp_code")
        period = _require(month, "month")
        pipeline = employee_monthly_pipeline(code, period)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_aggregate("employees", pipeline, IDX_EMP_CODE),
        }
    if endpoint == "department_summary":
        period = _require(month, "month")
        pipeline, hint = department_summary_pipeline(period, department)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_aggregate("employees", pipeline, hint),
        }
    if endpoint == "late_leaderboard":
        period = _require(month, "month")
        pipeline = leaderboard_pipeline(period, limit, department)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_aggregate("attendance_logs", pipeline, IDX_DATE_EMP),
        }
    dept = _require(department, "department")
    start, end = _trend_dates(date_from_alias, date_to_alias)
    pipeline = department_trend_pipeline(dept, start, end)
    return {
        "endpoint": endpoint,
        "collection": "employees",
        "explain": explain_aggregate("employees", pipeline, IDX_DEPT_JOINED),
    }
