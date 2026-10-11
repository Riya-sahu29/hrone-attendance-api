
# REVIEW.md

Defects I found in the starter `app/main.py`, and how I fixed them.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Floors to whole minutes first and only then checks `> 10`, so the grace rule (strictly more than 10:00) is wrong. | Shift 09:30, punch-in 09:40:01 returns 0 instead of 10. | Compare whole seconds against 600, then floor to minutes. |
| 2 | `compute_late_minutes` | Builds the shift start with `replace()` in the datetime's own timezone (not IST). Overnight shifts and second-truncation are ignored. | A 09:50 IST punch-in gives a wrong value. | `resolve_shift` builds the IST shift start and end, and instants are truncated to whole seconds. |
| 3 | `compute_work_hours` | `round()` on a float is not half-up rounding. | 4.495 hours can come out as 4.49, which flips `half_day`. | `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | No 30-minute minimum. | Punch-out 5 minutes after shift end returns 5, should be 0. | Return 0 unless the minutes are at least 30. |
| 5 | `compute_overtime` | Mixes naive and aware datetimes, and puts the shift end on the wrong day for overnight shifts. | `TypeError`, or wrong overtime for a 22:00-06:00 shift. | The shift end comes from `resolve_shift`, and all datetimes are timezone-aware. |
| 6 | `EmployeeIn` | No validation of `emp_code`, email, shift times or `joined_on`. | `joined_on` of `10/10/2026` was accepted. | Field patterns, a real-date check, and `shift_start` must differ from `shift_end`. Invalid input returns 422. |
| 7 | `PunchInIn.status` | Accepts any string, including `ABSENT` and junk values. | Punch-in with status `ABSENT`. | Only `PRESENT`, `WFH` and `ON_DUTY` are allowed. |
| 8 | `PunchInIn.punched_at` | Accepts seconds, floats and strings. `if body.punched_at` treats 0 as "not given". | `punched_at` of 1784000000 (seconds) is accepted. | Strict integer in the epoch-milliseconds range, and the check uses `is not None`. |
| 9 | `/health` | Always returns ok, even when MongoDB is down. | Stop the database and call `/health`. | Ping MongoDB and return 503 on failure. A connection timeout is set so it does not hang. |
| 10 | `create_employee` | Check-then-insert race: two requests can both pass the check. | Two simultaneous creates both return 201. | Unique index on `emp_code`. `DuplicateKeyError` becomes 409. |
| 11 | `create_employee` | `datetime.now()` is naive local time, and the response returns a datetime instead of epoch milliseconds. | `created_at` is wrong on a server that is not in UTC. | `now_utc()`, and `employee_out` converts to epoch milliseconds. |
| 12 | `list_employees` | `skip = page * page_size` skips the whole first page. | Page 1 shows the data that belongs on page 2. | `skip = (page - 1) * page_size`. |
| 13 | `list_employees` | `total` counts all employees and ignores the department filter. | Filter by department, `total` is still the full count. | `count_documents` uses the same filter as the query. |
| 14 | `list_employees` | No sort, and `page` / `page_size` are not validated. | Unstable order, and `page=0` is accepted. | Sort by `emp_code`, and `page >= 1`, `1 <= page_size <= 100`. |
| 15 | `punch_in` | An unknown employee gives `None["shift_start"]`, which is a 500 error. | Punch-in with `emp_code` `NOPE`. | Return 404. |
| 16 | `punch_in` | `fromtimestamp()` uses server-local time, so the date is not the IST date. No overnight rule, and milliseconds are not truncated. | An overnight punch-in at 00:30 gets the wrong date. | `ms_to_dt` truncates to whole seconds, and `resolve_shift` applies the attendance-date rule. |
| 17 | `punch_in` | Check-then-insert race. | Two simultaneous punch-ins both return 201. | Unique `(emp_code, date)` index. `DuplicateKeyError` becomes 409. |
| 18 | `punch_in` response | Returns an `id` taken from `_id`, and raw datetimes. | The response contains `id`. | `attendance_out` returns no `_id` and uses epoch milliseconds. |
| 19 | `list_attendance` | Loads every record into memory, then sorts and pages in Python. | Slow and memory-heavy at 100,000 records. | Sort, skip and limit run in MongoDB, backed by an index. |
| 20 | `list_attendance` | Returns `id`. Dates and status are not validated, and `date_from` after `date_to` is accepted. | `date_from=2026-02-30` is accepted. | Date check, status limited to the five allowed values, and 422 for a bad date order. |
| 21 | Whole file | `MongoClient` is not `tz_aware`, no indexes are created at startup, and there is no connection timeout. | Naive datetimes come back from MongoDB, and queries are slow. | `tz_aware=True`, `ensure_indexes()` at startup, and `serverSelectionTimeoutMS`. |

## Looked at and decided NOT a defect

- `load_dotenv()` does not override real environment variables, so an injected `MONGO_URI` still wins. That matches the contract.
- The endpoints are plain `def`, so FastAPI runs them in a thread pool and the synchronous PyMongo calls do not block the event loop.