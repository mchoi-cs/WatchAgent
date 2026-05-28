---
name: replay-events
description: |
  Replay the most recent N stored readings through the current event
  detectors and report which events would fire — without writing
  anything to the database. Use when tuning thresholds in
  ``src/watchagent/events.py`` to see the effect on real captured data
  before committing.
---

# replay_events

## When to use

You changed a threshold in `events.py` (lowered `Z_THRESHOLD`, raised
`WIND_SPIKE_DELTA_KMH`, added a new detector, etc.) and want to know
whether the change would flood the event stream or silence it entirely.

This skill loads the last N readings per city from the live DB and pipes
them back through the same detector code path that the poller uses,
respecting the same per-type cooldown. Nothing is written.

## How to invoke

```bash
python .cursor/skills/replay_events/replay.py \
  --db ./data/weather.db \
  --per-city 200
```

`--per-city` (default 200) controls how many recent readings per city to
replay.

## Output

JSON object with:

```json
{
  "replayed_readings": <int>,
  "would_fire": <int>,
  "by_type": { "<event_type>": <int>, ... },
  "by_city": { "<city>": { "<event_type>": <int> } },
  "sample": [ { "city": "...", "event_type": "...", "observed_at": "...", "reason": "..." }, ... up to 20 ]
}
```

Compare `would_fire` and `by_type` before vs. after your threshold change
to decide whether the new value is reasonable.

## Constraints

- Read-only access to the database.
- Imports `watchagent.events` and `watchagent.storage` directly, so it
  must be run from the repo root with the package importable
  (`pip install -e .` if you haven't already, or use the same venv used
  by tests).
