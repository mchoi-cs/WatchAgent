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
import statistics
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
    "attribute-summary": (
        "Per-city distribution (mean/min/max/p10/p50/p90) for every measured "
        "attribute plus a decoded weather-code breakdown."
    ),
    "compound-conditions": (
        "Scans readings for co-occurring severe attributes (storm, "
        "freezing-rain risk, humidity heat stress, wind chill) per city."
    ),
    "regional-baseline": (
        "Hybrid per-city temperature baseline (stored history if available, "
        "else seasonal climate prior) and how the latest reading compares."
    ),
}

# ---- compound-condition screens -------------------------------------------
# Physical thresholds for spotting attribute *combinations*. These are the
# skill's exploratory lens; once the Stage-2 detectors land in
# src/watchagent/events.py, that module is the canonical firing rule and these
# should be kept in step with it.
STORM_WIND_KMH = 35.0          # sustained wind that, with rain, reads as a storm
STORM_PRECIP_MM = 2.0
FREEZING_TEMP_C = 1.0          # at/below this *with* precip => freezing-rain risk
FREEZING_PRECIP_MM = 0.2
HEAT_TEMP_C = 28.0             # hot enough that humidity load matters
HEAT_APPARENT_GAP_C = 3.0     # apparent >> actual => humidity-driven heat stress
WIND_CHILL_TEMP_C = 0.0
WIND_CHILL_GAP_C = 5.0         # actual - apparent => wind chill bites

# Thunderstorm / freezing-rain WMO codes, mirrored from events.py for decoding.
_STORM_WMO = frozenset({95, 96, 99})
_FREEZING_WMO = frozenset({66, 67})

# Need at least this many stored readings for a trustworthy data-driven
# baseline; below it we fall back to the static seasonal prior.
MIN_HISTORY_FOR_BASELINE = 12
EXAMPLES_PER_CONDITION = 3


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


_NUMERIC_ATTRS = (
    "temperature_c",
    "apparent_temperature_c",
    "precipitation_mm",
    "wind_speed_kmh",
)


def _q_attribute_summary(
    conn: sqlite3.Connection, city: str | None, hours: int, **_: Any
) -> dict[str, Any]:
    """Distribution of every measured attribute, per city, plus a decoded
    weather-code breakdown. Answers 'what is actually in the data'."""
    rows = _fetch_readings(conn, city, hours)
    by_city: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_city[r["city"]].append(r)

    out: dict[str, Any] = {}
    for c, city_rows in sorted(by_city.items()):
        attrs: dict[str, Any] = {}
        for attr in _NUMERIC_ATTRS:
            values = sorted(float(r[attr]) for r in city_rows)
            attrs[attr] = _distribution(values)
        codes: dict[str, int] = defaultdict(int)
        for r in city_rows:
            codes[_wmo_category(int(r["weather_code"]))] += 1
        out[c] = {
            "readings": len(city_rows),
            "attributes": attrs,
            "weather": dict(sorted(codes.items(), key=lambda kv: -kv[1])),
        }
    return out


