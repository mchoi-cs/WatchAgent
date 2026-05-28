"""API-shape tests against a seeded database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from watchagent.api.routes import router
from watchagent.openmeteo import Reading
from watchagent.storage import NewEvent, Storage


@pytest_asyncio.fixture
async def seeded_storage(tmp_path: Path) -> AsyncIterator[Storage]:
    db_path = tmp_path / "api.db"
    storage = Storage(str(db_path))
    await storage.connect()
    try:
        reading = Reading(
            city="Ottawa",
            observed_at=datetime(2026, 5, 26, 12, tzinfo=timezone.utc),
            temperature_c=22.0,
            apparent_temperature_c=21.0,
            precipitation_mm=0.0,
            wind_speed_kmh=12.0,
            weather_code=1,
        )
        stored = await storage.store_reading(reading)
        assert stored is not None
        await storage.store_events(
            [
                NewEvent(
                    city="Ottawa",
                    observed_at=stored.observed_at,
                    event_type="temperature_anomaly",
                    severity="warning",
                    reason="hot for Ottawa",
                    value=22.0,
                    baseline=10.0,
                    reading_id=stored.id,
                )
            ]
        )
        yield storage
    finally:
        await storage.close()


@pytest.fixture
def client(seeded_storage: Storage) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.storage = seeded_storage
    return TestClient(app)


def test_health_returns_counts(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok", "readings_stored": 1, "events_stored": 1}


def test_readings_returns_seeded_row(client: TestClient) -> None:
    response = client.get("/readings")
    assert response.status_code == 200
    body = response.json()
    assert "readings" in body
    assert len(body["readings"]) == 1
    row = body["readings"][0]
    for key in (
        "id",
        "city",
        "observed_at",
        "temperature_c",
        "apparent_temperature_c",
        "precipitation_mm",
        "wind_speed_kmh",
        "weather_code",
        "fetched_at",
    ):
        assert key in row, f"missing key {key}"
    assert row["city"] == "Ottawa"


def test_events_returns_seeded_event(client: TestClient) -> None:
    response = client.get("/events")
    assert response.status_code == 200
    body = response.json()
    assert "events" in body
    assert len(body["events"]) == 1
    ev = body["events"][0]
    for key in (
        "id",
        "city",
        "observed_at",
        "event_type",
        "severity",
        "value",
        "baseline",
        "reason",
        "reading_id",
    ):
        assert key in ev, f"missing key {key}"
    assert ev["event_type"] == "temperature_anomaly"


def test_city_filter_validates(client: TestClient) -> None:
    response = client.get("/readings", params={"city": "Calgary"})
    assert response.status_code == 400


def test_city_filter_filters(client: TestClient) -> None:
    response = client.get("/readings", params={"city": "Toronto"})
    assert response.status_code == 200
    assert response.json() == {"readings": []}


def test_limit_is_clamped(client: TestClient) -> None:
    response = client.get("/readings", params={"limit": 0})
    assert response.status_code == 422
    response = client.get("/readings", params={"limit": 10_000})
    assert response.status_code == 422
