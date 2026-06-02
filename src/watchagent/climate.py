"""Per-city seasonal climate priors.

Loads the static normals in ``climate_normals.json`` (same package directory)
once at import and exposes a seasonal baseline ``(mean, stddev)`` per
city/month. Used by the region-aware ``heat_stress`` detector so that "hot" is
judged relative to a city's season rather than a flat global threshold, and
mirrored read-only by the ``analyze_data`` skill.

Loading is tolerant: if the data file is missing or malformed the table is
empty and :func:`seasonal_baseline` returns ``None``, so callers fall back to
region-independent behaviour rather than crashing. After import the lookups are
pure dict access, which keeps the detectors in ``events.py`` side-effect free.
"""

from __future__ import annotations

import json
from pathlib import Path

_DATA_FILE = Path(__file__).with_name("climate_normals.json")


def _load() -> dict:
    try:
        with _DATA_FILE.open(encoding="utf-8") as fh:
            return json.load(fh).get("cities", {})
    except (OSError, ValueError):
        return {}


_NORMALS: dict = _load()


def seasonal_baseline(city: str, month: int) -> tuple[float, float] | None:
    """Return ``(mean_c, stddev_c)`` for ``city`` in ``month`` (1-12), or
    ``None`` when we have no prior for that city/month or a zero spread."""
    entry = _NORMALS.get(city)
    if not entry:
        return None
    means = entry.get("monthly_mean_c") or []
    idx = month - 1
    if not (0 <= idx < len(means)):
        return None
    stddev = entry.get("daily_stddev_c")
    if not stddev:
        return None
    return float(means[idx]), float(stddev)
