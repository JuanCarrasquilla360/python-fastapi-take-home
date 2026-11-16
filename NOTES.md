# Notes

## Running it

```bash
make setup                   # uv sync + .env
make db-reset && make load   # fresh schema, then load the CSV
make dev                     # http://localhost:8000/docs
make test                    # 103 tests (needs the database running)
make lint                    # ruff + ruff format --check + mypy --strict, all clean
```

`make test` does not depend on `make load` having run: the API fixture runs the loader
first, and since the loader is idempotent that costs about one second.

**Windows without `make`:** run the commands from the README table directly, e.g.
`docker compose down -v; docker compose up -d --wait db` then
`uv run python scripts/load_data.py data/coffee_health_raw.csv`.

### Load result

```
rows read                                          10,015
blank lines skipped                                     3
rejected (invalid)                                     25
skipped (exact duplicate)                               8
rejected (conflicting duplicate)                        8
inserted                                            9,971
fixed (normalized, then loaded or deduplicated)        56
participants in database                            9,971
```

`10,015 = 3 + 25 + 8 + 8 + 9,971`. The loader checks this invariant and aborts the
transaction if it does not hold. Every line that was not loaded is in `load_rejections`:

```sql
select line_number, kind, reason from load_rejections
where run_id = (select max(id) from load_runs) order by line_number;
```

A second `make load` reports `inserted 0, updated 0, unchanged 9,971`.

---

## Layout

| File | What it does |
| --- | --- |
| `db/init/001_schema.sql` | Tables, enum types, constraints and indexes (replaces the sample `notes` table) |
| `app/cleaning.py` | Pure parsing, validation and normalization. Shared by the loader and the API |
| `scripts/load_data.py` | Streaming loader: validate → COPY to staging → dedupe in SQL → upsert |
| `app/schemas.py` | Pydantic query-parameter and response models |
| `app/participants.py` | `/participants` endpoints and SQL building |
| `tests/test_cleaning.py` | Validation and normalization unit tests (no database) |
| `tests/test_loader.py` | Loader tests against Postgres, inside a rolled-back transaction |
| `tests/test_api.py` | `TestClient` tests against the real database |

## Schema

`participants` has one row per person.

- **`id integer primary key`** is the source system's ID, not a surrogate key. Using it as the
  key is what makes the load idempotent (`insert ... on conflict (id)`).
- **Closed vocabularies are Postgres `enum` types** (gender, sleep_quality, stress_level,
  health_issues, occupation). They take 4 bytes, reject typos at the database, and sort in
  declaration order. That gives `Poor < Fair < Good < Excellent` in summaries and `ORDER BY`,
  where `text` would sort alphabetically. The cost is that adding a value needs a migration
  (`alter type ... add value`). That is acceptable for vocabularies this stable.
- **`country` is `text`**, not an enum: it is an open set, and a new country in an export
  should not require a schema change.
- **Measurements are `numeric(p,1)`**, which is exact and matches the one-decimal source
  precision. Age and heart rate are `smallint`; smoking and alcohol are `boolean`.
- **`CHECK` constraints mirror the loader's plausibility limits.** The loader rejects first
  with a readable reason; the constraints guarantee that no other writer can bypass the rules.
- **`load_runs` and `load_rejections` form the audit trail.** `load_runs` holds the counters of
  each run. `load_rejections` holds every line a run did not load: its `kind` (`invalid` /
  `duplicate` / `conflict`), the reason, and the raw fields.

