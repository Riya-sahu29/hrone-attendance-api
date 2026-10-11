"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)
"""
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override real environment variables

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,                    # datetimes come back timezone-aware (UTC)
    serverSelectionTimeoutMS=5000,    # fail fast if Mongo is down
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
GRACE_SECONDS = 600

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# Strict integer epoch milliseconds: floats, strings and seconds-looking values -> 422
EpochMs = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]


def ensure_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Datetime from Mongo -> epoch ms (int). None stays None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def hhmm_to_minutes(s: str) -> int:
    h, m = map(int, s.split(":"))
    return h * 60 + m


def resolve_shift(punch_in: datetime, shift_start: str, shift_end: str):
    """Return (attendance_date_str, shift_start_dt, shift_end_dt) per R1."""
    ist = punch_in.astimezone(IST)
    s, e = hhmm_to_minutes(shift_start), hhmm_to_minutes(shift_end)
    overnight = e <= s
    day = ist.date()
    seconds_of_day = ist.hour * 3600 + ist.minute * 60 + ist.second
    if overnight and seconds_of_day < e * 60:
        day -= timedelta(days=1)
    start_dt = datetime.combine(day, time(s // 60, s % 60), tzinfo=IST)
    end_day = day + timedelta(days=1) if overnight else day
    end_dt = datetime.combine(end_day, time(e // 60, e % 60), tzinfo=IST)
    return day.isoformat(), start_dt, end_dt


def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    secs = int((punch_in - shift_start_dt).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    mins = int((punch_out - shift_end_dt).total_seconds()) // 60
    return mins if mins >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    secs = int((punch_out - punch_in).total_seconds())
    hours = Decimal(secs) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: float) -> bool:
    return work_hours < 4.5


def derived_fields(status: str, pin, pout, emp: dict) -> dict:
    """Recompute late/work_hours/overtime/half_day from the final state (R2-R5)."""
    if status in ("ABSENT", "LEAVE"):
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    _, start_dt, end_dt = resolve_shift(pin, emp["shift_start"], emp["shift_end"])
    out = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(pin, start_dt),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if pout is not None:
        wh = compute_work_hours(pin, pout)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(pout, end_dt)
        out["half_day"] = compute_half_day(wh)
    return out


def valid_date_str(v: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v):
        raise ValueError("must be YYYY-MM-DD")
    try:
        date_cls.fromisoformat(v)
    except ValueError:
        raise ValueError("not a real calendar date")
    return v


def date_param(name: str, v: Optional[str]) -> Optional[str]:
    """Validate a YYYY-MM-DD query parameter; 422 if bad."""
    if v is None:
        return None
    try:
        return valid_date_str(v)
    except ValueError as exc:
        raise HTTPException(422, f"{name}: {exc}")


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, v):
        return valid_date_str(v)

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def history_out(h: dict) -> dict:
    changes = {}
    for key, val in (h.get("changes") or {}).items():
        if key in ("punch_in", "punch_out"):
            val = {"from": dt_to_ms(val.get("from")), "to": dt_to_ms(val.get("to"))}
        changes[key] = val
    return {
        "at": dt_to_ms(h.get("at")),
        "by": h.get("by"),
        "reason": h.get("reason"),
        "changes": changes,
    }


def attendance_out(doc: dict) -> dict:
    """Mongo document -> API record. No _id; instants as epoch ms; missing fields defaulted."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cur = (
        db.employees.find(q)
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_out(d) for d in cur],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day, shift_start_dt, _ = resolve_shift(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, shift_start_dt),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index decides the race
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # most recent record whose punch_in is at or before punched_at (works across midnight)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    pin = rec["punch_in"]
    if ts <= pin:
        raise HTTPException(422, "punched_at must be after punch_in")
    if ts - pin > timedelta(hours=24):
        raise HTTPException(422, "punch_out is more than 24 hours after punch_in")

    _, _, shift_end_dt = resolve_shift(pin, emp["shift_start"], emp["shift_end"])
    work_hours = compute_work_hours(pin, ts)
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": rec["date"], "punch_out": None},  # only if still open
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": compute_overtime(ts, shift_end_dt),
            "half_day": compute_half_day(work_hours),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:  # someone else punched out between our read and write
        raise HTTPException(409, "already punched out")
    return attendance_out(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    date = date_param("date", date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    new_status = body.status or rec["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            raise HTTPException(422, "a presence status requires punch_in")
        day, _, _ = resolve_shift(new_in, emp["shift_start"], emp["shift_end"])
        if day != rec["date"]:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derived_fields(new_status, new_in, new_out, emp)}
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    history = rec.get("history") or []
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        {   # apply only if the record is still exactly as we read it
            "emp_code": emp_code,
            "date": date,
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            f"history.{len(history)}": {"$exists": False},
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return attendance_out(updated)


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    """Validate the filters and build the Mongo filter (shared by the list endpoint and explain)."""
    date_from = date_param("date_from", date_from)
    date_to = date_param("date_to", date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_out(d) for d in cur],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


def month_bounds(month: Optional[str]):
    """'2026-07' -> (first_day, last_day) as dates. 422 if invalid."""
    if not month or not MONTH_RE.fullmatch(month):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = int(month[:4]), int(month[5:7])
    try:
        first = date_cls(y, m, 1)
        nxt = date_cls(y + 1, 1, 1) if m == 12 else date_cls(y, m + 1, 1)
    except ValueError:
        raise HTTPException(422, "invalid month")
    return first, nxt - timedelta(days=1)


def count_weekdays(a: date_cls, b: date_cls) -> int:
    """Number of Mon-Fri days between a and b inclusive (R7)."""
    if b < a:
        return 0
    return sum(1 for i in range((b - a).days + 1) if (a + timedelta(days=i)).weekday() < 5)


def half_up(x, places: int) -> float:
    """Round half-up (R8) using Decimal, never float rounding."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP))


# --- small building blocks for Mongo expressions. v is the field prefix: "$" or "$$l." ---
def mx_present(v):
    return {"$in": [f"{v}status", PRESENCE]}


def mx_weekday(v):
    # $dayOfWeek: 1 = Sunday ... 7 = Saturday, so Mon-Fri = 2..6
    return {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": f"{v}date"}}}, [2, 3, 4, 5, 6]]}


def mx_weight(v):
    """1 for a present record, 0.5 if it is a half day, 0 if not present."""
    return {"$cond": [mx_present(v), {"$cond": [{"$ifNull": [f"{v}half_day", False]}, 0.5, 1]}, 0]}


def mx_weekday_weight(v):
    return {"$cond": [mx_weekday(v), mx_weight(v), 0]}


def mx_late(v):
    return {"$ifNull": [f"{v}late_minutes", 0]}


def mx_flag(cond):
    return {"$cond": [cond, 1, 0]}


def mx_half_up(x, places):
    """Half-up rounding inside Mongo (its own $round is banker's rounding)."""
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [x, scale]}, 0.5000000001]}}, scale]}


