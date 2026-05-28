#!/usr/bin/env python3
"""Replay recent readings through the current event detectors.

Run from the repo root. Nothing is written to the DB.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))

from watchagent import events as event_logic  # noqa: E402
from watchagent.config import CITIES  # noqa: E402
from watchagent.storage import NewEvent, StoredReading  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay readings through detectors.")
    parser.add_argument("--db", default="./data/weather.db")
    parser.add_argument("--per-city", type=int, default=200)
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        json.dump({"error": f"DB not found at {db_path}"}, sys.stderr)
        sys.stderr.write("\n")
        return 2

    city_names = tuple(c.name for c in CITIES)
    readings_by_city: dict[str, list[StoredReading]] = {c: [] for c in city_names}

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for city in city_names:
            rows = conn.execute(
                "SELECT * FROM readings WHERE city = ? "
                "ORDER BY observed_at ASC LIMIT ?",
                (city, args.per_city),
            ).fetchall()
            readings_by_city[city] = [_row_to_reading(r) for r in rows]
    finally:
        conn.close()

    timeline = sorted(
        (r for rs in readings_by_city.values() for r in rs),
        key=lambda r: r.observed_at,
    )

    history_per_city: dict[str, list[StoredReading]] = {c: [] for c in city_names}
    latest_per_city: dict[str, StoredReading] = {}
    last_seen: dict[tuple[str, str], datetime] = {}

    by_type: dict[str, int] = defaultdict(int)
    by_city: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    sample: list[dict[str, object]] = []

    for reading in timeline:
        history = list(reversed(history_per_city[reading.city]))
        latest_per_city[reading.city] = reading
        candidates = event_logic.candidate_events(
            reading=reading,
            history=history,
            latest_per_city=dict(latest_per_city),
            all_city_names=city_names,
        )
        kept = event_logic.apply_cooldown(
            candidates, {k: v for k, v in last_seen.items()}
        )
        for ev in kept:
            by_type[ev.event_type] += 1
            by_city[ev.city][ev.event_type] += 1
            last_seen[(ev.city, ev.event_type)] = ev.observed_at
            if len(sample) < 20:
                sample.append(
                    {
                        "city": ev.city,
                        "event_type": ev.event_type,
                        "observed_at": ev.observed_at.isoformat(),
                        "reason": ev.reason,
                    }
                )
        history_per_city[reading.city].append(reading)

    out = {
        "replayed_readings": sum(len(v) for v in readings_by_city.values()),
        "would_fire": sum(by_type.values()),
        "by_type": dict(by_type),
        "by_city": {k: dict(v) for k, v in by_city.items()},
        "sample": sample,
    }
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _row_to_reading(row: sqlite3.Row) -> StoredReading:
    return StoredReading(
        id=row["id"],
        city=row["city"],
        observed_at=_iso(row["observed_at"]),
        temperature_c=row["temperature_c"],
        apparent_temperature_c=row["apparent_temperature_c"],
        precipitation_mm=row["precipitation_mm"],
        wind_speed_kmh=row["wind_speed_kmh"],
        weather_code=row["weather_code"],
        fetched_at=_iso(row["fetched_at"]),
    )


def _iso(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_ = NewEvent  # keep import for downstream type-checker users

if __name__ == "__main__":
    raise SystemExit(main())
