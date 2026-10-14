# Employee Attendance & Analytics API

A FastAPI service for employee records, attendance punches and corrections, and MongoDB-backed attendance analytics. Calendar dates and shift rules use IST (UTC+05:30); API instants are epoch milliseconds and MongoDB stores instants as BSON dates.

## Runtime

- Python 3.11 or newer (verified with Python 3.11.7).
- MongoDB 6.0 or newer (verified with MongoDB 7.0.45).

## Install and configure

From the project directory on Windows:

```cmd
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`dnspython` is listed explicitly to support MongoDB Atlas `mongodb+srv://` connection URIs without relying on a PyMongo extra that current releases no longer provide.

Start MongoDB locally, or use a MongoDB Atlas connection URI. Configure `MONGO_URI` and `MONGO_DB` in the environment before starting the API. For Command Prompt:

```cmd
set MONGO_URI=mongodb://localhost:27017
set MONGO_DB=attendance_db
```

The app also loads values from a local `.env` file. Create one if preferred; do not commit it or put credentials in source control. Required indexes are created idempotently during application startup. A startup/database/index failure is logged and prevents the app from starting successfully.

Optional sample data is available in `sample_data/`; `sample_seed.py` loads it into the configured database. Use a disposable database if you want to seed or exercise write endpoints.

## Start the server

```cmd
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000/docs` for the interactive API documentation, or `http://127.0.0.1:8000/health` to check database readiness.

## Endpoints

- `GET /health`
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

The analytics endpoints use MongoDB aggregation pipelines. The explain endpoint accepts `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, or `department_trend` and returns MongoDB execution statistics for the corresponding query.

## Testing

There is no checked-in pytest/unittest suite or dedicated test command yet. A basic smoke check after starting the server is:

```cmd
curl.exe -i http://127.0.0.1:8000/health
curl.exe -i http://127.0.0.1:8000/docs
curl.exe -i "http://127.0.0.1:8000/employees?page=1&page_size=20"
```

Use `/docs` or `curl.exe` to exercise the remaining routes. Check validation and conflict status codes, concurrent punch operations, overnight shifts, mid-month joiners, weekend records, leaderboard ties, trend gaps, and explain plans. Write-endpoint tests create or change data, so run them against a disposable database. During local integration checks, the implemented endpoints, key edge cases, startup indexes, and explain plans were exercised against a temporary MongoDB database; the explain plans used `IXSCAN` and had no `COLLSCAN`.

## Known limitations

- Automated regression tests are not included.
- The local explain checks used a small temporary dataset; they are not a 100,000-record load or latency benchmark.
- MongoDB-unavailable behavior for `/health` was verified through its simulated exception path rather than by stopping the database during the integration run.
