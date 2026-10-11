# Employee Attendance & Analytics API

A REST API for tracking employee attendance, correcting records with a full audit trail, and reporting attendance analytics. Built with **Python, FastAPI and MongoDB**.

All application code is in a single file: `app/main.py`.

## Features

- **Employees:** create and list employees, with pagination and a department filter.
- **Attendance:** punch-in and punch-out with business rules (shift timing, late arrival, overtime, half day, overnight shifts).
- **Corrections:** manually correct a day's record. Every correction is stored in the record's history, showing what changed, who changed it, and why.
- **Analytics (MongoDB aggregation pipelines):**
  - monthly summary for one employee
  - monthly summary for a department
  - late-comers leaderboard with proper ranking of ties
  - daily department trend with a 7-day moving average
- **Query explain:** an admin endpoint that shows the MongoDB execution plan for the main queries, to confirm they use indexes.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer (a local server or a free MongoDB Atlas cluster)

## Setup and run

1. **Create and activate a virtual environment**

   ```
   python -m venv .venv
   .venv\Scripts\activate          # Windows
   source .venv/bin/activate       # macOS / Linux
   ```

2. **Install dependencies**

   ```
   pip install -r requirements.txt
   ```

3. **Configure the database.** Set these environment variables, or put them in a local `.env` file (not committed):

   | Variable | Meaning | Example |
   |---|---|---|
   | `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
   | `MONGO_DB` | Database name | `attendance_db` |

   Real environment variables take priority over the `.env` file.

4. **Start the server**

   ```
   uvicorn app.main:app --port 8000
   ```

5. **Open the interactive docs:** http://localhost:8000/docs

Indexes are created automatically at startup, so no manual database setup is needed.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Health check (pings MongoDB) |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List employees |
| POST | `/attendance/punch-in` | Record a punch-in |
| POST | `/attendance/punch-out` | Record a punch-out |
| GET | `/attendance` | List attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Correct a record (adds a history entry) |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee |
| GET | `/analytics/departments/summary` | Monthly summary per department |
| GET | `/analytics/leaderboard/late` | Late-comers leaderboard |
| GET | `/analytics/departments/{department}/trend` | Daily trend for a department |
| GET | `/admin/explain/{endpoint}` | MongoDB execution plan for a query |

## Design notes

- **Time handling:** times are sent and returned as epoch milliseconds. They are stored as UTC datetimes in MongoDB. Shift times and attendance dates use IST (UTC+05:30).
- **One record per employee per day:** enforced by a unique `(emp_code, date)` index, so two simultaneous punch-ins can never create duplicates. The second request receives `409`.
- **Concurrency:** punch-out and corrections only apply if the record is still in the state that was read. Otherwise the request receives `409` and no history is lost.
- **Exact rounding:** work hours and percentages use `Decimal` with half-up rounding, not floating-point rounding.
- **Performance:** listing, sorting and paging happen inside MongoDB with supporting indexes, not in Python.

More detail on the design choices is in `DECISIONS.md`, and the review of the original starter code is in `REVIEW.md`.

## Status

All endpoints listed above are implemented and were tested manually through the Swagger UI against MongoDB Atlas.

**Not included:**
- Automated test suite (testing was manual).
- Authentication (the `regularized_by` field on corrections is free text).

## Project structure

```
app/
  __init__.py
  main.py        # the whole application
requirements.txt
README.md
REVIEW.md        # review of the starter code
DECISIONS.md     # design decisions
```