# --------------------------------------------------------------------------- #
# Analytics 1: one employee, one month
# --------------------------------------------------------------------------- #
def monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": mx_weekday_weight("$")},
            "leave_days": {"$sum": mx_flag({"$eq": ["$status", "LEAVE"]})},
            "late_count": {"$sum": mx_flag({"$gt": [mx_late("$"), 0]})},
            "total_late_minutes": {"$sum": mx_late("$")},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    first, last = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    start = max(first, date_cls.fromisoformat(emp["joined_on"]))  # R7: count from joined_on
    working_days = count_weekdays(start, last)
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first.isoformat(), last.isoformat())))
    a = rows[0] if rows else {}
    present = a.get("present_days", 0)
    pct = None
    if working_days > 0:
        pct = half_up(Decimal(str(present)) * 100 / Decimal(working_days), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": half_up(present, 2),
        "leave_days": int(a.get("leave_days", 0)),
        "late_count": int(a.get("late_count", 0)),
        "total_late_minutes": int(a.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(a.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


# --------------------------------------------------------------------------- #
# Analytics 2: department summary
# --------------------------------------------------------------------------- #
def dept_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    """Start from EMPLOYEES so people with zero logs still count (R9)."""
    emp_match = {"joined_on": {"$lte": end}}
    if department:
        emp_match["department"] = department
    L = "$$l."

    def over_logs(expr):
        return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expr}}}

    has_hours = {"$and": [mx_present(L), {"$isNumber": f"{L}work_hours"}]}
    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}}],
            "as": "logs",
        }},
        {"$set": {   # one set of numbers per employee
            "present_days": over_logs(mx_weekday_weight(L)),
            "late_count": over_logs(mx_flag({"$gt": [mx_late(L), 0]})),
            "late_total": over_logs(mx_late(L)),
            "leave_count": over_logs(mx_flag({"$eq": [f"{L}status", "LEAVE"]})),
            "on_duty_count": over_logs(mx_flag({"$eq": [f"{L}status", "ON_DUTY"]})),
            "wh_cnt": over_logs(mx_flag(has_hours)),
            # work_hours in hundredths, so the sum is an exact integer
            "wh_centi": over_logs({"$cond": [has_hours,
                {"$toLong": {"$round": [{"$multiply": [f"{L}work_hours", 100]}, 0]}}, 0]}),
        }},
        {"$group": {   # then add the employees up per department
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "late_total": {"$sum": "$late_total"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "wh_cnt": {"$sum": "$wh_cnt"},
            "wh_centi": {"$sum": "$wh_centi"},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    first, last = month_bounds(month)
    rows = db.employees.aggregate(dept_summary_pipeline(first.isoformat(), last.isoformat(), department))
    items = []
    for r in rows:
        avg = None
        if r["wh_cnt"] > 0:  # average over RECORDS, not an average of averages
            avg = half_up(Decimal(r["wh_centi"]) / Decimal(r["wh_cnt"]) / 100, 2)
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": half_up(r["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["late_total"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics 3: late leaderboard
# --------------------------------------------------------------------------- #
def leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    p = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},   # logs of unknown employees disappear here
    ]
    if department:
        p.append({"$match": {"emp.department": department}})   # rank inside the department only
    p += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},   # limit applies AFTER ranking, so ties stay together
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return p


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    items = list(db.attendance_logs.aggregate(
        leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department)))
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics 4: department daily trend
# --------------------------------------------------------------------------- #
def build_trend(department: str, from_s: Optional[str], to_s: Optional[str]) -> list:
    frm = date_param("from", from_s)
    to = date_param("to", to_s)
    d_from, d_to = date_cls.fromisoformat(frm), date_cls.fromisoformat(to)
    if d_to < d_from:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    codes = [e["emp_code"] for e in emps]
    joined = [e["joined_on"] for e in emps]
    lower = datetime.combine(d_from, time(0), tzinfo=timezone.utc)
    upper = datetime.combine(d_to + timedelta(days=1), time(0), tzinfo=timezone.utc)  # upper bound is exclusive
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": frm, "$lte": to}}},
        {"$group": {"_id": "$date",
                    "present_count": {"$sum": mx_weight("$")},   # weekends count too
                    "late_count": {"$sum": mx_flag({"$gt": [mx_late("$"), 0]})}}},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id"}}}},
        # gap filling: one row for every calendar day, even days with no records
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {"$set": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
            "is_working_day": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$set": {   # headcount = employees who had joined by that day (R9)
            "headcount": {"$size": {"$filter": {"input": joined, "as": "j",
                                                "cond": {"$lte": ["$$j", "$date"]}}}},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            mx_half_up({"$divide": ["$present_count", "$headcount"]}, 4),
            None,
        ]}}},
        {"$setWindowFields": {   # current row + up to 6 rows before it, inside the range; $avg skips nulls
            "sortBy": {"d": 1},
            "output": {"ma": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$set": {"moving_avg_7d": {"$cond": [{"$isNumber": "$ma"}, mx_half_up("$ma", 4), None]}}},
        {"$sort": {"d": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    pipeline = build_trend(department, from_, to)
    return {"department": department, "items": list(db.attendance_logs.aggregate(pipeline))}


# --------------------------------------------------------------------------- #
# Admin: explain. Builds each query EXACTLY like the endpoint does, then asks Mongo to explain it.
# --------------------------------------------------------------------------- #
def need(**params):
    missing = [k for k, v in params.items() if v in (None, "")]
    if missing:
        raise HTTPException(422, f"missing parameters: {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        coll = "attendance_logs"
        cmd = {"find": coll,
               "filter": attendance_filter(emp_code, date_from, date_to, status),
               "sort": {"date": -1, "emp_code": 1},
               "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint == "employee_monthly":
            need(emp_code=emp_code, month=month)
            first, last = month_bounds(month)
            coll, pipeline = "attendance_logs", monthly_pipeline(emp_code, first.isoformat(), last.isoformat())
        elif endpoint == "department_summary":
            need(month=month)
            first, last = month_bounds(month)
            coll, pipeline = "employees", dept_summary_pipeline(first.isoformat(), last.isoformat(), department)
        elif endpoint == "late_leaderboard":
            need(month=month)
            first, last = month_bounds(month)
            coll, pipeline = "attendance_logs", leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department)
        else:  # department_trend
            need(department=department, **{"from": from_, "to": to})
            coll, pipeline = "attendance_logs", build_trend(department, from_, to)
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    plain = json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": coll, "explain": plain}