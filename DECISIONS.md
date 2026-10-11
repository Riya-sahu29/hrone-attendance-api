# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.**  The unique emp_code index prevents duplicate employees. The unique (emp_code, date) index prevents duplicate daily attendance. The (date, emp_code) index supports attendance list sorting, while (emp_code, punch_in) helps find the open record for punch-out. I rejected a status-only index because date filtering is more useful.
2. **Punch-in race.**Both requests try to insert the same attendance record. The unique index allows only one insert; the other raises DuplicateKeyError, and I return 409.
3. **Ties.**  $rank gives equal scores the same rank. I apply the limit after ranking, so all employees tied at the cutoff are included.
4. **Headcount.** The summary starts from employees and joins attendance logs. Employees with no logs remain in the result and are still counted.
5. **One thing you would change** I would store monthly attendance summaries. This avoids recalculating reports from many logs, but the summaries must be updated after every punch or correction.