"""Load the coffee health CSV into Postgres.

Usage:
    uv run python scripts/load_data.py data/coffee_health_raw.csv

Pipeline (one transaction, constant memory in the size of the file):
    1. Stream the CSV with csv.reader and parse/validate/normalize every row in Python
       (app/cleaning.py). Invalid rows are reported and stored in `load_rejections`.
    2. COPY valid rows, in batches, into a temporary staging table together with their
       line number.
    3. Resolve duplicate IDs in SQL (set-based, so no per-ID state is kept in Python):
       exact duplicates keep their earliest line; IDs whose lines disagree are rejected.
    4. Upsert the survivors into `participants` with ON CONFLICT (id): a second run of the
       same file inserts and updates nothing, so the load is idempotent.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass
from itertools import batched
from pathlib import Path
from typing import Any, TextIO

# Allow `python scripts/load_data.py` (the Makefile entry point) to import the `app` package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow

from app.cleaning import COLUMNS, parse_row, validate_header
from app.config import settings

BATCH_SIZE = 10_000

Conn = psycopg.Connection[TupleRow]
Reporter = Callable[[str], None]

_COLS = list(COLUMNS.values())
_COL_LIST = sql.SQL(", ").join(map(sql.Identifier, _COLS))
_NON_KEY_COLS = [c for c in _COLS if c != "id"]


@dataclass(slots=True)
class LoadSummary:
    read: int = 0  # data lines read (header excluded)
    blank: int = 0  # empty lines, skipped
    fixed: int = 0  # valid lines that needed normalization (subset of the valid ones)
    invalid: int = 0  # failed validation
    duplicate: int = 0  # exact repeats of an earlier line, skipped
    conflict: int = 0  # lines sharing an ID with different values, all rejected
    inserted: int = 0  # new rows in participants
    updated: int = 0  # existing rows whose values changed
    unchanged: int = 0  # already in participants with identical values (re-runs)

    @property
    def accounted(self) -> int:
        return (
            self.blank
            + self.invalid
            + self.duplicate
            + self.conflict
            + self.inserted
            + self.updated
            + self.unchanged
        )

    def render(self) -> str:
        labels = {
            "read": "rows read",
            "blank": "blank lines skipped",
            "invalid": "rejected (invalid)",
            "duplicate": "skipped (exact duplicate)",
            "conflict": "rejected (conflicting duplicate)",
            "inserted": "inserted",
            "updated": "updated",
            "unchanged": "unchanged (already loaded)",
            "fixed": "fixed (normalized, then loaded or deduplicated)",
        }
        width = max(map(len, labels.values()))
        values = asdict(self)
        return "\n".join(f"  {label:<{width}}  {values[key]:>8,}" for key, label in labels.items())


@dataclass(frozen=True, slots=True)
class _Rejection:
    line_number: int
    source_id: str | None
    reason: str
    raw_fields: list[str]


def iter_csv(stream: TextIO) -> Iterator[tuple[int, list[str]]]:
    """Yield (line_number, fields) for each data row. Validates the header first."""
    reader = csv.reader(stream)
    header = next(reader, None)
    if header is None:
        raise ValueError("the CSV file is empty")
    validate_header(header)
    for fields in reader:
        yield reader.line_num, fields


def _copy_rows(
    conn: Conn, statement: sql.SQL | sql.Composed, rows: Iterable[tuple[Any, ...]]
) -> None:
    with conn.cursor() as cur, cur.copy(statement) as copy:
        for row in rows:
            copy.write_row(row)


def _stage(
    conn: Conn,
    run_id: int,
    rows: Iterable[tuple[int, list[str]]],
    summary: LoadSummary,
    report: Reporter,
) -> None:
    """Validate rows and COPY them, batch by batch, into staging / load_rejections."""
    copy_staging = sql.SQL("copy staging (line_number, {}) from stdin").format(_COL_LIST)
    copy_rejections = sql.SQL(
        "copy load_rejections (run_id, line_number, kind, source_id, reason, raw_fields) from stdin"
    )
    for batch in batched(rows, BATCH_SIZE):
        valid: list[tuple[Any, ...]] = []
        rejected: list[_Rejection] = []
        for line_number, fields in batch:
            summary.read += 1
            if not any(f.strip() for f in fields):
                summary.blank += 1
                continue
            result = parse_row(fields)
            if result.record is None:
                summary.invalid += 1
                reason = "; ".join(result.errors)
                source_id = fields[0].strip() or None if fields else None
                rejected.append(_Rejection(line_number, source_id, reason, fields))
                report(f"line {line_number}: rejected: {reason}")
                continue
            if result.fixes:
                summary.fixed += 1
            valid.append((line_number, *result.record.as_row()))

        _copy_rows(conn, copy_staging, valid)
        _copy_rows(
            conn,
            copy_rejections,
            (
                (run_id, r.line_number, "invalid", r.source_id, r.reason, r.raw_fields)
                for r in rejected
            ),
        )


def _resolve_duplicates(conn: Conn, run_id: int, summary: LoadSummary, report: Reporter) -> None:
    """Classify repeated IDs in staging and log every line that will not be loaded."""
    conn.execute("analyze staging")
    # Exact duplicates have a single distinct value tuple; conflicts have more than one.
    conn.execute(
        sql.SQL(
            """
            create temp table staging_dups on commit drop as
            select id,
                   count(distinct ({non_key})) > 1 as is_conflict,
                   array_agg(line_number order by line_number) as lines
            from staging
            group by id
            having count(*) > 1
            """
        ).format(non_key=sql.SQL(", ").join(map(sql.Identifier, _NON_KEY_COLS)))
    )
    rows = conn.execute(
        """
        insert into load_rejections (run_id, line_number, kind, source_id, reason)
        select %(run_id)s, s.line_number,
               case when d.is_conflict then 'conflict' else 'duplicate' end,
               s.id::text,
               case when d.is_conflict
                    then 'conflicting duplicate: ID ' || s.id || ' appears on lines '
                         || array_to_string(d.lines, ', ') || ' with different values'
                    else 'exact duplicate of line ' || d.lines[1]
               end
        from staging s
        join staging_dups d using (id)
        where d.is_conflict or s.line_number <> d.lines[1]
        returning kind, line_number, reason
        """,
        {"run_id": run_id},
    ).fetchall()
    for kind, line_number, reason in sorted(rows, key=lambda r: r[1]):
        if kind == "conflict":
            summary.conflict += 1
        else:
            summary.duplicate += 1
        report(f"line {line_number}: {'rejected' if kind == 'conflict' else 'skipped'}: {reason}")


def _upsert(conn: Conn, summary: LoadSummary) -> None:
    """Move one row per non-conflicting ID from staging into participants."""
    query = sql.SQL(
        """
        with candidates as (
            select distinct on (id) {cols}
            from staging s
            where not exists (select 1 from staging_dups d where d.id = s.id and d.is_conflict)
            order by id, line_number
        ),
        written as (
            insert into participants ({cols})
            select {cols} from candidates
            on conflict (id) do update set {assignments}
            where ({current}) is distinct from ({incoming})
            returning (xmax = 0) as inserted
        )
        select (select count(*) from candidates),
               count(*) filter (where inserted),
               count(*) filter (where not inserted)
        from written
        """
    ).format(
        cols=_COL_LIST,
        assignments=sql.SQL(", ").join(
            sql.SQL("{c} = excluded.{c}").format(c=sql.Identifier(c)) for c in _NON_KEY_COLS
        ),
        current=sql.SQL(", ").join(sql.Identifier("participants", c) for c in _NON_KEY_COLS),
        incoming=sql.SQL(", ").join(sql.Identifier("excluded", c) for c in _NON_KEY_COLS),
    )
    row = conn.execute(query).fetchone()
    assert row is not None
    candidates, summary.inserted, summary.updated = row
    summary.unchanged = candidates - summary.inserted - summary.updated


def load(conn: Conn, csv_path: Path, report: Reporter = print) -> tuple[int, LoadSummary]:
    """Load `csv_path` in a single transaction. Returns (run_id, summary)."""
    summary = LoadSummary()
    with conn.transaction():
        row = conn.execute(
            "insert into load_runs (source_file) values (%s) returning id", (str(csv_path),)
        ).fetchone()
        assert row is not None
        run_id: int = row[0]
        conn.execute(
            "create temp table staging (line_number integer not null, like participants) "
            "on commit drop"
        )
        with csv_path.open(encoding="utf-8-sig", newline="") as stream:
            _stage(conn, run_id, iter_csv(stream), summary, report)
        _resolve_duplicates(conn, run_id, summary, report)
        _upsert(conn, summary)

        if summary.accounted != summary.read:  # every line must land in exactly one bucket
            raise RuntimeError(f"row accounting mismatch: {summary}")
        conn.execute(
            """
            update load_runs set finished_at = now(),
                rows_read = %(read)s, rows_blank = %(blank)s, rows_fixed = %(fixed)s,
                rows_invalid = %(invalid)s, rows_duplicate = %(duplicate)s,
                rows_conflict = %(conflict)s, rows_inserted = %(inserted)s,
                rows_updated = %(updated)s, rows_unchanged = %(unchanged)s
            where id = %(run_id)s
            """,
            {**asdict(summary), "run_id": run_id},
        )
        # Temp tables also drop on commit; dropping explicitly lets load() run more than
        # once inside an outer transaction (where its transaction is just a savepoint).
        conn.execute("drop table staging, staging_dups")
    return run_id, summary


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument(
        "--quiet", action="store_true", help="do not print each rejected/skipped line"
    )
    args = parser.parse_args()

    report: Reporter = (lambda _: None) if args.quiet else print
    started = time.perf_counter()
    try:
        with psycopg.connect(args.database_url) as conn:
            run_id, summary = load(conn, args.csv_path, report)
            total_row = conn.execute("select count(*) from participants").fetchone()
    except psycopg.errors.UndefinedTable as exc:
        raise SystemExit(f"error: {exc}\nhint: apply the schema with `make db-reset`") from exc
    except (OSError, ValueError, psycopg.Error) as exc:  # bad file / header / connection
        raise SystemExit(f"error: {exc}") from exc
    elapsed = time.perf_counter() - started

    print(f"\nLoad run {run_id}: {args.csv_path} in {elapsed:.2f}s")
    print(summary.render())
    print(f"  {'participants in database':<46}  {total_row[0] if total_row else 0:>8,}")
    print(f"\nDetails: select * from load_rejections where run_id = {run_id} order by line_number;")


if __name__ == "__main__":
    main()
