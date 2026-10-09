# DECISIONS.md

1. **Indexes.** Unique `emp_code` and unique `(emp_code, date)` reject a duplicate create and a second punch-in. `(department, emp_code)` lists employees. `(joined_on)` and `(department, joined_on)` serve headcount. `(date, emp_code)` sorts attendance and scans a month; `(status, date, emp_code)` filters status; `(emp_code, punch_in)` finds the open shift. I rejected a partial index on `late_minutes > 0`, because a month total must include zeros.

2. **Punch-in race.** Both calls truncate the instant, compute the same IST date, and insert. One commit returns 201. The other hits the unique index and returns 409.

3. **Ties.** `$rank` is competition ranking (1, 2, 2, 4). Rows with `rank <= limit` are kept, so a tie on the cutoff is included and the list can be longer than `limit`. Order is late minutes descending, then `emp_code`. The next total gets the skipped rank.

4. **Headcount.** The pipeline starts from employees with `joined_on` on or before month end, then looks up logs. An empty lookup still counts. Later joiners are excluded. Headcount 0 drops the department.

5. **At 100x** I would write a daily per-employee rollup on each punch, so analytics stop scanning raw logs, and paginate lists with a keyset on `(date, emp_code)`.
