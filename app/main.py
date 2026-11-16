from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import PoolTimeout
from pydantic import BaseModel

from app import participants
from app.db import create_pool, get_conn


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.pool = create_pool()
    yield
    app.state.pool.close()


app = FastAPI(
    title="Coffee Health API",
    description="Filter and summarize the cleaned coffee & health dataset.",
    lifespan=lifespan,
)
app.include_router(participants.router)


@app.exception_handler(PoolTimeout)
def pool_exhausted(request: Request, exc: PoolTimeout) -> JSONResponse:
    """All DB connections busy for longer than the pool timeout: shed load with a 503."""
    return JSONResponse(
        status_code=503,
        content={"detail": "database busy, retry later"},
        headers={"Retry-After": "1"},
    )


Conn = Annotated[Connection[DictRow], Depends(get_conn)]


class Health(BaseModel):
    status: str
    database: str


@app.get("/health")
def health(conn: Conn) -> Health:
    conn.execute("select 1")
    return Health(status="ok", database="ok")
