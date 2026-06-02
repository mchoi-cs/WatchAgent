"""SQLite storage layer.

Pattern: Facade / Repository (see ``.cursor/rules/architecture-patterns.mdc``).
Only this module talks to ``aiosqlite``. Routes, the poller, and tests all
go through these functions — that constraint is what the ``db-access.mdc``
rule enforces.

Design notes:
- WAL mode is enabled so the API can read while the poller writes.
- ``UNIQUE(city, observed_at)`` does the heavy lifting for dedup; the
  ``store_reading`` helper still checks the rowid so we can report whether a
  row was actually inserted (the event detector only runs on new rows).
- All timestamps are stored as ISO-8601 strings in UTC. SQLite has no native
  ``timestamp`` type and string ordering on ISO-8601 is correct.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiosqlite

from .openmeteo import Reading

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    temperature_c REAL NOT NULL,
    apparent_temperature_c REAL NOT NULL,
    precipitation_mm REAL NOT NULL,
    wind_speed_kmh REAL NOT NULL,
    weather_code INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE(city, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_readings_city_observed
    ON readings(city, observed_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    value REAL,
    baseline REAL,
    reason TEXT NOT NULL,
    reading_id INTEGER,
    FOREIGN KEY(reading_id) REFERENCES readings(id)
);
CREATE INDEX IF NOT EXISTS idx_events_city_observed
    ON events(city, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_type_city_observed
    ON events(event_type, city, observed_at DESC);
"""


@dataclass(frozen=True)
class StoredReading:
    id: int
    city: str
    observed_at: datetime
    temperature_c: float
    apparent_temperature_c: float
    precipitation_mm: float
    wind_speed_kmh: float
    weather_code: int
    fetched_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "city": self.city,
            "observed_at": _iso(self.observed_at),
            "temperature_c": self.temperature_c,
            "apparent_temperature_c": self.apparent_temperature_c,
            "precipitation_mm": self.precipitation_mm,
            "wind_speed_kmh": self.wind_speed_kmh,
            "weather_code": self.weather_code,
            "fetched_at": _iso(self.fetched_at),
        }


@dataclass(frozen=True)
class StoredEvent:
    id: int
    city: str
    observed_at: datetime
    event_type: str
    severity: str
    value: float | None
    baseline: float | None
    reason: str
    reading_id: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "city": self.city,
            "observed_at": _iso(self.observed_at),
            "event_type": self.event_type,
            "severity": self.severity,
            "value": self.value,
            "baseline": self.baseline,
            "reason": self.reason,
            "reading_id": self.reading_id,
        }


@dataclass(frozen=True)
class NewEvent:
    """Event payload produced by detectors before it's written."""

    city: str
    observed_at: datetime
    event_type: str
    severity: str
    reason: str
    value: float | None = None
    baseline: float | None = None
    reading_id: int | None = None


