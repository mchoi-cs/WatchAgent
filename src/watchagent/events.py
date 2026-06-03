"""Notable-event detection.

Three families of detectors, deliberately chosen because each addresses a
different kind of signal:

1. **Per-city contextual anomaly** (``temperature_anomaly``).
   Temperature drifts. ``25 C`` in Vancouver in February is dramatic; the
   same value in Ottawa in July is unremarkable. We keep a rolling window of
   each city's most recent readings, and fire when the new reading is more
   than ``Z_THRESHOLD`` standard deviations from that window's mean *and*
   the absolute delta is at least ``MIN_ABS_DELTA_C`` (so a flat window
   doesn't generate noise on a tiny variation that happens to be a large
   number of stddevs).

2. **Rate-of-change spikes** (``wind_spike``, ``precip_onset``).
   Wind and precipitation don't drift, they spike. Using a rolling stddev on
   bursty fields produces noise (a single gust pollutes the baseline for
   hours). Instead we compare the new reading to the previous one and look
   for a sudden change.

3. **Categorical / cross-city** (``severe_weather``, ``synchronized_weather``).
   Some WMO codes are inherently notable regardless of context
   (thunderstorms, freezing rain, heavy snow). And when all three cities
   share the same non-trivial weather code, that coordination itself is the
   story.

4. **Compound / region-aware** (``storm``, ``freezing_rain``, ``heat_warning``,
   ``cold_warning``). These read *combinations* of attributes — a single
   reading can satisfy several at once, which is the point: cold + rain is
   freezing rain, wind + rain is a storm. ``heat_warning`` and ``cold_warning``
   are also region-aware: "hot" and "cold" are judged against each city's
   seasonal climate normal (see :mod:`climate`), so the same temperature can be
   newsworthy in maritime Vancouver yet unremarkable in continental Ottawa.
   Humidity (heat) and wind chill / freezing precipitation (cold) act as
   *severity* modifiers, escalating a ``warning`` to ``critical``.

Cooldowns: each ``(city, event_type)`` pair has its own cooldown so a
sustained heat wave fires one onset event rather than 24. The cooldown is
checked against ``observed_at``, not wall-clock time.

This module is pure: detectors take inputs and return :class:`NewEvent`
instances. The poller writes them via :mod:`storage`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Sequence

from . import climate
from .storage import NewEvent, StoredReading

# ---- thresholds (single source of truth — README quotes these) -------------

ROLLING_WINDOW = 24                # readings (~24 hours at hourly Open-Meteo)
MIN_WINDOW_FOR_BASELINE = 6        # need at least this many to trust stddev
Z_THRESHOLD = 2.0
MIN_ABS_DELTA_C = 5.0

WIND_SPIKE_DELTA_KMH = 25.0
WIND_SPIKE_MIN_KMH = 30.0          # ignore spikes from 0->5 etc.

PRECIP_ONSET_BASELINE_MM = 0.2
PRECIP_ONSET_TRIGGER_MM = 2.0

# ---- compound / region-aware thresholds ----
# These detectors look at *combinations* of attributes (and, for the heat /
# cold warnings, at the city's seasonal climate) rather than a single field.
STORM_WIND_KMH = 35.0              # sustained wind that, with rain, reads as a storm
STORM_PRECIP_MM = 2.0

FREEZING_TEMP_C = 1.0              # at/below this *with* precip => freezing-rain risk
FREEZING_PRECIP_MM = 0.2

HEAT_ABS_MIN_C = 20.0            # floor so a heat warning implies genuine warmth
HEAT_APPARENT_GAP_C = 3.0       # apparent >> actual => humidity load (escalates severity)
HEAT_SEASONAL_Z = 1.0           # ...and hot for THIS city's season (region-aware)

COLD_ABS_MAX_C = 5.0            # ceiling so a cold warning implies genuine chill
COLD_WINDCHILL_GAP_C = 3.0      # actual >> apparent => wind chill (escalates severity)
COLD_SEASONAL_Z = 1.0           # ...and cold for THIS city's season (region-aware)

SEVERE_WEATHER_CODES: frozenset[int] = frozenset(
    {
        65,  # rain: heavy intensity
        66, 67,  # freezing rain
        75,  # snow fall: heavy intensity
        82,  # rain showers: violent
        95,  # thunderstorm: slight or moderate
        96, 99,  # thunderstorm with hail
    }
)

# Trivial codes we never trigger "synchronized" on — they're the default for
# half the year and would dominate the event stream.
TRIVIAL_WEATHER_CODES: frozenset[int] = frozenset({0, 1, 2, 3})

COOLDOWN: dict[str, timedelta] = {
    "temperature_anomaly": timedelta(hours=12),
    "wind_spike": timedelta(hours=6),
    "precip_onset": timedelta(hours=6),
    "severe_weather": timedelta(hours=6),
    "synchronized_weather": timedelta(hours=12),
    "storm": timedelta(hours=6),
    "freezing_rain": timedelta(hours=6),
    "heat_warning": timedelta(hours=12),
    "cold_warning": timedelta(hours=12),
}


# ---- detectors -------------------------------------------------------------


def detect_temperature_anomaly(
    reading: StoredReading, history: Sequence[StoredReading]
) -> NewEvent | None:
    """``history`` is the recent past for this city, excluding ``reading``.

    We use ``temperature_c`` (not apparent) as the signal because apparent
    temperature combines wind + humidity and is more volatile.
    """
    prior = [h.temperature_c for h in history[:ROLLING_WINDOW]]
    if len(prior) < MIN_WINDOW_FOR_BASELINE:
        return None
    mean = sum(prior) / len(prior)
    variance = sum((x - mean) ** 2 for x in prior) / len(prior)
    stddev = math.sqrt(variance)
    if stddev == 0:
        return None
    delta = reading.temperature_c - mean
    z = delta / stddev
    if abs(z) < Z_THRESHOLD or abs(delta) < MIN_ABS_DELTA_C:
        return None
    direction = "hot" if delta > 0 else "cold"
    return NewEvent(
        city=reading.city,
        observed_at=reading.observed_at,
        event_type="temperature_anomaly",
        severity="warning",
        value=reading.temperature_c,
        baseline=round(mean, 2),
        reading_id=reading.id,
        reason=(
            f"Temperature {reading.temperature_c:.1f}C is {direction} for "
            f"{reading.city}: {delta:+.1f}C from {len(prior)}-reading mean "
            f"({mean:.1f}C, z={z:+.2f})."
        ),
    )


def detect_wind_spike(
    reading: StoredReading, history: Sequence[StoredReading]
) -> NewEvent | None:
    if not history:
        return None
    previous = history[0]
    delta = reading.wind_speed_kmh - previous.wind_speed_kmh
    if delta < WIND_SPIKE_DELTA_KMH or reading.wind_speed_kmh < WIND_SPIKE_MIN_KMH:
        return None
    return NewEvent(
        city=reading.city,
        observed_at=reading.observed_at,
        event_type="wind_spike",
        severity="warning",
        value=reading.wind_speed_kmh,
        baseline=previous.wind_speed_kmh,
        reading_id=reading.id,
        reason=(
            f"Wind jumped from {previous.wind_speed_kmh:.0f} to "
            f"{reading.wind_speed_kmh:.0f} km/h in {reading.city} "
            f"(+{delta:.0f} km/h)."
        ),
    )


def detect_precip_onset(
    reading: StoredReading, history: Sequence[StoredReading]
) -> NewEvent | None:
    if not history:
        return None
    previous = history[0]
    if (
        previous.precipitation_mm > PRECIP_ONSET_BASELINE_MM
        or reading.precipitation_mm < PRECIP_ONSET_TRIGGER_MM
    ):
        return None
    return NewEvent(
        city=reading.city,
        observed_at=reading.observed_at,
        event_type="precip_onset",
        severity="info",
        value=reading.precipitation_mm,
        baseline=previous.precipitation_mm,
        reading_id=reading.id,
        reason=(
            f"Precipitation started in {reading.city}: "
            f"{previous.precipitation_mm:.1f} -> {reading.precipitation_mm:.1f} mm/h."
        ),
    )


def detect_severe_weather(reading: StoredReading) -> NewEvent | None:
    if reading.weather_code not in SEVERE_WEATHER_CODES:
        return None
    return NewEvent(
        city=reading.city,
        observed_at=reading.observed_at,
        event_type="severe_weather",
        severity="critical",
        value=float(reading.weather_code),
        baseline=None,
        reading_id=reading.id,
        reason=(
            f"Severe weather in {reading.city}: WMO code {reading.weather_code} "
            f"({_wmo_label(reading.weather_code)})."
        ),
    )


def detect_synchronized_weather(
    reading: StoredReading,
    latest_per_city: dict[str, StoredReading],
    all_city_names: Iterable[str],
) -> NewEvent | None:
    """Fire when every monitored city shares the same non-trivial weather code.

    ``latest_per_city`` includes the new ``reading`` (caller's job to refresh
    it before calling). We attribute the event to ``reading.city`` so it
    surfaces on that city's timeline; the reason text names all three.
    """
    if reading.weather_code in TRIVIAL_WEATHER_CODES:
        return None
    cities = list(all_city_names)
    snapshot = {c: latest_per_city.get(c) for c in cities}
    if any(s is None for s in snapshot.values()):
        return None
    codes = {s.weather_code for s in snapshot.values() if s is not None}
    if len(codes) != 1:
        return None
    code = next(iter(codes))
    return NewEvent(
        city=reading.city,
        observed_at=reading.observed_at,
        event_type="synchronized_weather",
        severity="info",
        value=float(code),
        baseline=None,
        reading_id=reading.id,
        reason=(
            f"All monitored cities ({', '.join(sorted(cities))}) report "
            f"WMO code {code} ({_wmo_label(code)})."
        ),
    )


def detect_storm(reading: StoredReading) -> NewEvent | None:
    """Compound: strong wind *and* meaningful precipitation at once. Neither
    alone is a storm — a dry gale or a calm downpour each have their own
    detector — but together they are."""
    if (
        reading.wind_speed_kmh < STORM_WIND_KMH
        or reading.precipitation_mm < STORM_PRECIP_MM
    ):
        return None
    return NewEvent(
        city=reading.city,
        observed_at=reading.observed_at,
        event_type="storm",
        severity="warning",
        value=reading.wind_speed_kmh,
        baseline=None,
        reading_id=reading.id,
        reason=(
            f"Storm conditions in {reading.city}: wind {reading.wind_speed_kmh:.0f} "
            f"km/h with {reading.precipitation_mm:.1f} mm/h precipitation."
        ),
    )


def detect_freezing_rain(reading: StoredReading) -> NewEvent | None:
    """Compound: precipitation while at or below freezing. Catches freezing-rain
    risk from the measurements even when the WMO code didn't flag it."""
    if (
        reading.temperature_c > FREEZING_TEMP_C
        or reading.precipitation_mm < FREEZING_PRECIP_MM
    ):
        return None
    return NewEvent(
        city=reading.city,
        observed_at=reading.observed_at,
        event_type="freezing_rain",
        severity="warning",
        value=reading.temperature_c,
        baseline=None,
        reading_id=reading.id,
        reason=(
            f"Freezing-rain risk in {reading.city}: {reading.precipitation_mm:.1f} "
            f"mm/h precipitation at {reading.temperature_c:.1f}C."
        ),
    )


