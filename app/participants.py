"""Participants endpoints: filtered keyset-paginated listing, lookup by ID and summaries."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from psycopg import Connection, sql
from psycopg.rows import DictRow

from app.cleaning import COLUMNS
from app.db import get_conn
from app.schemas import (
    ListParams,
    Participant,
    ParticipantFilters,
    ParticipantPage,
    Summary,
    SummaryGroup,
    SummaryParams,
    SummaryRow,
)

router = APIRouter(prefix="/participants", tags=["participants"])

Conn = Annotated[Connection[DictRow], Depends(get_conn)]

_SELECT_COLUMNS = sql.SQL(", ").join(map(sql.Identifier, COLUMNS.values()))

# Multi-select filters: column -> Postgres array type the parameter is cast to, so the
# comparison is `enum = any(enum[])` (no per-row cast, indexes stay usable).
_IN_FILTERS: dict[str, str] = {
    "country": "text",
    "gender": "gender",
    "sleep_quality": "sleep_quality",
    "stress_level": "stress_level",
    "health_issues": "health_issues",
    "occupation": "occupation",
}
_EQ_FILTERS = ("smoking", "alcohol_consumption")
_RANGE_FILTERS = ("age", "coffee_intake", "bmi", "sleep_hours")

# group_by -> (SQL key expression, Python label for the key). Keys are grouped and
# ordered in SQL, so enums sort in their natural order (Poor < Fair < Good < Excellent).
_GROUPS: dict[SummaryGroup, tuple[sql.Composable, Callable[[Any], str]]] = {
    SummaryGroup.AGE_GROUP: (sql.SQL("(age / 10) * 10"), lambda k: f"{k}-{k + 9}"),
    SummaryGroup.COFFEE_INTAKE: (
        sql.SQL("floor(coffee_intake)::int"),
        lambda k: f"{k}-{k + 1} cups",
    ),
    **{
        g: (sql.Identifier(g.value), str)
        for g in SummaryGroup
        if g not in (SummaryGroup.AGE_GROUP, SummaryGroup.COFFEE_INTAKE)
    },
}


def build_where(filters: ParticipantFilters) -> tuple[sql.Composable, dict[str, Any]]:
    """Translate filters into a parameterized WHERE clause (values never touch the SQL text)."""
    clauses: list[sql.Composable] = []
    params: dict[str, Any] = {}

    for column, pg_type in _IN_FILTERS.items():
        values = getattr(filters, column)
        if values:
            clauses.append(
                sql.SQL("{col} = any({ph}::{typ}[])").format(
                    col=sql.Identifier(column), ph=sql.Placeholder(column), typ=sql.SQL(pg_type)
                )
            )
            params[column] = [str(v) for v in values]

    for column in _EQ_FILTERS:
        value = getattr(filters, column)
        if value is not None:
            clauses.append(
                sql.SQL("{col} = {ph}").format(
                    col=sql.Identifier(column), ph=sql.Placeholder(column)
                )
            )
            params[column] = value

    for column in _RANGE_FILTERS:
        for bound, op in (("min", ">="), ("max", "<=")):
            name = f"{bound}_{column}"
            value = getattr(filters, name)
            if value is not None:
                clauses.append(
                    sql.SQL("{col} {op} {ph}").format(
                        col=sql.Identifier(column), op=sql.SQL(op), ph=sql.Placeholder(name)
                    )
                )
                params[name] = value

    where = sql.SQL(" and ").join(clauses) if clauses else sql.SQL("true")
    return where, params


@router.get("")
def list_participants(conn: Conn, params: Annotated[ListParams, Query()]) -> ParticipantPage:
    """List participants matching the filters, ordered by `id`, one page at a time.

    Pagination is keyset-based: follow `next_cursor` (pass it as `after_id`) until it is
    null. Each page costs the same whether it is the first or the 50,000th.
    """
    where, query_params = build_where(params)
    query = sql.SQL(
        "select {cols} from participants where {where} and id > %(after_id)s "
        "order by id limit %(fetch)s"
    ).format(cols=_SELECT_COLUMNS, where=where)
    # Fetch one extra row to know whether there is a next page without a count query.
    rows = conn.execute(
        query, {**query_params, "after_id": params.after_id, "fetch": params.limit + 1}
    ).fetchall()
    has_more = len(rows) > params.limit
    items = [Participant.model_validate(r) for r in rows[: params.limit]]

    total = None
    if params.include_total:
        count_query = sql.SQL("select count(*) as n from participants where {where}").format(
            where=where
        )
        count_row = conn.execute(count_query, query_params).fetchone()
        total = count_row["n"] if count_row else 0

    return ParticipantPage(
        items=items,
        limit=params.limit,
        next_cursor=items[-1].id if has_more else None,
        total=total,
    )


@router.get("/summary")
def summarize_participants(conn: Conn, params: Annotated[SummaryParams, Query()]) -> Summary:
    """Aggregate metrics (averages of sleep, coffee, caffeine, BMI, heart rate) per group,
    over the participants matching the same filters as the list endpoint."""
    key_expr, label = _GROUPS[params.group_by]
    where, query_params = build_where(params)
    query = sql.SQL(
        """
        select {key} as key,
               count(*) as participants,
               round(avg(sleep_hours), 2) as avg_sleep_hours,
               round(avg(coffee_intake), 2) as avg_coffee_intake,
               round(avg(caffeine_mg), 2) as avg_caffeine_mg,
               round(avg(bmi), 2) as avg_bmi,
               round(avg(heart_rate), 2) as avg_heart_rate
        from participants
        where {where}
        group by 1
        order by 1
        """
    ).format(key=key_expr, where=where)
    rows = conn.execute(query, query_params).fetchall()
    groups = [SummaryRow.model_validate({**r, "group": label(r["key"])}) for r in rows]
    return Summary(group_by=params.group_by, groups=groups)


@router.get("/{participant_id}", responses={404: {"description": "Participant not found"}})
def get_participant(
    conn: Conn, participant_id: Annotated[int, Path(ge=1, le=2_147_483_647)]
) -> Participant:
    query = sql.SQL("select {cols} from participants where id = %s").format(cols=_SELECT_COLUMNS)
    row = conn.execute(query, (participant_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"participant {participant_id} not found")
    return Participant.model_validate(row)
