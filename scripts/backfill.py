#!/usr/bin/env python3
"""Backfill real historical readings and run them through the detectors.

This is a **developer / demo tool**, not part of the graded HTTP service. It
exists so you can populate the database with real recent weather instead of
waiting hours for the live poller to accumulate enough history for the
event detectors to fire.

It uses the *same* Open-Meteo endpoint the poller uses, with the endpoint's
``past_days`` parameter to pull a window of real historical hourly readings,
then feeds each reading (in chronological order, across all cities) through
the exact same public functions the poller uses:

    storage.store_reading  ->  events.candidate_events  ->  events.apply_cooldown
                           ->  storage.store_events

so the events produced here are identical to what the live system would have
produced had it been running the whole time.

Usage:
    python scripts/backfill.py --db ./data/weather.db --past-days 31
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))

from watchagent import events as event_logic  # noqa: E402
from watchagent.config import CITIES, City  # noqa: E402
from watchagent.openmeteo import OPEN_METEO_URL, Reading, _parse_timestamp  # noqa: E402
from watchagent.storage import Storage, StoredReading  # noqa: E402

_HOURLY_FIELDS = (
    "temperature_2m",
    "apparent_temperature",
    "precipitation",
    "wind_speed_10m",
    "weather_code",
)


async def fetch_history(client: httpx.AsyncClient, city: City, past_days: int) -> list[Reading]:
    params = {
        "latitude": city.latitude,
        "longitude": city.longitude,
        "hourly": ",".join(_HOURLY_FIELDS),
        "past_days": past_days,
        "forecast_days": 1,
        "wind_speed_unit": "kmh",
        "timezone": "auto",
    }
    resp = await client.get(OPEN_METEO_URL, params=params)
    resp.raise_for_status()
    hourly = resp.json().get("hourly", {})
    times = hourly.get("time", [])
    now = datetime.now(timezone.utc)

    readings: list[Reading] = []
    for i, raw_time in enumerate(times):
        try:
            observed_at = _parse_timestamp(raw_time)
        except (ValueError, TypeError):
            continue
        # Skip future hours (forecast tail) — we only want real observations.
        if observed_at > now:
            continue
        values = [hourly.get(f, [])[i] for f in _HOURLY_FIELDS]
        if any(v is None for v in values):
            continue
        temp, apparent, precip, wind, code = values
        readings.append(
            Reading(
                city=city.name,
                observed_at=observed_at,
                temperature_c=float(temp),
                apparent_temperature_c=float(apparent),
                precipitation_mm=float(precip),
                wind_speed_kmh=float(wind),
                weather_code=int(code),
            )
        )
    return readings


async def evaluate(storage: Storage, reading: StoredReading, all_names: tuple[str, ...]) -> int:
    """Mirror of Poller._evaluate_events using only public functions."""
    history = await storage.readings_for_city(
        reading.city, limit=event_logic.ROLLING_WINDOW + 1
    )
    prior = [r for r in history if r.id != reading.id]

    latest_per_city = await storage.latest_reading_per_city()
    latest_per_city[reading.city] = reading

    candidates = event_logic.candidate_events(
        reading=reading,
        history=prior,
        latest_per_city=latest_per_city,
        all_city_names=all_names,
    )
    if not candidates:
        return 0

    last_seen = {
        (ev.city, ev.event_type): await storage.last_event_at(ev.city, ev.event_type)
        for ev in candidates
    }
    keepers = event_logic.apply_cooldown(candidates, last_seen)
    if not keepers:
        return 0
    await storage.store_events(keepers)
    return len(keepers)


async def run(db_path: str, past_days: int) -> None:
    storage = Storage(db_path)
    await storage.connect()
    all_names = tuple(c.name for c in CITIES)

    async with httpx.AsyncClient(timeout=30.0) as client:
        per_city = await asyncio.gather(
            *(fetch_history(client, c, past_days) for c in CITIES)
        )

    # Interleave chronologically so cross-city detectors see a realistic
    # timeline rather than all of one city then all of the next.
    timeline = sorted(
        (r for readings in per_city for r in readings),
        key=lambda r: r.observed_at,
    )

    stored_readings = 0
    fired_events = 0
    for reading in timeline:
        stored = await storage.store_reading(reading)
        if stored is None:
            continue  # dedup: already present
        stored_readings += 1
        fired_events += await evaluate(storage, stored, all_names)

    total_readings = await storage.count_readings()
    total_events = await storage.count_events()
    await storage.close()

    print(
        f"Backfill complete.\n"
        f"  fetched window : {past_days} days\n"
        f"  new readings   : {stored_readings}\n"
        f"  new events     : {fired_events}\n"
        f"  total readings : {total_readings}\n"
        f"  total events   : {total_events}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="./data/weather.db")
    parser.add_argument(
        "--past-days",
        type=int,
        default=31,
        help="How many days of real history to pull (Open-Meteo allows up to 92).",
    )
    args = parser.parse_args(argv)
    asyncio.run(run(args.db, args.past_days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
