#!/usr/bin/env python3
"""Read-only analysis skill for the WatchAgent SQLite database.

Usage:
    python analyze.py --question <id> [--db PATH] [--city CITY] [--hours N]

Outputs a single JSON object to stdout. See SKILL.md for the question
catalog and result schemas.

Implementation notes:
- Opens the DB read-only via the ``file:...?mode=ro`` URI so a running
  poller can't be impacted.
- Uses only the stdlib (``sqlite3``, ``json``, ``argparse``, ``datetime``)
  so the skill works without installing the watchagent package.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

QUESTIONS: dict[str, str] = {
    "event-counts": "Per-city total event count, broken out by event_type.",
    "temperature-trend": "Per-city mean/min/max temperature over the window.",
    "time-window": "Total readings + events in the window, grouped by city.",
    "synchronized": "synchronized_weather events with the WMO code involved.",
    "event-types": "Total count per event type across all cities.",
    "dedup-check": "Verifies (city, observed_at) uniqueness in readings.",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyse the WatchAgent SQLite database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Available questions:\n  "
        + "\n  ".join(f"{k}: {v}" for k, v in QUESTIONS.items()),
    )
    parser.add_argument("--db", default="./data/weather.db", help="Path to SQLite DB.")
    parser.add_argument(
        "--question",
        required=True,
        choices=sorted(QUESTIONS),
        help="Which canned question to answer.",
    )
    parser.add_argument("--city", default=None, help="Filter to a single city.")
    parser.add_argument(
        "--hours",
        type=int,
        default=168,
        help="Window in hours for time-bounded questions (default: 168 = 7d).",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        _die(f"DB not found at {db_path}")

    handler = _HANDLERS[args.question]
    try:
        with _connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            result = handler(conn, city=args.city, hours=args.hours)
    except sqlite3.OperationalError as exc:
        _die(f"sqlite error: {exc}")

    envelope = {
        "question": args.question,
        "window_hours": args.hours if args.question in _WINDOWED else None,
        "city": args.city,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "result": result,
    }
    json.dump(envelope, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


# ---- handlers --------------------------------------------------------------


Handler = Callable[..., Any]


def _q_event_counts(conn: sqlite3.Connection, city: str | None, **_: Any) -> dict[str, Any]:
    sql = "SELECT city, event_type, COUNT(*) AS n FROM events"
    params: list[Any] = []
    if city:
        sql += " WHERE city = ?"
        params.append(city)
    sql += " GROUP BY city, event_type ORDER BY city, event_type"
    by_city: dict[str, dict[str, int]] = defaultdict(dict)
    totals: dict[str, int] = defaultdict(int)
    for row in conn.execute(sql, params):
        by_city[row["city"]][row["event_type"]] = row["n"]
        totals[row["city"]] += row["n"]
    return {
        "by_city": {k: dict(v) for k, v in by_city.items()},
        "totals": dict(totals),
    }


def _q_temperature_trend(
    conn: sqlite3.Connection, city: str | None, hours: int, **_: Any
) -> dict[str, Any]:
    since = _since(hours)
    sql = (
        "SELECT city, "
        "  AVG(temperature_c) AS mean, "
        "  MIN(temperature_c) AS min, "
        "  MAX(temperature_c) AS max, "
        "  COUNT(*) AS n "
        "FROM readings WHERE observed_at >= ?"
    )
    params: list[Any] = [since]
    if city:
        sql += " AND city = ?"
        params.append(city)
    sql += " GROUP BY city ORDER BY city"
    out: dict[str, Any] = {}
    for row in conn.execute(sql, params):
        out[row["city"]] = {
            "mean": _round(row["mean"]),
            "min": _round(row["min"]),
            "max": _round(row["max"]),
            "readings": row["n"],
        }
    return out


def _q_time_window(
    conn: sqlite3.Connection, city: str | None, hours: int, **_: Any
) -> dict[str, Any]:
    since = _since(hours)
    by_city: dict[str, dict[str, int]] = defaultdict(lambda: {"readings": 0, "events": 0})
    where_city = " AND city = ?" if city else ""
    params: list[Any] = [since] + ([city] if city else [])
    for row in conn.execute(
        f"SELECT city, COUNT(*) AS n FROM readings WHERE observed_at >= ?{where_city} GROUP BY city",
        params,
    ):
        by_city[row["city"]]["readings"] = row["n"]
    for row in conn.execute(
        f"SELECT city, COUNT(*) AS n FROM events WHERE observed_at >= ?{where_city} GROUP BY city",
        params,
    ):
        by_city[row["city"]]["events"] = row["n"]
    return {"by_city": {k: dict(v) for k, v in by_city.items()}}


def _q_synchronized(
    conn: sqlite3.Connection, city: str | None, hours: int, **_: Any
) -> dict[str, Any]:
    since = _since(hours)
    sql = (
        "SELECT id, city, observed_at, value, reason FROM events "
        "WHERE event_type = 'synchronized_weather' AND observed_at >= ?"
    )
    params: list[Any] = [since]
    if city:
        sql += " AND city = ?"
        params.append(city)
    sql += " ORDER BY observed_at DESC"
    rows = [
        {
            "id": r["id"],
            "city": r["city"],
            "observed_at": r["observed_at"],
            "wmo_code": int(r["value"]) if r["value"] is not None else None,
            "reason": r["reason"],
        }
        for r in conn.execute(sql, params)
    ]
    return {"count": len(rows), "events": rows}


def _q_event_types(conn: sqlite3.Connection, **_: Any) -> dict[str, int]:
    return {
        row["event_type"]: row["n"]
        for row in conn.execute(
            "SELECT event_type, COUNT(*) AS n FROM events GROUP BY event_type ORDER BY n DESC"
        )
    }


def _q_dedup_check(conn: sqlite3.Connection, **_: Any) -> dict[str, Any]:
    duplicates = [
        {"city": r["city"], "observed_at": r["observed_at"], "count": r["n"]}
        for r in conn.execute(
            "SELECT city, observed_at, COUNT(*) AS n FROM readings "
            "GROUP BY city, observed_at HAVING COUNT(*) > 1"
        )
    ]
    total = conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"]
    distinct = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT 1 FROM readings GROUP BY city, observed_at)"
    ).fetchone()["n"]
    return {
        "total_readings": total,
        "distinct_city_observed_at": distinct,
        "duplicate_groups": duplicates,
        "ok": len(duplicates) == 0,
    }


_HANDLERS: dict[str, Handler] = {
    "event-counts": _q_event_counts,
    "temperature-trend": _q_temperature_trend,
    "time-window": _q_time_window,
    "synchronized": _q_synchronized,
    "event-types": _q_event_types,
    "dedup-check": _q_dedup_check,
}

_WINDOWED: frozenset[str] = frozenset(
    {"temperature-trend", "time-window", "synchronized"}
)


# ---- helpers ---------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _since(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


def _die(message: str) -> int:
    json.dump({"error": message}, sys.stderr)
    sys.stderr.write("\n")
    sys.exit(2)


if __name__ == "__main__":
    raise SystemExit(main())
