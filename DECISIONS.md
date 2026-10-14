# Implementation decisions

1. **Indexes.** Unique `employees.emp_code` enforces identity. `(department, emp_code)` supports employee filtering; `(joined_on, department)` supports headcount eligibility, and `(department, joined_on)` supports department-specific eligibility. Unique attendance `(emp_code, date)` prevents duplicates. `(date, emp_code)` and `(status, date, emp_code)` support attendance listing; `(emp_code, punch_in)` supports punch-out lookup. I rejected `(emp_code, status, date)` because existing indexes cover main lookups; add it only if query plans justify it.

2. **Punch-in race.** Concurrent requests may both start, but the unique index allows only one employee/date insert. MongoDB rejects the loser, and the endpoint returns `409`. A pre-insert check is insufficient: both requests could check before either inserts.

3. **Ties.** `$rank` gives equal late totals the same rank and skips the next rank. Filtering `rank <= limit` afterward retains every employee tied at the cutoff.

4. **Headcount.** Department summary starts with employees joined by month-end, then looks up monthly attendance while preserving employees with no matching logs. Later joiners are filtered out.

5. **100x data.** I would monitor query plans and latency, then tune compound indexes against observed filters and sorts. The explain endpoint provides execution statistics.