def detect_heat_warning(reading: StoredReading) -> NewEvent | None:
    """Compound + region-aware heat. Fires when it is genuinely warm *and* hot
    for THIS city's season (seasonal z >= ``HEAT_SEASONAL_Z``), so the same 30C
    means different things in maritime Vancouver vs continental Ottawa.

    Humidity is a *severity* modifier, not a gate: a humid day (apparent
    temperature well above actual) escalates to ``critical`` (humidex load),
    while a hot-but-dry day is a ``warning``. When we have no seasonal prior for
    the city we cannot judge "hot for season", so we fall back to requiring the
    humidity load to avoid firing on every warm afternoon.
    """
    if reading.temperature_c < HEAT_ABS_MIN_C:
        return None
    gap = reading.apparent_temperature_c - reading.temperature_c
    humid = gap >= HEAT_APPARENT_GAP_C

    baseline_val: float | None = None
    seasonal_note = ""
    baseline = climate.seasonal_baseline(reading.city, reading.observed_at.month)
    if baseline is not None:
        mean, stddev = baseline
        z = (reading.temperature_c - mean) / stddev
        if z < HEAT_SEASONAL_Z:
            return None
        baseline_val = round(mean, 2)
        seasonal_note = f", {z:+.1f}\u03c3 above the {reading.city} seasonal normal {mean:.1f}C"
    elif not humid:
        # No regional prior and no humidity load => not enough signal to fire.
        return None

    if humid:
        severity = "critical"
        load_note = (
            f" feels like {reading.apparent_temperature_c:.1f}C "
            f"(+{gap:.1f}C humidity load)"
        )
    else:
        severity = "warning"
        load_note = " (dry heat)"

    return NewEvent(
        city=reading.city,
        observed_at=reading.observed_at,
        event_type="heat_warning",
        severity=severity,
        value=reading.temperature_c,
        baseline=baseline_val,
        reading_id=reading.id,
        reason=(
            f"Heat warning in {reading.city}: {reading.temperature_c:.1f}C"
            f"{load_note}{seasonal_note}."
        ),
    )


