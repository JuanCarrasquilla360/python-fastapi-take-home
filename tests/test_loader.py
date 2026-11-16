"""Loader tests.

`iter_csv` is tested without a database. The `load` tests run against the real database
inside a transaction that is rolled back, so they leave no trace.
"""

import io
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import TupleRow

from scripts.load_data import iter_csv, load
from tests.conftest import DATASET
from tests.test_cleaning import HEADER

Conn = psycopg.Connection[TupleRow]

# IDs far above the dataset's so these rows never collide with the real data.
SAMPLE = "\n".join(
    [
        ",".join(HEADER),
        "900001,40,Male,Germany,3.5,328.1,7.5,Good,24.9,78,Low,14.5,None,Other,0,0",
        "900002,33, male ,SPAIN,1.0,94.1,6.2,good,20.0,67,Low,11.0,None,Service,no,Y",  # fixed
        "",  # blank
        "900003,thirty,Male,Germany,3.5,328.1,7.5,Good,24.9,78,Low,14.5,None,Other,0,0",  # bad
        "900001,40,Male,Germany,3.5,328.1,7.5,Good,24.9,78,Low,14.5,None,Other,0,0",  # dup
        "900004,30,Female,Italy,1.6,152.7,6.3,Good,20.0,80,Low,14.1,None,Student,0,1",  # conflict
        "900004,26,Female,Italy,1.6,152.7,6.3,Good,20.0,86,Low,14.1,None,Student,0,1",  # conflict
        "900005,30,Female,Italy,1.6,152.7,6.3,Good,20.0,80,Low",  # too few fields
    ]
)


@pytest.fixture
def sample_csv(tmp_path: Path) -> Path:
    path = tmp_path / "sample.csv"
    path.write_text(SAMPLE + "\n", encoding="utf-8")
    return path


def test_iter_csv_yields_physical_line_numbers() -> None:
    rows = list(iter_csv(io.StringIO(SAMPLE)))
    assert [n for n, _ in rows] == [2, 3, 4, 5, 6, 7, 8, 9]
    assert rows[2] == (4, [])  # blank line


def test_iter_csv_rejects_bad_header() -> None:
    with pytest.raises(ValueError, match="unexpected CSV header"):
        list(iter_csv(io.StringIO("id,name\n1,x\n")))


def test_load_classifies_every_line(db: Conn, sample_csv: Path) -> None:
    run_id, summary = load(db, sample_csv, report=lambda _: None)

    assert (summary.read, summary.blank, summary.invalid) == (8, 1, 2)
    assert (summary.duplicate, summary.conflict) == (1, 2)
    assert summary.inserted + summary.updated + summary.unchanged == 2
    assert summary.fixed == 1

    rejections = db.execute(
        "select line_number, kind from load_rejections where run_id = %s order by line_number",
        (run_id,),
    ).fetchall()
    assert rejections == [
        (5, "invalid"),
        (6, "duplicate"),
        (7, "conflict"),
        (8, "conflict"),
        (9, "invalid"),
    ]
    stored = db.execute(
        "select id, gender::text, country, smoking, alcohol_consumption "
        "from participants where id between 900001 and 900005 order by id"
    ).fetchall()
    assert stored == [
        (900001, "Male", "Germany", False, False),
        (900002, "Male", "Spain", False, True),
    ]


def test_load_is_idempotent(db: Conn) -> None:
    _, first = load(db, DATASET, report=lambda _: None)
    _, second = load(db, DATASET, report=lambda _: None)

    assert (second.inserted, second.updated) == (0, 0)
    assert second.unchanged == first.inserted + first.updated + first.unchanged
    row = db.execute("select count(*) from participants").fetchone()
    assert row is not None
    assert row[0] == second.unchanged


def test_load_matches_expected_dataset_summary(db: Conn) -> None:
    """Documents (and guards) the numbers reported in NOTES.md."""
    _, s = load(db, DATASET, report=lambda _: None)
    assert (s.read, s.blank, s.invalid, s.duplicate, s.conflict) == (10_015, 3, 25, 8, 8)
    assert s.inserted + s.updated + s.unchanged == 9_971
    assert s.fixed == 56