class Storage:
    """Async SQLite wrapper. One instance per process; safe to share."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        parent = Path(self._db_path).expanduser().parent
        if str(parent) and parent != Path(""):
            os.makedirs(parent, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- writes ---------------------------------------------------------

    async def store_reading(self, reading: Reading) -> StoredReading | None:
        """Insert a reading. Returns the stored row, or ``None`` if the
        ``(city, observed_at)`` pair was already present (dedup hit)."""
        conn = self._require()
        fetched_at = datetime.now(timezone.utc)
        cursor = await conn.execute(
            """
            INSERT OR IGNORE INTO readings
                (city, observed_at, temperature_c, apparent_temperature_c,
                 precipitation_mm, wind_speed_kmh, weather_code, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reading.city,
                _iso(reading.observed_at),
                reading.temperature_c,
                reading.apparent_temperature_c,
                reading.precipitation_mm,
                reading.wind_speed_kmh,
                reading.weather_code,
                _iso(fetched_at),
            ),
        )
        await conn.commit()
        if cursor.rowcount == 0:
            return None
        row_id = cursor.lastrowid
        assert row_id is not None
        return StoredReading(
            id=row_id,
            city=reading.city,
            observed_at=reading.observed_at,
            temperature_c=reading.temperature_c,
            apparent_temperature_c=reading.apparent_temperature_c,
            precipitation_mm=reading.precipitation_mm,
            wind_speed_kmh=reading.wind_speed_kmh,
            weather_code=reading.weather_code,
            fetched_at=fetched_at,
        )

    async def store_events(self, events: Iterable[NewEvent]) -> list[StoredEvent]:
        conn = self._require()
        stored: list[StoredEvent] = []
        for ev in events:
            cursor = await conn.execute(
                """
                INSERT INTO events
                    (city, observed_at, event_type, severity, value, baseline,
                     reason, reading_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ev.city,
                    _iso(ev.observed_at),
                    ev.event_type,
                    ev.severity,
                    ev.value,
                    ev.baseline,
                    ev.reason,
                    ev.reading_id,
                ),
            )
            row_id = cursor.lastrowid
            assert row_id is not None
            stored.append(
                StoredEvent(
                    id=row_id,
                    city=ev.city,
                    observed_at=ev.observed_at,
                    event_type=ev.event_type,
                    severity=ev.severity,
                    value=ev.value,
                    baseline=ev.baseline,
                    reason=ev.reason,
                    reading_id=ev.reading_id,
                )
            )
        await conn.commit()
        return stored

    # ---- reads ----------------------------------------------------------

    async def count_readings(self) -> int:
        return await self._count("readings")

    async def count_events(self) -> int:
        return await self._count("events")

    async def recent_readings(
        self, city: str | None = None, limit: int = 50
    ) -> list[StoredReading]:
        conn = self._require()
        if city is None:
            sql = (
                "SELECT * FROM readings ORDER BY observed_at DESC, id DESC LIMIT ?"
            )
            params: Sequence[Any] = (limit,)
        else:
            sql = (
                "SELECT * FROM readings WHERE city = ? "
                "ORDER BY observed_at DESC, id DESC LIMIT ?"
            )
            params = (city, limit)
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_reading(r) for r in rows]

    async def recent_events(
        self, city: str | None = None, limit: int = 50
    ) -> list[StoredEvent]:
        conn = self._require()
        if city is None:
            sql = (
                "SELECT * FROM events ORDER BY observed_at DESC, id DESC LIMIT ?"
            )
            params: Sequence[Any] = (limit,)
        else:
            sql = (
                "SELECT * FROM events WHERE city = ? "
                "ORDER BY observed_at DESC, id DESC LIMIT ?"
            )
            params = (city, limit)
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_event(r) for r in rows]

    async def readings_for_city(
        self, city: str, limit: int
    ) -> list[StoredReading]:
        """Most-recent-first slice for a single city — used by the event
        detector to build a rolling window."""
        return await self.recent_readings(city=city, limit=limit)

    async def last_event_at(
        self, city: str, event_type: str
    ) -> datetime | None:
        conn = self._require()
        async with conn.execute(
            "SELECT observed_at FROM events WHERE city = ? AND event_type = ? "
            "ORDER BY observed_at DESC LIMIT 1",
            (city, event_type),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _parse_iso(row["observed_at"])

    async def latest_reading_per_city(self) -> dict[str, StoredReading]:
        """One reading per city — the most recent — used by the cross-city
        synchronisation detector."""
        conn = self._require()
        async with conn.execute(
            """
            SELECT r.* FROM readings r
            JOIN (
                SELECT city, MAX(observed_at) AS max_observed
                FROM readings
                GROUP BY city
            ) m ON m.city = r.city AND m.max_observed = r.observed_at
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return {r["city"]: _row_to_reading(r) for r in rows}

    # ---- internal -------------------------------------------------------

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Storage.connect() was not called")
        return self._conn

    async def _count(self, table: str) -> int:
        conn = self._require()
        async with conn.execute(f"SELECT COUNT(*) AS n FROM {table}") as cursor:
            row = await cursor.fetchone()
        return int(row["n"]) if row else 0


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_reading(row: aiosqlite.Row) -> StoredReading:
    return StoredReading(
        id=row["id"],
        city=row["city"],
        observed_at=_parse_iso(row["observed_at"]),
        temperature_c=row["temperature_c"],
        apparent_temperature_c=row["apparent_temperature_c"],
        precipitation_mm=row["precipitation_mm"],
        wind_speed_kmh=row["wind_speed_kmh"],
        weather_code=row["weather_code"],
        fetched_at=_parse_iso(row["fetched_at"]),
    )


def _row_to_event(row: aiosqlite.Row) -> StoredEvent:
    return StoredEvent(
        id=row["id"],
        city=row["city"],
        observed_at=_parse_iso(row["observed_at"]),
        event_type=row["event_type"],
        severity=row["severity"],
        value=row["value"],
        baseline=row["baseline"],
        reason=row["reason"],
        reading_id=row["reading_id"],
    )
