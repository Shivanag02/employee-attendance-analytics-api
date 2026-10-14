"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=2000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

def create_indexes() -> None:
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uniq_employees_emp_code",
    )
    db.employees.create_index(
        [("department", 1), ("emp_code", 1)],
        name="employees_department_emp_code",
    )
    db.employees.create_index(
        [("joined_on", 1), ("department", 1)],
        name="employees_joined_department",
    )
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="employees_department_joined",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uniq_attendance_emp_code_date",
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="attendance_date_emp_code",
    )
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1), ("emp_code", 1)],
        name="attendance_status_date_emp_code",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)],
        name="attendance_emp_punch_in",
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        create_indexes()
    except PyMongoError:
        logger.exception("MongoDB index initialization failed")
        raise
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def truncate_to_seconds(value: datetime) -> datetime:
    # A naive datetime from older MongoDB data is interpreted as UTC, never local time.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)

def epoch_ms_to_datetime(value: int) -> datetime:
    return truncate_to_seconds(UTC_EPOCH + timedelta(milliseconds=value))

def datetime_to_epoch_ms(value: datetime) -> int:
    delta = truncate_to_seconds(value) - UTC_EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000

def whole_minutes(delta: timedelta) -> int:
    return delta.days * 1440 + delta.seconds // 60

def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: str,
) -> int:
    punch_in_utc = truncate_to_seconds(punch_in)
    start_time = time.fromisoformat(shift_start)
    start_ist = datetime.combine(
        date.fromisoformat(attendance_date),
        start_time,
        tzinfo=IST,
    )
    elapsed = punch_in_utc - start_ist.astimezone(timezone.utc)

    if elapsed <= timedelta(minutes=10):
        return 0
    return whole_minutes(elapsed)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    elapsed = truncate_to_seconds(punch_out) - truncate_to_seconds(punch_in)
    seconds = elapsed.days * 86400 + elapsed.seconds
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    shift_date = date.fromisoformat(attendance_date)
    if shift_end <= shift_start:
        shift_date += timedelta(days=1)

    end_ist = datetime.combine(
        shift_date,
        time.fromisoformat(shift_end),
        tzinfo=IST,
    )
    elapsed = truncate_to_seconds(punch_out) - end_ist.astimezone(timezone.utc)
    overtime_minutes = whole_minutes(elapsed)

    return overtime_minutes if overtime_minutes >= 30 else 0

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date") from exc
        return value

    @model_validator(mode="after")
    def validate_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(
        default=None,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )
    punch_out: Optional[StrictInt] = Field(
        default=None,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]


def month_bounds(month: str) -> tuple[str, str]:
    try:
        month_start = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM") from exc
    if len(month) != 7 or month_start.strftime("%Y-%m") != month:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    month_end = date(
        month_start.year,
        month_start.month,
        calendar.monthrange(month_start.year, month_start.month)[1],
    )
    return month_start.isoformat(), month_end.isoformat()


def response_record(record: dict) -> dict:
    record = {key: value for key, value in record.items() if key != "_id"}
    record.setdefault("punch_in", None)
    record.setdefault("punch_out", None)
    record.setdefault("work_hours", None)
    record.setdefault("late_minutes", 0)
    record.setdefault("overtime_minutes", 0)
    record.setdefault("half_day", False)
    record.setdefault("history", [])
    for field in ("punch_in", "punch_out"):
        if isinstance(record[field], datetime):
            record[field] = datetime_to_epoch_ms(record[field])
    for entry in record["history"]:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = datetime_to_epoch_ms(entry["at"])
        changes = entry.get("changes", {})
        for field in ("punch_in", "punch_out"):
            if field in changes:
                for direction in ("from", "to"):
                    value = changes[field].get(direction)
                    if isinstance(value, datetime):
                        changes[field][direction] = datetime_to_epoch_ms(value)
    return record


def attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    status: Optional[str],
) -> dict:
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to is not None:
            query["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        query["status"] = status
    return query


def month_log_lookup(
    start_date: str,
    end_date: str,
    *,
    emp_code_expr: str,
    joined_on_expr: Optional[str] = None,
) -> dict:
    conditions = [
        {"$eq": ["$emp_code", emp_code_expr]},
        {"$gte": ["$date", start_date]},
        {"$lte": ["$date", end_date]},
    ]
    if joined_on_expr is not None:
        conditions.append({"$gte": ["$date", joined_on_expr]})
    return {
        "$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": emp_code_expr, "joined_on": joined_on_expr},
            "pipeline": [
                {
                    "$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$emp_code", "$$employee_code"]},
                                {"$gte": ["$date", start_date]},
                                {"$lte": ["$date", end_date]},
                                *(
                                    [{"$gte": ["$date", "$$joined_on"]}]
                                    if joined_on_expr is not None
                                    else []
                                ),
                            ]
                        }
                    }
                }
            ],
            "as": "monthly_logs",
        }
    }