def _q_compound_conditions(
    conn: sqlite3.Connection, city: str | None, hours: int, **_: Any
) -> dict[str, Any]:
    """Scan readings for co-occurring severe attributes. Each screen looks at
    *two or more* attributes together, which single-attribute aggregates miss.
    """
    rows = _fetch_readings(conn, city, hours)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    examples: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for r in rows:
        c = r["city"]
        temp = float(r["temperature_c"])
        apparent = float(r["apparent_temperature_c"])
        precip = float(r["precipitation_mm"])
        wind = float(r["wind_speed_kmh"])
        code = int(r["weather_code"])
        for name, hit in _screen(temp, apparent, precip, wind, code).items():
            if not hit:
                continue
            counts[c][name] += 1
            bucket = examples[c][name]
            if len(bucket) < EXAMPLES_PER_CONDITION:
                bucket.append(
                    {
                        "observed_at": r["observed_at"],
                        "temperature_c": temp,
                        "apparent_temperature_c": apparent,
                        "precipitation_mm": precip,
                        "wind_speed_kmh": wind,
                        "weather_code": code,
                    }
                )

    cities = sorted(set(counts) | set(examples))
    return {
        "screens": {
            "storm": f"wind>={STORM_WIND_KMH}km/h AND precip>={STORM_PRECIP_MM}mm (or thunderstorm code)",
            "freezing_rain_risk": f"temp<={FREEZING_TEMP_C}C AND precip>={FREEZING_PRECIP_MM}mm (or freezing-rain code)",
            "heat_stress": f"temp>={HEAT_TEMP_C}C AND apparent-temp gap>={HEAT_APPARENT_GAP_C}C",
            "wind_chill": f"temp<={WIND_CHILL_TEMP_C}C AND actual-apparent gap>={WIND_CHILL_GAP_C}C",
        },
        "by_city": {
            c: {
                "counts": dict(counts[c]),
                "examples": {k: v for k, v in examples[c].items()},
            }
            for c in cities
        },
    }


def _q_regional_baseline(
    conn: sqlite3.Connection, city: str | None, hours: int, **_: Any
) -> dict[str, Any]:
    """Hybrid baseline: prefer a data-driven mean/stddev from stored history,
    fall back to the static seasonal prior when history is thin. Then score the
    latest reading against the chosen baseline so 'hot' is relative to *this*
    city and season (25C means very different things in Ottawa vs Vancouver).
    """
    normals = _load_normals()
    rows = _fetch_readings(conn, city, hours)
    by_city: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_city[r["city"]].append(r)

    cities = sorted(by_city) if by_city else sorted(normals)
    if city:
        cities = [city]

    out: dict[str, Any] = {}
    for c in cities:
        city_rows = by_city.get(c, [])
        temps = [float(r["temperature_c"]) for r in city_rows]
        latest = city_rows[0] if city_rows else None  # rows are newest-first
        month = _month_of(latest["observed_at"]) if latest else _utc_month()

        prior = _seasonal_prior(normals, c, month)
        history = _history_baseline(temps)

        if history and history["n"] >= MIN_HISTORY_FOR_BASELINE:
            source, mean, stddev = "history", history["mean"], history["stddev"]
        elif prior:
            source, mean, stddev = "prior", prior["mean"], prior["stddev"]
        else:
            source, mean, stddev = "none", None, None

        latest_eval: dict[str, Any] | None = None
        if latest is not None and mean is not None and stddev:
            value = float(latest["temperature_c"])
            z = (value - mean) / stddev
            latest_eval = {
                "observed_at": latest["observed_at"],
                "temperature_c": value,
                "z_vs_baseline": _round(z),
                "severity": _regional_severity(z),
            }

        out[c] = {
            "month": month + 1,  # human 1-12
            "baseline_used": source,
            "baseline_mean_c": _round(mean),
            "baseline_stddev_c": _round(stddev),
            "history": history,
            "seasonal_prior": prior,
            "latest": latest_eval,
        }
    return out


_HANDLERS: dict[str, Handler] = {
    "event-counts": _q_event_counts,
    "temperature-trend": _q_temperature_trend,
    "time-window": _q_time_window,
    "synchronized": _q_synchronized,
    "event-types": _q_event_types,
    "dedup-check": _q_dedup_check,
    "attribute-summary": _q_attribute_summary,
    "compound-conditions": _q_compound_conditions,
    "regional-baseline": _q_regional_baseline,
}

_WINDOWED: frozenset[str] = frozenset(
    {
        "temperature-trend",
        "time-window",
        "synchronized",
        "attribute-summary",
        "compound-conditions",
        "regional-baseline",
    }
)


# ---- helpers ---------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _since(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


