---
name: analyze-watchagent-data
description: |
  Query the WatchAgent SQLite database to answer questions about stored
  readings and notable events: per-city event counts, temperature trends,
  time-window summaries, synchronized-weather episodes, and event-type
  breakdowns. Use this whenever the user asks "what's in the data" or
  wants to compare cities, periods, or detectors. Output is structured
  JSON to stdout so it can be piped into other tools.
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

| `--question`         | What it returns                                                    |
| -------------------- | ------------------------------------------------------------------ |
| `event-counts`       | Per-city total event count, broken out by `event_type`.            |
| `temperature-trend`  | Per-city mean / min / max temperature over the window.             |
| `time-window`        | Total readings + events in the window, grouped by city.            |
| `synchronized`       | List of `synchronized_weather` events with the WMO code involved.  |
| `event-types`        | Total count per event type across all cities.                      |
| `dedup-check`        | Verifies `(city, observed_at)` uniqueness; flags any duplicates.   |

`--city` filters to a single city when set. `--hours` (default 168 = 7d)
sets the window for time-bounded questions.

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