def detect_cold_warning(reading: StoredReading) -> NewEvent | None:
    """Compound + region-aware cold — the winter mirror of ``heat_warning``.
    Fires when it is genuinely cold *and* cold for THIS city's season
    (seasonal z <= ``-COLD_SEASONAL_Z``), so a routine Ottawa winter day stays
    quiet while the *same* temperature in mild Vancouver is news.

    Wind chill (apparent temperature well below actual) or freezing
    precipitation escalates to ``critical``; otherwise it is a ``warning``. With
    no seasonal prior we fall back to requiring the wind-chill load.
    """
    if reading.temperature_c > COLD_ABS_MAX_C:
        return None
    chill_gap = reading.temperature_c - reading.apparent_temperature_c
    windy_chill = chill_gap >= COLD_WINDCHILL_GAP_C
    icy = (
        reading.temperature_c <= FREEZING_TEMP_C
        and reading.precipitation_mm >= FREEZING_PRECIP_MM
    )

    baseline_val: float | None = None
    seasonal_note = ""
    baseline = climate.seasonal_baseline(reading.city, reading.observed_at.month)
    if baseline is not None:
        mean, stddev = baseline
        z = (reading.temperature_c - mean) / stddev
        if z > -COLD_SEASONAL_Z:
            return None
        baseline_val = round(mean, 2)
        seasonal_note = f", {abs(z):.1f}\u03c3 below the {reading.city} seasonal normal {mean:.1f}C"
    elif not windy_chill:
        # No regional prior and no wind-chill load => not enough signal to fire.
        return None

    if windy_chill:
        severity = "critical"
        load_note = (
            f" feels like {reading.apparent_temperature_c:.1f}C "
            f"(-{chill_gap:.1f}C wind chill)"
        )
    elif icy:
        severity = "critical"
        load_note = f" with {reading.precipitation_mm:.1f} mm/h freezing precipitation"
    else:
        severity = "warning"
        load_note = ""

    return NewEvent(
        city=reading.city,
        observed_at=reading.observed_at,
        event_type="cold_warning",
        severity=severity,
        value=reading.temperature_c,
        baseline=baseline_val,
        reading_id=reading.id,
        reason=(
            f"Cold warning in {reading.city}: {reading.temperature_c:.1f}C"
            f"{load_note}{seasonal_note}."
        ),
    )