Indexes are covered in [Scale](#indexes) below.

## Cleaning and validation

Every row goes through `app/cleaning.parse_row`, which returns either a typed `Record` or
the list of **all** errors in the row, not just the first. It also returns which fields it
had to fix.

### What gets fixed (56 rows)

| Problem | Example | Normalization | Rows |
| --- | --- | --- | --- |
| Whitespace / case in country | `'\tUSA '`, `BELGIUM`, `france` | Trim, collapse inner whitespace, case-insensitive match to a canonical list | 18 |
| Whitespace / case in enums | `'\tMale  '`, `MALE`, `GOOD`, `medium`, `' Other  '` | Trim and case-insensitive match | 32 |
| Booleans spelled out | `Y`, `yes`, `no`, `false` | `1/true/t/yes/y` → true, `0/false/f/no/n` → false | 6 |
| BOM in the header | `﻿ID` | File read as `utf-8-sig` | — |

Integers written as `40.0` and decimals with more than one decimal place are also accepted
and normalized, rounding half-even to 0.1. Both count as "fixed". The dataset has none.

Unknown countries are **accepted** if they look like a name (letters, spaces, `'`, `-`, `.`,
at most 64 characters). They are trimmed but not re-cased. Rejecting them would silently
drop data the next time the source adds a country. The 20 known countries get their
canonical spelling (`UK`, `USA`, `South Korea`).

### What gets rejected

Plausibility limits are defined once in `app/cleaning.py` (`LIMITS`) and mirrored by the
`CHECK` constraints. They are deliberately generous: they reject impossible values, not
unusual ones. All valid data in the file sits well inside them.

| Field | Accepted range | Reasoning |
| --- | --- | --- |
| Age | 18–110 | The dataset is adults; 110 is above any realistic participant |
| Coffee_Intake | 0–20 cups/day | Data max is 8.2 |
| Caffeine_mg | 0–2000 mg/day | Data max is 780; 0 is valid even with coffee > 0 and vice versa (tea, decaf) |
| Sleep_Hours | 1–18 h/night | Average hours per night; 0, negative or more than 24 is impossible |
| BMI | 10–70 | Survivable human range; 0 is a missing value in disguise |
| Heart_Rate | 25–220 bpm | Resting rate: elite athletes go below 40; 0 and 350 are not physiological |
| Physical_Activity_Hours | 0–100 h/week | Data max is 15 |

Rejected lines in this file (**25 invalid**):

| Reason | Lines |
| --- | --- |
| Age not a number (`thirty`, `4O`, `unknown`, empty) | 816, 1918, 3240, 1154 |
| Age out of range (212, 150, -3) | 1114, 2128, 6606 |
| Sleep_Hours out of range (-1, 25) / not a number (`seven`) / empty | 107, 3187, 7089, 1809 |
| Gender invalid (empty, `?`, `N/A`) | 3517, 4128, 4380 |
| BMI 0 / `n/a` / empty | 6803, 7902, 4880 |
| Heart_Rate 0 / 350 / empty | 5894, 9872, 6988 |
| Smoking `-1` / `2` | 3388, 8226 |
| Wrong number of fields (13, 13, 17) | 4869, 7175, 2659 |

`4O` (letter O) is rejected, not "fixed" to 40. Guessing at typos means loading a value
nobody wrote. The same applies to `thirty`.

Line numbers are physical file lines (header = line 1, blank lines counted), so
`sed -n '816p' data/coffee_health_raw.csv` shows the rejected row.

### Duplicates and the ambiguous case

Twelve IDs appear twice:

- **8 exact duplicates** (all 16 fields identical). The earliest line is loaded and the
  repeat is logged as `duplicate`. Nothing is lost.
- **4 conflicting IDs** (3338, 6115, 6748, 7494): same ID, but `Age` and `Heart_Rate` differ.
  **Both lines are rejected** and logged as `conflict`, with the line numbers of both.

  Why reject both instead of keeping the first or the last: the file gives no ordering or
  timestamp that says which version is correct. "First wins" would be a guess hidden in
  the code. Rejecting is visible: the rows are in `load_rejections` with both line numbers,
  ready for someone with access to the source system to resolve. Losing 4 people out of
  ~10k is a much smaller harm than serving a wrong age as if it were fact.

  A conflict decides on **normalized** values: `Male` vs `MALE` is an exact duplicate, not
  a conflict.

### Re-running and later exports

The upsert is `on conflict (id) do update ... where (row) is distinct from (excluded row)`:

- same file again → every row `unchanged`, no writes, no duplicate data;
- a newer export with corrected values for an existing ID → the row is `updated`, so the
  latest export is the source of truth;
- `load_runs` and `load_rejections` get a new run each time. They are an audit log, so that
  is expected.

An ID that is in conflict within a file is left untouched in the table if an earlier run
had already loaded it.

## The API

| Endpoint | Purpose |
| --- | --- |
| `GET /participants` | Filtered list, keyset-paginated |
| `GET /participants/{id}` | One participant, or 404 |
| `GET /participants/summary?group_by=...` | Count and averages (sleep, coffee, caffeine, BMI, heart rate) per group, with the same filters |
| `GET /health` | Liveness and database check |

### Filters

- **Multi-select** (repeat the parameter, values are OR-ed): `country`, `gender`,
  `sleep_quality`, `stress_level`, `health_issues`, `occupation`. Country goes through the
  same `normalize_country` as the loader, so `?country=south korea` works.
- **Boolean:** `smoking`, `alcohol_consumption`.
- **Inclusive ranges:** `min_/max_age`, `min_/max_coffee_intake`, `min_/max_bmi`,
  `min_/max_sleep_hours`.
- Different filters are AND-ed.

`group_by` accepts `country`, `gender`, `occupation`, `sleep_quality`, `stress_level`,
`health_issues`, `age_group` (decades) and `coffee_intake` (whole-cup buckets
`[n, n+1)`, the default).

**Validation returns 422 with a precise `loc`** for: min > max on any range, unknown enum
values, a country that is not a name, `limit` outside 1–100, negative `after_id`, wrong
types, and **unknown query parameters**. The filter model uses `extra="forbid"`, so a typo
like `min_ag=30` returns 422 instead of silently returning unfiltered data.

Values never touch the SQL text: the `WHERE` clause is built with `psycopg.sql`
identifiers and named placeholders. Enum filters are cast to the enum array type
(`gender = any(%s::gender[])`) so the comparison needs no per-row cast.

### Pagination

Pagination is keyset (cursor), not `OFFSET`:

```
GET /participants?country=Japan&limit=50                 -> { items, next_cursor: 812, ... }
GET /participants?country=Japan&limit=50&after_id=812    -> next page; next_cursor null on the last one
```

- The query is `where <filters> and id > :after_id order by id limit :limit + 1`. The extra
  row tells us whether there is a next page without running a count.
- The total is opt-in (`include_total=true`). An exact `count(*)` over a filtered set of
  tens of millions of rows is the most expensive part of a list request, and most clients
  just follow `next_cursor`.
- Trade-offs:
  - Results are ordered by `id` only. Arbitrary sort orders would need a composite cursor
    (`(sort_col, id)`) and an index per sortable column.
  - Clients cannot jump to "page 37". For a data API that is acceptable; for a UI with page
    numbers it would not be.
  - A cursor stays correct while rows are inserted or deleted, which `OFFSET` does not:
    with `OFFSET`, rows shift and pages skip or repeat rows.

---

## Scale

Measured on a laptop (Docker Desktop on Windows, Postgres 17) with a synthetic file of
**1,001,200 rows (78 MB)**: the dataset repeated 100 times with shifted IDs, dirty rows
included.

### Loader

| | Result |
| --- | --- |
| Memory | Constant: about 27 MB peak Python allocation for 1M rows. It streams the file and holds one 10k-row batch at a time |
| Fresh load of 1M rows | **79 s (~12,700 rows/s)**. With the 4 secondary indexes dropped: 61 s, plus 4.4 s to rebuild them |
| Re-run of the same 1M rows | All `unchanged`, no table writes |
| Python parsing alone | ~49,000 rows/s |
| Estimate for 10M rows | ~13 min as is |

How it scales:

- **No per-row database round trips.** Rows go through `COPY ... FROM STDIN` in 10k-row
  batches into an unlogged temporary staging table. Deduplication (`group by id`) and the
  upsert (`insert ... select distinct on (id) ... on conflict`) are single set-based
  statements, so Python never holds an "IDs seen" set that would grow with the file.
- **Profiled and optimized once.** The first version parsed 23k rows/s. Skipping the regex
  on already-clean fields, dict lookups for enums instead of linear scans, and an explicit
  `as_row` doubled it to 49k rows/s.
- **One transaction.** A load is all-or-nothing, so a crash halfway leaves the table
  exactly as it was. The cost: at tens of millions of rows, this is one long transaction
  plus a temp table about the size of the data, and the upsert holds row locks on
  the rows it changes until commit. Reads are not blocked thanks to MVCC.

What I would do next at 10–100M rows, in this order:

1. **Bulk-load mode:** for an initial or full reload, drop the secondary indexes and
   rebuild them after the load. Measured here: indexes add 28 % to the load, while
   rebuilding them is 4.4 s per 1M rows.
2. **Parallel parsing:** parsing is CPU-bound and single-threaded. Splitting the file by
   byte ranges across processes, each with its own COPY into staging, would scale with
   cores until Postgres write throughput becomes the limit.
3. **Commit per chunk into staging** (a persistent staging table keyed by run), so a crash
   at row 40M can resume instead of starting over. The final dedupe and upsert stays one
   statement.

### Indexes

Every list query has the shape `where <filters> and id > :cursor order by id limit n`.

| Index | Why | Size at 1M rows |
| --- | --- | --- |
| `participants_pkey (id)` | Keyset order, lookup by ID, upsert conflict target | 21 MB |
| `(country, id)` | The most common filter, already in `id` order: country + cursor is a single index range scan | 29 MB |
| `(age)`, `(bmi)`, `(coffee_intake)` | The range filters; selective ranges become bitmap scans | 6–21 MB each |

There are **no indexes on the low-cardinality enums or booleans.** A value that matches
20–70 % of the table (gender, smoking, stress) is read faster by walking the primary key
and filtering. A standalone index on them would cost writes and never be chosen.

**Cost:** the secondary indexes together take 98 MB, about the size of the table itself
(105 MB), and add ~28 % to load time. That is why the index list is short and tied to real
filters, not "one per column".

Measured plans at 1M rows (`explain analyze`):

| Query | Plan | Time |
| --- | --- | --- |
| Page 1 (`id > 0 limit 21`) | PK index scan | 0.08 ms |
| Deep page (`id > 1,900,000`) | PK index scan | 0.10 ms |
| **Same depth with `OFFSET 980000`** | PK scan over 980k rows | **208 ms** |
| `country=Japan` + deep cursor | PK index scan + filter | 0.22 ms |
| `country in (Japan, Spain) & Female & age 30–40` | PK index scan + filter | 0.34 ms |
| Rare value `age >= 79` (200 rows) | Bitmap scan on age index + top-N sort | 1.6 ms |
| No matches `age >= 100` | Age index, empty | 0.06 ms |
| Rare enum `health_issues=Severe` (0.2 %) | PK index scan + filter | 4.3 ms |
| `include_total` for a 50 % filter | Parallel seq scan | 75 ms |
| Summary group by coffee bucket | Parallel seq scan + hash aggregate | 165 ms |

So **page 1 and page 50,000 cost the same** with keyset pagination, and OFFSET degrades
linearly.

Two cases would degrade at tens of millions of rows:

- **A rare value of an unindexed column combined with the `id` order.** Postgres walks the
  PK until it finds `limit` matches. At 100M rows with a 0.01 % match, that becomes a long
  scan. The fix is targeted: a partial index (`where health_issues = 'Severe'`) or a
  composite `(column, id)` index if that filter turns out to be common in practice.
- **Summaries and `include_total`** are full scans by nature: they are aggregates. At 10M+
  rows I would serve summaries from a materialized view (refreshed by the loader at the
  end of each run), or from pre-aggregated rollups per (country, group). I would also cap
  or estimate the count (`explain` row estimate, or a `count` over at most N rows).

### Connections under concurrent load

- The endpoints are sync `def`, so FastAPI runs them in its threadpool (40 threads by
  default) over a psycopg `ConnectionPool` (`DB_POOL_MAX_SIZE`, default 10).
- Each request holds a connection only for its queries, in autocommit mode (read-only API,
  so there is no BEGIN/COMMIT round trip).
- When all connections are busy, a request waits up to `DB_POOL_TIMEOUT` (5 s) and then
  gets a **503 with `Retry-After`**. That is load shedding instead of an ever-growing queue
  and timeouts piling up.
- Sizing: total connections = API processes × `max_size`, and it must stay below Postgres'
  `max_connections` (100 by default). With N uvicorn workers or replicas I would set
  `max_size` to about `max_connections / N` minus headroom, or put PgBouncer (transaction
  pooling) in front of Postgres.
- Queries are short (sub-millisecond list pages), so 10 connections serve far more than 10
  concurrent users. The summary endpoint is the one that holds a connection for long; at
  scale, the materialized view removes that problem.
- An async stack (`AsyncConnectionPool` + `async def`) would save threads, not database
  work. The bottleneck under load is the database and its connection count, not the
  threads, so I kept the sync code that came with the skeleton.

## Assumptions and things I left out

- The CSV header must match the 16 expected columns, in order (case and whitespace are
  ignored). Anything else aborts the load before writing, since a reordered export would
  otherwise load columns into the wrong fields.
- `Caffeine_mg` and `Coffee_Intake` are not cross-validated. The ratio in the data varies
  (49–140 mg per cup) and 30 rows have caffeine with 0 cups, which is plausible (tea, energy
  drinks).
- No authentication, no rate limiting, no migration tool: the README's `db/init` approach
  is enough for a single schema version. With a second version I would move to Alembic or
  dbmate.
- No CORS and no front end. `/docs` is the UI, as the README suggests.
