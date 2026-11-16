"""API tests with TestClient against the real database (loaded by the `client` fixture)."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout

from app.db import get_conn
from app.main import app

TOTAL_LOADED = 9_971


def get(client: TestClient, url: str, **params: Any) -> Any:
    response = client.get(url, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_health(client: TestClient) -> None:
    assert get(client, "/health") == {"status": "ok", "database": "ok"}


def test_list_default_page(client: TestClient) -> None:
    page = get(client, "/participants")
    assert page["limit"] == 20
    assert len(page["items"]) == 20
    assert page["total"] is None
    ids = [p["id"] for p in page["items"]]
    assert ids == sorted(ids)
    assert page["next_cursor"] == ids[-1]
    assert page["items"][0] == {
        "id": 1, "age": 40, "gender": "Male", "country": "Germany", "coffee_intake": 3.5,
        "caffeine_mg": 328.1, "sleep_hours": 7.5, "sleep_quality": "Good", "bmi": 24.9,
        "heart_rate": 78, "stress_level": "Low", "physical_activity_hours": 14.5,
        "health_issues": "None", "occupation": "Other", "smoking": False,
        "alcohol_consumption": False,
    }  # fmt: skip


def test_keyset_pagination_walks_all_rows_without_gaps_or_overlap(client: TestClient) -> None:
    params: dict[str, Any] = {"country": "Japan", "limit": 100, "include_total": True}
    seen: list[int] = []
    cursor: int | None = 0
    total = None
    while cursor is not None:
        page = get(client, "/participants", **params, after_id=cursor)
        total = page["total"]
        seen.extend(p["id"] for p in page["items"])
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == total
    assert seen == sorted(seen)
    assert total is not None and 0 < total < TOTAL_LOADED


def test_include_total_counts_all_rows(client: TestClient) -> None:
    assert get(client, "/participants", include_total=True, limit=1)["total"] == TOTAL_LOADED


def test_filters_are_applied(client: TestClient) -> None:
    params = {
        "country": ["south korea", "SPAIN"],  # normalized like the loader does
        "gender": "Female",
        "stress_level": ["Medium", "High"],
        "min_age": 30,
        "max_age": 40,
        "min_bmi": 20.5,
        "smoking": False,
        "limit": 100,
    }
    items = get(client, "/participants", **params)["items"]
    assert items
    for p in items:
        assert p["country"] in {"South Korea", "Spain"}
        assert p["gender"] == "Female"
        assert p["stress_level"] in {"Medium", "High"}
        assert 30 <= p["age"] <= 40
        assert p["bmi"] >= 20.5
        assert p["smoking"] is False


def test_filters_with_no_matches_return_empty_page(client: TestClient) -> None:
    page = get(client, "/participants", min_age=105, include_total=True)
    assert page == {"items": [], "limit": 20, "next_cursor": None, "total": 0}


@pytest.mark.parametrize(
    ("params", "loc", "msg"),
    [
        ({"min_age": 50, "max_age": 30}, ["query"], "min_age (50) must be less than or equal"),
        ({"min_bmi": 30, "max_bmi": 20}, ["query"], "min_bmi (30.0) must be less than or equal"),
        ({"gender": "Robot"}, ["query", "gender", 0], "Input should be 'Male'"),
        ({"country": "123"}, ["query", "country"], "invalid country"),
        ({"limit": 0}, ["query", "limit"], "greater than or equal to 1"),
        ({"limit": 101}, ["query", "limit"], "less than or equal to 100"),
        ({"after_id": -1}, ["query", "after_id"], "greater than or equal to 0"),
        ({"min_age": "old"}, ["query", "min_age"], "valid integer"),
        ({"min_ag": 30}, ["query", "min_ag"], "Extra inputs are not permitted"),
    ],
)
def test_invalid_parameters_return_422(
    client: TestClient, params: dict[str, Any], loc: list[Any], msg: str
) -> None:
    response = client.get("/participants", params=params)
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(e["loc"] == loc and msg in e["msg"] for e in errors), errors


def test_get_participant(client: TestClient) -> None:
    assert get(client, "/participants/2")["country"] == "Germany"


@pytest.mark.parametrize("participant_id", [3338, 99_999])  # conflicting ID / unknown ID
def test_get_missing_participant_returns_404(client: TestClient, participant_id: int) -> None:
    assert client.get(f"/participants/{participant_id}").status_code == 404


def test_summary_groups_cover_all_rows(client: TestClient) -> None:
    summary = get(client, "/participants/summary", group_by="sleep_quality")
    groups = summary["groups"]
    assert [g["group"] for g in groups] == ["Poor", "Fair", "Good", "Excellent"]  # enum order
    assert sum(g["participants"] for g in groups) == TOTAL_LOADED


def test_summary_respects_filters(client: TestClient) -> None:
    summary = get(client, "/participants/summary", group_by="coffee_intake", country="Norway")
    total = get(client, "/participants", country="Norway", include_total=True, limit=1)["total"]
    assert sum(g["participants"] for g in summary["groups"]) == total
    assert summary["groups"][0]["group"] == "0-1 cups"


def test_summary_rejects_unknown_group(client: TestClient) -> None:
    assert client.get("/participants/summary", params={"group_by": "bmi"}).status_code == 422


def test_exhausted_connection_pool_returns_503(client: TestClient) -> None:
    def busy() -> Iterator[None]:
        raise PoolTimeout("no connection available")
        yield  # pragma: no cover - makes this a generator dependency like get_conn

    app.dependency_overrides[get_conn] = busy
    try:
        response = client.get("/participants")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
