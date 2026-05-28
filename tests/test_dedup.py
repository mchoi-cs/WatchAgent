"""Dedup behaviour, both at the Storage layer and end-to-end through the
poller with a mocked Open-Meteo response."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import respx

from watchagent.config import CITIES, Settings
from watchagent.openmeteo import OpenMeteoClient, Reading
from watchagent.poller import Poller
from watchagent.storage import Storage


def _reading(city: str = "Ottawa", hour: int = 12) -> Reading:
    return Reading(
        city=city,
        observed_at=datetime(2026, 5, 26, hour, 0, tzinfo=timezone.utc),
        temperature_c=18.0,
        apparent_temperature_c=16.5,
        precipitation_mm=0.0,
        wind_speed_kmh=10.0,
        weather_code=1,
    )


async def test_storage_dedupes_identical_readings(storage: Storage) -> None:
    first = await storage.store_reading(_reading())
    second = await storage.store_reading(_reading())

    assert first is not None
    assert second is None, "second identical reading must be dropped"
    assert await storage.count_readings() == 1


async def test_storage_accepts_distinct_timestamps(storage: Storage) -> None:
    await storage.store_reading(_reading(hour=12))
    await storage.store_reading(_reading(hour=13))
    assert await storage.count_readings() == 2


def _open_meteo_payload(hour: int = 12, temperature: float = 18.0) -> dict[str, Any]:
    return {
        "current": {
            "time": f"2026-05-26T{hour:02d}:00",
            "temperature_2m": temperature,
            "apparent_temperature": temperature - 1.5,
            "precipitation": 0.0,
            "wind_speed_10m": 10.0,
            "weather_code": 1,
        }
    }


@respx.mock
async def test_poller_dedupes_when_open_meteo_returns_same_reading(
    storage: Storage,
) -> None:
    """Mock Open-Meteo to return the same hourly reading twice and assert
    only one row is stored per city."""
    route = respx.get("https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(200, json=_open_meteo_payload(hour=12))
    )

    settings = Settings(poll_interval_seconds=1, db_path=":memory:")
    client = OpenMeteoClient()
    poller = Poller(storage=storage, client=client, settings=settings, cities=CITIES)

    try:
        await poller.poll_once()
        first_count = await storage.count_readings()
        await poller.poll_once()
        second_count = await storage.count_readings()
    finally:
        await client.aclose()

    assert first_count == len(CITIES), "first poll should store one row per city"
    assert second_count == first_count, (
        "second poll with identical readings must not insert new rows"
    )
    assert route.called