def _fetch_readings(
    conn: sqlite3.Connection, city: str | None, hours: int
) -> list[sqlite3.Row]:
    """Newest-first readings in the window, optionally for one city."""
    sql = (
        "SELECT city, observed_at, temperature_c, apparent_temperature_c, "
        "precipitation_mm, wind_speed_kmh, weather_code "
        "FROM readings WHERE observed_at >= ?"
    )
    params: list[Any] = [_since(hours)]
    if city:
        sql += " AND city = ?"
        params.append(city)
    sql += " ORDER BY observed_at DESC, id DESC"
    return list(conn.execute(sql, params))


def _distribution(sorted_values: list[float]) -> dict[str, Any]:
    if not sorted_values:
        return {"n": 0, "mean": None, "min": None, "max": None,
                "p10": None, "p50": None, "p90": None}
    return {
        "n": len(sorted_values),
        "mean": _round(statistics.fmean(sorted_values)),
        "min": _round(sorted_values[0]),
        "max": _round(sorted_values[-1]),
        "p10": _round(_percentile(sorted_values, 0.10)),
        "p50": _round(_percentile(sorted_values, 0.50)),
        "p90": _round(_percentile(sorted_values, 0.90)),
    }


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    frac = pos - lo
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _wmo_category(code: int) -> str:
    """Coarse bucket for a WMO weather code (full labels live in events.py)."""
    if code in _STORM_WMO:
        return "thunderstorm"
    if code in _FREEZING_WMO:
        return "freezing_rain"
    if code in (0, 1):
        return "clear"
    if code in (2, 3):
        return "cloud"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 67:
        return "rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if 80 <= code <= 82:
        return "rain_showers"
    return f"other_{code}"


def _screen(
    temp: float, apparent: float, precip: float, wind: float, code: int
) -> dict[str, bool]:
    """Evaluate the compound screens for a single reading."""
    return {
        "storm": (wind >= STORM_WIND_KMH and precip >= STORM_PRECIP_MM)
        or code in _STORM_WMO,
        "freezing_rain_risk": (
            temp <= FREEZING_TEMP_C and precip >= FREEZING_PRECIP_MM
        )
        or code in _FREEZING_WMO,
        "heat_stress": temp >= HEAT_TEMP_C
        and (apparent - temp) >= HEAT_APPARENT_GAP_C,
        "wind_chill": temp <= WIND_CHILL_TEMP_C
        and (temp - apparent) >= WIND_CHILL_GAP_C,
    }


def _load_normals() -> dict[str, Any]:
    """Load the shared per-city climate priors. Canonical copy lives at
    src/watchagent/climate_normals.json (repo root = this file's parents[3])."""
    path = Path(__file__).resolve().parents[3] / "src" / "watchagent" / "climate_normals.json"
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh).get("cities", {})
    except (OSError, ValueError):
        return {}


def _seasonal_prior(
    normals: dict[str, Any], city: str, month: int
) -> dict[str, Any] | None:
    entry = normals.get(city)
    if not entry:
        return None
    means = entry.get("monthly_mean_c") or []
    if month >= len(means):
        return None
    return {
        "mean": _round(means[month]),
        "stddev": _round(entry.get("daily_stddev_c")),
        "month": month + 1,
    }


def _history_baseline(temps: list[float]) -> dict[str, Any] | None:
    if not temps:
        return None
    mean = statistics.fmean(temps)
    stddev = statistics.pstdev(temps) if len(temps) > 1 else 0.0
    return {"n": len(temps), "mean": _round(mean), "stddev": _round(stddev)}


def _regional_severity(z: float) -> str:
    az = abs(z)
    if az >= 3.0:
        return "critical"
    if az >= 2.0:
        return "warning"
    return "normal"


def _month_of(observed_at: str) -> int:
    try:
        return datetime.fromisoformat(observed_at).month - 1
    except ValueError:
        return _utc_month()


def _utc_month() -> int:
    return datetime.now(timezone.utc).month - 1


def _die(message: str) -> int:
    json.dump({"error": message}, sys.stderr)
    sys.stderr.write("\n")
    sys.exit(2)


if __name__ == "__main__":
    raise SystemExit(main())
