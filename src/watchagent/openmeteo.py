"""Open-Meteo HTTP client.

Owns the URL, the query-string shape, and the parsing of the JSON ``current``
block into a normalised :class:`Reading`. Nothing else in the codebase knows
about the Open-Meteo response shape, so if the upstream changes we only touch
this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .config import City

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

_CURRENT_FIELDS = (
    "temperature_2m",
    "apparent_temperature",
    "precipitation",
    "wind_speed_10m",
    "weather_code",
)


@dataclass(frozen=True)
class Reading:
    """A single normalised reading from Open-Meteo."""

    city: str
    observed_at: datetime
    temperature_c: float
    apparent_temperature_c: float
    precipitation_mm: float
    wind_speed_kmh: float
    weather_code: int


class OpenMeteoError(RuntimeError):
    """Raised when Open-Meteo returns something we cannot parse."""


class OpenMeteoClient:
    """Tiny async wrapper around ``httpx.AsyncClient``.

    Constructed once at startup and reused — keeps a connection pool open and
    avoids the TLS handshake on every poll.
    """

    def __init__(self, client: httpx.AsyncClient | None = None, timeout: float = 10.0) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def fetch_current(self, city: City) -> Reading:
        params = {
            "latitude": city.latitude,
            "longitude": city.longitude,
            "current": ",".join(_CURRENT_FIELDS),
            "wind_speed_unit": "kmh",
            "timezone": "auto",
        }
        response = await self._client.get(OPEN_METEO_URL, params=params)
        response.raise_for_status()
        payload = response.json()
        return _parse_current(city.name, payload)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _parse_current(city_name: str, payload: dict) -> Reading:
    current = payload.get("current")
    if not isinstance(current, dict):
        raise OpenMeteoError(f"missing 'current' block for {city_name}")

    try:
        observed_at = _parse_timestamp(current["time"])
        return Reading(
            city=city_name,
            observed_at=observed_at,
            temperature_c=float(current["temperature_2m"]),
            apparent_temperature_c=float(current["apparent_temperature"]),
            precipitation_mm=float(current["precipitation"]),
            wind_speed_kmh=float(current["wind_speed_10m"]),
            weather_code=int(current["weather_code"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise OpenMeteoError(f"malformed 'current' for {city_name}: {exc}") from exc


def _parse_timestamp(raw: str) -> datetime:
    """Open-Meteo returns ISO-8601 like ``2024-05-26T18:00`` (no tz when
    ``timezone=auto`` matches local). We treat the value as naive local-to-API
    time and tag it UTC for consistent ordering — the spec only requires that
    we dedup on identical timestamps, not that we reconstruct local time.
    """
    # ``fromisoformat`` accepts trailing ``Z`` from py3.11+; strip if present.
    cleaned = raw.rstrip("Z")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
