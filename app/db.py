from collections.abc import Iterator

from fastapi import Request
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from app.config import settings

Pool = ConnectionPool[Connection[DictRow]]


def create_pool() -> Pool:
    """Create and open a connection pool. Called once per app lifespan."""
    pool: Pool = ConnectionPool(
        settings.database_url,
        connection_class=Connection[DictRow],
        # The API only reads: autocommit avoids a BEGIN/COMMIT round trip per request.
        kwargs={"row_factory": dict_row, "autocommit": True},
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        timeout=settings.db_pool_timeout,
        open=False,
    )
    pool.open(wait=True)
    return pool


def get_conn(request: Request) -> Iterator[Connection[DictRow]]:
    """FastAPI dependency that borrows a connection from the pool for one request."""
    pool: Pool = request.app.state.pool
    with pool.connection() as conn:
        yield conn
