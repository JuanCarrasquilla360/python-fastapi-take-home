from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import TupleRow

from app.config import settings
from app.main import app
from scripts.load_data import load

DATASET = Path(__file__).resolve().parents[1] / "data" / "coffee_health_raw.csv"


@pytest.fixture
def db() -> Iterator[psycopg.Connection[TupleRow]]:
    """A database connection whose changes are rolled back at the end of the test."""
    with psycopg.connect(settings.database_url) as conn, conn.transaction(force_rollback=True):
        yield conn


@pytest.fixture(scope="session")
def loaded_dataset() -> None:
    """Make sure the real dataset is loaded. The loader is idempotent, so this is a no-op
    (every row 'unchanged') when `make load` has already been run."""
    with psycopg.connect(settings.database_url) as conn:
        load(conn, DATASET, report=lambda _: None)


@pytest.fixture
def client(loaded_dataset: None) -> Iterator[TestClient]:
    # Using TestClient as a context manager runs the app lifespan (opens the DB pool).
    with TestClient(app) as c:
        yield c
