---
name: analyze-watchagent-data
description: |
  Query the WatchAgent SQLite database to answer questions about stored
  readings and notable events: per-attribute distributions, compound severe
  conditions (storm / freezing-rain / heat-stress / wind-chill), region-aware
  temperature baselines, per-city event counts, temperature trends,
  time-window summaries, synchronized-weather episodes, and event-type
  breakdowns. Use this whenever the user asks "what's in the data", wants to
  compare cities/periods/detectors, or wants to know whether attribute
  combinations co-occur. Output is structured JSON to stdout.
---

# analyze_data

## When to use

Use this skill any time the user wants to interrogate the collected data.
Examples:

- "Which city had the most events this week?"
- "What's the temperature trend in Vancouver?"
- "How many synchronized-weather episodes happened in the last 7 days?"
- "Break down events by type and city."
- "Give me a summary of the last 24 hours."
- "What's the full distribution of wind and precipitation in Ottawa?"
- "Did high wind and heavy rain ever co-occur (storm conditions)?"
- "Is the latest Vancouver reading hot *for Vancouver*, given the season?"

## Available weather attributes

Each reading stores five measured fields (see `storage.py`):

| Attribute                | Meaning                                                              |
| ------------------------ | -------------------------------------------------------------------- |
| `temperature_c`          | Actual air temperature.                                              |
| `apparent_temperature_c` | "Feels-like". The **gap** vs actual encodes humidity (heat index, summer) and wind chill (winter) — we don't store raw humidity, so this delta is the proxy. |
| `precipitation_mm`       | Precipitation rate.                                                  |
| `wind_speed_kmh`         | Wind speed.                                                          |
| `weather_code`           | WMO category (clear / rain / snow / freezing rain / thunderstorm…).  |

Single-attribute aggregates (`temperature-trend`) miss *combinations*; the
`attribute-summary` and `compound-conditions` questions exist to cover the
other four fields and their interactions.

## How to invoke

```bash
python .cursor/skills/analyze_data/analyze.py \
  --db ./data/weather.db \
  --question <question_id> \
  [--city <city>] [--hours <int>]
```

`--db` defaults to `./data/weather.db` (matches the host-side path that
`docker compose` writes to via the named volume; pass an explicit path if
you mounted somewhere else).

## Available questions

| `--question`          | What it returns                                                    |
| --------------------- | ------------------------------------------------------------------ |
| `event-counts`        | Per-city total event count, broken out by `event_type`.            |
| `temperature-trend`   | Per-city mean / min / max temperature over the window.             |
| `time-window`         | Total readings + events in the window, grouped by city.            |
| `synchronized`        | List of `synchronized_weather` events with the WMO code involved.  |
| `event-types`         | Total count per event type across all cities.                      |
| `dedup-check`         | Verifies `(city, observed_at)` uniqueness; flags any duplicates.   |
| `attribute-summary`   | Per-city distribution (n / mean / min / max / p10 / p50 / p90) for **all four** numeric attributes, plus a decoded weather-code breakdown. |
| `compound-conditions` | Scans readings for co-occurring severe attributes and counts them per city, with examples. See screens below. |
| `regional-baseline`   | Hybrid per-city temperature baseline and how the latest reading scores against it (region- and season-aware). |

`--city` filters to a single city when set. `--hours` (default 168 = 7d)
sets the window for time-bounded questions (now including the three new ones).

### `compound-conditions` screens

Each screen inspects **two or more attributes together** — the thing
single-attribute aggregates can't see:

| Screen               | Fires when…                                                          |
| -------------------- | ------------------------------------------------------------------- |
| `storm`              | `wind ≥ 35 km/h` **and** `precip ≥ 2 mm`, or a thunderstorm WMO code (95/96/99). |
| `freezing_rain_risk` | `temp ≤ 1 °C` **and** `precip ≥ 0.2 mm`, or a freezing-rain code (66/67). |
| `heat_stress`        | `temp ≥ 28 °C` **and** `apparent − actual ≥ 3 °C` (humidity load).  |
| `wind_chill`         | `temp ≤ 0 °C` **and** `actual − apparent ≥ 5 °C` (wind bites).      |

Thresholds are constants at the top of `analyze.py`. They are the skill's
**exploratory** lens; once the Stage-2 detectors land in `events.py`, that
module is the canonical firing rule and the two are kept in step.

### `regional-baseline` — why it's hybrid

`25 °C` is unremarkable in Vancouver but extreme in Ottawa in winter, so a
flat global threshold is wrong. For each city the question:

1. Computes a **data-driven** mean/stddev from stored readings in the window.
2. Loads a **static seasonal prior** (per-city monthly normals from
   `src/watchagent/climate_normals.json`, which encodes that continental
   Ottawa swings harder than maritime Vancouver).
3. Uses the data-driven baseline when there are at least
   `MIN_HISTORY_FOR_BASELINE` (12) readings, otherwise falls back to the
   prior — and reports which one it used (`baseline_used`).
4. Scores the latest reading as a z-score against the chosen baseline and
   labels it `normal` / `warning` / `critical`.

This means severity is relative to *that city and season*, and it degrades
gracefully on a fresh database with little history.

## Output

JSON object with these top-level keys:

```json
{
  "question": "<question id>",
  "window_hours": <int or null>,
  "city": "<city or null>",
  "generated_at": "<iso8601 utc>",
  "result": { ... }
}
```

The shape of `result` depends on the question; see `analyze.py` for the
canonical schemas (one helper function per question).

## Constraints

- Read-only. The skill opens the SQLite file with `mode=ro` and never
  writes. This is the only place outside `storage.py` that is allowed to
  open its own sqlite connection — see `.cursor/rules/db-access.mdc`.
- No network calls. The skill works entirely from the local DB.
- Exits non-zero on schema mismatch or missing DB file with an error JSON
  on stderr.