# ---- orchestration ---------------------------------------------------------


def candidate_events(
    reading: StoredReading,
    history: Sequence[StoredReading],
    latest_per_city: dict[str, StoredReading],
    all_city_names: Iterable[str],
) -> list[NewEvent]:
    """Run every detector against the new reading. No cooldown filtering —
    that's done separately so it can be tested in isolation."""
    found: list[NewEvent | None] = [
        detect_temperature_anomaly(reading, history),
        detect_wind_spike(reading, history),
        detect_precip_onset(reading, history),
        detect_severe_weather(reading),
        detect_synchronized_weather(reading, latest_per_city, all_city_names),
        detect_storm(reading),
        detect_freezing_rain(reading),
        detect_heat_warning(reading),
        detect_cold_warning(reading),
    ]
    return [e for e in found if e is not None]


def apply_cooldown(
    events: Iterable[NewEvent],
    last_seen: dict[tuple[str, str], datetime | None],
) -> list[NewEvent]:
    """Drop any event whose ``(city, event_type)`` last fired inside its
    cooldown window."""
    kept: list[NewEvent] = []
    for ev in events:
        previous = last_seen.get((ev.city, ev.event_type))
        cooldown = COOLDOWN.get(ev.event_type, timedelta(0))
        if previous is not None and ev.observed_at - previous < cooldown:
            continue
        kept.append(ev)
    return kept


# ---- helpers ---------------------------------------------------------------


_WMO_LABELS: dict[int, str] = {
    0: "clear",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snow fall",
    73: "moderate snow fall",
    75: "heavy snow fall",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


def _wmo_label(code: int) -> str:
    return _WMO_LABELS.get(code, "unknown")