def employee_monthly_pipeline(emp_code: str, start_date: str, end_date: str) -> list:
    start_dt = datetime.combine(date.fromisoformat(start_date), time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(date.fromisoformat(end_date), time.min, tzinfo=timezone.utc)
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$addFields": {
                "_period_start": {
                    "$max": [
                        start_dt,
                        {"$dateFromString": {"dateString": "$joined_on"}},
                    ]
                }
            }
        },
        {
            "$addFields": {
                "_working_days": {
                    "$size": {
                        "$filter": {
                            "input": {
                                "$map": {
                                    "input": {
                                        "$range": [
                                            0,
                                            {
                                                "$add": [
                                                    {
                                                        "$dateDiff": {
                                                            "startDate": "$_period_start",
                                                            "endDate": end_dt,
                                                            "unit": "day",
                                                        }
                                                    },
                                                    1,
                                                ]
                                            },
                                        ]
                                    },
                                    "as": "offset",
                                    "in": {
                                        "$dateAdd": {
                                            "startDate": "$_period_start",
                                            "unit": "day",
                                            "amount": "$$offset",
                                        }
                                    },
                                }
                            },
                            "as": "calendar_day",
                            "cond": {
                                "$and": [
                                    {"$gte": ["$$calendar_day", "$_period_start"]},
                                    {"$lte": ["$$calendar_day", end_dt]},
                                    {
                                        "$in": [
                                            {"$dayOfWeek": "$$calendar_day"},
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                        }
                    }
                }
            }
        },
        month_log_lookup(
            start_date,
            end_date,
            emp_code_expr="$emp_code",
            joined_on_expr="$joined_on",
        ),
        {
            "$unwind": {
                "path": "$monthly_logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$addFields": {
                "_log_weekday": {
                    "$cond": [
                        {"$ne": ["$monthly_logs", None]},
                        {
                            "$in": [
                                {
                                    "$dayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$monthly_logs.date"
                                        }
                                    }
                                },
                                [2, 3, 4, 5, 6],
                            ]
                        },
                        False,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": {
                    "emp_code": "$emp_code",
                    "month": start_date[:7],
                    "working_days": "$_working_days",
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_log_weekday",
                                    {"$in": ["$monthly_logs.status", PRESENCE_STATUSES]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$ifNull": ["$monthly_logs.half_day", False]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$monthly_logs.status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$monthly_logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$monthly_logs.late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$monthly_logs.overtime_minutes", 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id.emp_code",
                "month": "$_id.month",
                "working_days": "$_id.working_days",
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$_id.working_days", 0]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            "$present_days",
                                                            "$_id.working_days",
                                                        ]
                                                    },
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                100,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
    ]


def department_summary_pipeline(start_date: str, end_date: str, department: Optional[str]) -> list:
    employee_match = {"joined_on": {"$lte": end_date}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        month_log_lookup(
            start_date,
            end_date,
            emp_code_expr="$emp_code",
            joined_on_expr="$joined_on",
        ),
        {
            "$unwind": {
                "path": "$monthly_logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$addFields": {
                "_log_weekday": {
                    "$cond": [
                        {"$ne": ["$monthly_logs", None]},
                        {
                            "$in": [
                                {
                                    "$dayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$monthly_logs.date"
                                        }
                                    }
                                },
                                [2, 3, 4, 5, 6],
                            ]
                        },
                        False,
                    ]
                },
                "_present": {
                    "$in": ["$monthly_logs.status", PRESENCE_STATUSES]
                },
            }
        },
        {
            "$group": {
                "_id": {
                    "emp_code": "$emp_code",
                    "department": "$department",
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$_log_weekday", "$_present"]},
                            {
                                "$cond": [
                                    {"$ifNull": ["$monthly_logs.half_day", False]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_present",
                                    {"$ne": ["$monthly_logs.work_hours", None]},
                                ]
                            },
                            "$monthly_logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_present",
                                    {"$ne": ["$monthly_logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$monthly_logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$monthly_logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$monthly_logs.status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$monthly_logs.status", "ON_DUTY"]}, 1, 0]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            "$work_hours_sum",
                                                            "$work_hours_count",
                                                        ]
                                                    },
                                                    100,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                100,
                            ]
                        },
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(start_date: str, end_date: str, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": start_date, "$lte": end_date}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )
    return pipeline


def department_trend_pipeline(department: str, start_date: date, end_date: date) -> list:
    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    day_count = (end_date - start_date).days + 1
    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": "$department",
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
            }
        },
        {
            "$project": {
                "employees": 1,
                "calendar_days": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "day_offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": start_dt,
                                "unit": "day",
                                "amount": "$$day_offset",
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$calendar_days"},
        {
            "$addFields": {
                "date_string": {
                    "$dateToString": {
                        "date": "$calendar_days",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                },
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$calendar_days"}, [2, 3, 4, 5, 6]]
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "day": "$date_string",
                    "codes": {
                        "$map": {
                            "input": "$employees",
                            "as": "employee",
                            "in": "$$employee.emp_code",
                        }
                    },
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                        }
                    },
                ],
                "as": "daily_logs",
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "employee",
                            "cond": {"$lte": ["$$employee.joined_on", "$date_string"]},
                        }
                    }
                },
                "present_count": {
                    "$reduce": {
                        "input": "$daily_logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {"$in": ["$$this.status", PRESENCE_STATUSES]},
                                        {
                                            "$cond": [
                                                {"$ifNull": ["$$this.half_day", False]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$daily_logs",
                            "as": "log",
                            "cond": {
                                "$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]
                            },
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            "$present_count",
                                                            "$headcount",
                                                        ]
                                                    },
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"calendar_days": 1},
                "output": {
                    "_moving_avg": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$_moving_avg", None]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {"$multiply": ["$_moving_avg", 10000]},
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable",
        ) from exc

    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    created_at = truncate_to_seconds(datetime.now(timezone.utc))
    doc = body.model_dump()
    doc["created_at"] = created_at

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        ) from exc

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(created_at),
    }


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(query)
    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    for item in items:
        if item.get("created_at") is not None:
            item["created_at"] = datetime_to_epoch_ms(item["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    ts = (
        epoch_ms_to_datetime(body.punched_at)
        if body.punched_at is not None
        else truncate_to_seconds(datetime.now(timezone.utc))
    )
    local_ts = ts.astimezone(IST)

    attendance_day = local_ts.date()
    if (
        emp["shift_end"] <= emp["shift_start"]
        and local_ts.strftime("%H:%M") < emp["shift_end"]
    ):
        attendance_day -= timedelta(days=1)

    d = attendance_day.isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        ) from exc

    return response_record(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    query = attendance_filter(emp_code, date_from, date_to, status)

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(query)
    page_docs = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    page_docs = [response_record(doc) for doc in page_docs]

    return {
        "items": page_docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = (
        epoch_ms_to_datetime(body.punched_at)
        if body.punched_at is not None
        else truncate_to_seconds(datetime.now(timezone.utc))
    )
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punched_at},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in record found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record already punched out")

    punch_in_at = truncate_to_seconds(record["punch_in"])
    elapsed = punched_at - punch_in_at
    if elapsed <= timedelta(0) or elapsed > timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="punch_out must be after punch_in and within 24 hours",
        )

    work_hours = compute_work_hours(punch_in_at, punched_at)
    update = {
        "$set": {
            "punch_out": punched_at,
            "work_hours": work_hours,
            "overtime_minutes": compute_overtime(
                punched_at,
                employee["shift_start"],
                employee["shift_end"],
                record["date"],
            ),
            "half_day": work_hours < 4.50,
        }
    }
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        update,
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record already punched out")
    return response_record(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: date,
    body: RegularizeIn,
):
    date_string = date.isoformat()
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date_string}
    )
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    supplied = body.model_fields_set
    if "status" in supplied and body.status is None:
        raise HTTPException(status_code=422, detail="status cannot be null")
    status = body.status if "status" in supplied else record["status"]
    punch_in_at = record.get("punch_in")
    punch_out_at = record.get("punch_out")

    if status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                status_code=422,
                detail="punch times cannot be supplied for ABSENT or LEAVE",
            )
        punch_in_at = None
        punch_out_at = None
    else:
        if "punch_in" in supplied:
            if body.punch_in is None:
                raise HTTPException(status_code=422, detail="punch_in cannot be null")
            punch_in_at = epoch_ms_to_datetime(body.punch_in)
        if "punch_out" in supplied:
            if body.punch_out is None:
                raise HTTPException(status_code=422, detail="punch_out cannot be null")
            punch_out_at = epoch_ms_to_datetime(body.punch_out)

        if punch_in_at is None:
            raise HTTPException(
                status_code=422,
                detail="presence status requires punch_in",
            )

        local_punch_in = truncate_to_seconds(punch_in_at).astimezone(IST)
        expected_day = local_punch_in.date()
        if (
            employee["shift_end"] <= employee["shift_start"]
            and local_punch_in.strftime("%H:%M") < employee["shift_end"]
        ):
            expected_day -= timedelta(days=1)
        if expected_day.isoformat() != date_string:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the attendance date",
            )

        punch_in_at = truncate_to_seconds(punch_in_at)
        if punch_out_at is not None:
            punch_out_at = truncate_to_seconds(punch_out_at)
            elapsed = punch_out_at - punch_in_at
            if elapsed <= timedelta(0) or elapsed > timedelta(hours=24):
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )

    if status in ("ABSENT", "LEAVE"):
        derived = {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    else:
        work_hours = (
            compute_work_hours(punch_in_at, punch_out_at)
            if punch_out_at is not None
            else None
        )
        derived = {
            "late_minutes": compute_late_minutes(
                punch_in_at,
                employee["shift_start"],
                date_string,
            ),
            "work_hours": work_hours,
            "overtime_minutes": (
                compute_overtime(
                    punch_out_at,
                    employee["shift_start"],
                    employee["shift_end"],
                    date_string,
                )
                if punch_out_at is not None
                else 0
            ),
            "half_day": work_hours < 4.50 if work_hours is not None else False,
        }

    final_values = {
        "status": status,
        "punch_in": punch_in_at,
        "punch_out": punch_out_at,
        **derived,
    }
    old_values = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    changes = {}
    for field, new_value in final_values.items():
        old_value = old_values[field]
        if field in ("punch_in", "punch_out"):
            if old_value is not None:
                old_value = truncate_to_seconds(old_value)
            if new_value is not None:
                new_value = truncate_to_seconds(new_value)
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(status_code=422, detail="regularization changes nothing")

    history_entry = {
        "at": truncate_to_seconds(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    old_history = record.get("history", [])
    atomic_filter = {
        "_id": record["_id"],
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }
    if "history" in record:
        atomic_filter["history"] = old_history
    else:
        atomic_filter["history"] = {"$exists": False}

    updated = db.attendance_logs.find_one_and_update(
        atomic_filter,
        {"$set": final_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="attendance record changed concurrently",
        )
    return response_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    start_date, end_date = month_bounds(month)
    if db.employees.find_one({"emp_code": emp_code}) is None:
        raise HTTPException(status_code=404, detail="employee not found")
    pipeline = employee_monthly_pipeline(emp_code, start_date, end_date)
    result = list(db.employees.aggregate(pipeline))
    if not result:
        raise HTTPException(status_code=404, detail="employee not found")
    return result[0]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    start_date, end_date = month_bounds(month)
    items = list(
        db.employees.aggregate(
            department_summary_pipeline(start_date, end_date, department)
        )
    )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    start_date, end_date = month_bounds(month)
    pipeline = leaderboard_pipeline(start_date, end_date, department)
    pipeline.append({"$match": {"rank": {"$lte": limit}}})
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: date = Query(alias="from"),
    date_to: date = Query(alias="to"),
):
    if date_to < date_from:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (date_to - date_from).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="department not found")
    pipeline = department_trend_pipeline(department, date_from, date_to)
    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}


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
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    trend_from: Optional[date] = Query(default=None, alias="from"),
    trend_to: Optional[date] = Query(default=None, alias="to"),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not exceed date_to")
        query = attendance_filter(emp_code, date_from, date_to, status)
        command = {
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        collection_name = "attendance_logs"
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        start_date, end_date = month_bounds(month)
        command = {
            "explain": {
                "aggregate": "employees",
                "pipeline": employee_monthly_pipeline(emp_code, start_date, end_date),
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        collection_name = "employees"
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        start_date, end_date = month_bounds(month)
        command = {
            "explain": {
                "aggregate": "employees",
                "pipeline": department_summary_pipeline(start_date, end_date, department),
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        collection_name = "employees"
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        start_date, end_date = month_bounds(month)
        pipeline = leaderboard_pipeline(start_date, end_date, department)
        pipeline.append({"$match": {"rank": {"$lte": limit}}})
        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        collection_name = "attendance_logs"
    else:
        if department is None or trend_from is None or trend_to is None:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required",
            )
        if trend_to < trend_from or (trend_to - trend_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="invalid trend date range")
        pipeline = department_trend_pipeline(department, trend_from, trend_to)
        command = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        collection_name = "employees"

    explain = db.command(command)
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": json.loads(json_util.dumps(explain)),
    }
