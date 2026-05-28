"""Event-detection tests.

These tests are the direct expression of the event-detection design. Each
test asserts a specific claim from the README's reasoning section.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from watchagent import events as event_logic
from watchagent.events import (
    MIN_ABS_DELTA_C,
    apply_cooldown,
    candidate_events,
    detect_precip_onset,
    detect_severe_weather,
    detect_synchronized_weather,
    detect_temperature_anomaly,
    detect_wind_spike,
)
from watchagent.storage import NewEvent, StoredReading


def _r(
    *,
    rid: int,
    city: str = "Ottawa",
    hour: int = 12,
    temperature: float = 18.0,
    apparent: float | None = None,
    precip: float = 0.0,
    wind: float = 10.0,
    code: int = 1,
) -> StoredReading:
    observed = datetime(2026, 5, 26, 0, 0, tzinfo=timezone.utc) + timedelta(hours=hour)
    return StoredReading(
        id=rid,
        city=city,
        observed_at=observed,
        temperature_c=temperature,
        apparent_temperature_c=apparent if apparent is not None else temperature - 1.5,
        precipitation_mm=precip,
        wind_speed_kmh=wind,
        weather_code=code,
        fetched_at=observed,
    )


# ---- temperature anomaly --------------------------------------------------


def test_temperature_anomaly_fires_on_clear_outlier() -> None:
    history = [_r(rid=i, hour=i, temperature=18.0 + (i % 3) * 0.5) for i in range(24, 0, -1)]
    new = _r(rid=100, hour=25, temperature=35.0)
    event = detect_temperature_anomaly(new, history)
    assert event is not None
    assert event.event_type == "temperature_anomaly"
    assert event.value == 35.0
    assert event.baseline is not None and 17.0 < event.baseline < 20.0
    assert "hot" in event.reason


def test_temperature_anomaly_silent_on_flat_window() -> None:
    history = [_r(rid=i, hour=i, temperature=20.0) for i in range(24, 0, -1)]
    new = _r(rid=100, hour=25, temperature=20.1)
    assert detect_temperature_anomaly(new, history) is None


def test_temperature_anomaly_requires_minimum_window() -> None:
    history = [_r(rid=i, hour=i, temperature=20.0) for i in range(3, 0, -1)]
    new = _r(rid=100, hour=4, temperature=40.0)
    assert detect_temperature_anomaly(new, history) is None


def test_temperature_anomaly_respects_min_abs_delta() -> None:
    """A reading that's many stddevs out but only a tiny absolute delta
    should not fire — this is what stops a still day from generating noise
    on tiny variations."""
    history = [_r(rid=i, hour=i, temperature=20.0 + (i % 2) * 0.05) for i in range(24, 0, -1)]
    new = _r(rid=100, hour=25, temperature=20.0 + MIN_ABS_DELTA_C - 1)
    assert detect_temperature_anomaly(new, history) is None


# ---- wind spike -----------------------------------------------------------


def test_wind_spike_fires_on_jump() -> None:
    history = [_r(rid=1, hour=1, wind=15.0)]
    new = _r(rid=2, hour=2, wind=55.0)
    event = detect_wind_spike(new, history)
    assert event is not None
    assert event.event_type == "wind_spike"
    assert event.value == 55.0
    assert event.baseline == 15.0


def test_wind_spike_silent_on_plateau() -> None:
    history = [_r(rid=1, hour=1, wind=55.0)]
    new = _r(rid=2, hour=2, wind=58.0)
    assert detect_wind_spike(new, history) is None


def test_wind_spike_silent_when_absolute_low() -> None:
    """A spike from 0 to 25 km/h shouldn't count — gusty calm is not news."""
    history = [_r(rid=1, hour=1, wind=0.0)]
    new = _r(rid=2, hour=2, wind=25.0)
    assert detect_wind_spike(new, history) is None


# ---- precip onset ---------------------------------------------------------


def test_precip_onset_fires_on_start() -> None:
    history = [_r(rid=1, hour=1, precip=0.0)]
    new = _r(rid=2, hour=2, precip=3.0)
    event = detect_precip_onset(new, history)
    assert event is not None
    assert event.event_type == "precip_onset"


def test_precip_onset_silent_when_already_raining() -> None:
    history = [_r(rid=1, hour=1, precip=1.5)]
    new = _r(rid=2, hour=2, precip=4.0)
    assert detect_precip_onset(new, history) is None


# ---- severe weather -------------------------------------------------------


def test_severe_weather_fires_on_listed_code() -> None:
    event = detect_severe_weather(_r(rid=1, code=95))
    assert event is not None
    assert event.severity == "critical"


def test_severe_weather_silent_on_calm_code() -> None:
    assert detect_severe_weather(_r(rid=1, code=1)) is None


# ---- synchronized weather -------------------------------------------------


def test_synchronized_weather_fires_when_all_cities_match_non_trivial_code() -> None:
    latest = {
        "Ottawa": _r(rid=1, city="Ottawa", code=61),
        "Toronto": _r(rid=2, city="Toronto", code=61),
        "Vancouver": _r(rid=3, city="Vancouver", code=61),
    }
    event = detect_synchronized_weather(
        latest["Ottawa"], latest, ("Ottawa", "Toronto", "Vancouver")
    )
    assert event is not None
    assert event.event_type == "synchronized_weather"


def test_synchronized_weather_silent_on_trivial_code() -> None:
    """All cities clear is not news."""
    latest = {
        "Ottawa": _r(rid=1, city="Ottawa", code=0),
        "Toronto": _r(rid=2, city="Toronto", code=0),
        "Vancouver": _r(rid=3, city="Vancouver", code=0),
    }
    event = detect_synchronized_weather(
        latest["Ottawa"], latest, ("Ottawa", "Toronto", "Vancouver")
    )
    assert event is None


def test_synchronized_weather_silent_when_one_city_differs() -> None:
    latest = {
        "Ottawa": _r(rid=1, city="Ottawa", code=61),
        "Toronto": _r(rid=2, city="Toronto", code=61),
        "Vancouver": _r(rid=3, city="Vancouver", code=1),
    }
    event = detect_synchronized_weather(
        latest["Ottawa"], latest, ("Ottawa", "Toronto", "Vancouver")
    )
    assert event is None


# ---- cooldown -------------------------------------------------------------


def _new_event(city: str, event_type: str, hour: int) -> NewEvent:
    observed = datetime(2026, 5, 26, 0, 0, tzinfo=timezone.utc) + timedelta(hours=hour)
    return NewEvent(
        city=city,
        observed_at=observed,
        event_type=event_type,
        severity="warning",
        reason="test",
    )


def test_cooldown_suppresses_repeat_within_window() -> None:
    first = _new_event("Ottawa", "temperature_anomaly", hour=0)
    second = _new_event("Ottawa", "temperature_anomaly", hour=1)
    last_seen = {("Ottawa", "temperature_anomaly"): first.observed_at}
    kept = apply_cooldown([second], last_seen)
    assert kept == []


def test_cooldown_allows_repeat_after_window() -> None:
    first = _new_event("Ottawa", "temperature_anomaly", hour=0)
    cooldown_hours = event_logic.COOLDOWN["temperature_anomaly"].total_seconds() / 3600
    second = _new_event("Ottawa", "temperature_anomaly", hour=int(cooldown_hours) + 1)
    last_seen = {("Ottawa", "temperature_anomaly"): first.observed_at}
    kept = apply_cooldown([second], last_seen)
    assert len(kept) == 1


def test_cooldown_is_per_city_and_per_type() -> None:
    last_seen = {
        ("Ottawa", "wind_spike"): datetime(2026, 5, 26, 12, tzinfo=timezone.utc),
    }
    candidates = [
        _new_event("Toronto", "wind_spike", hour=13),
        _new_event("Ottawa", "precip_onset", hour=13),
    ]
    kept = apply_cooldown(candidates, last_seen)
    assert len(kept) == 2


# ---- orchestration --------------------------------------------------------


def test_candidate_events_combines_detectors() -> None:
    # Small variation so stddev is non-zero — otherwise the anomaly detector
    # short-circuits before it can fire.
    history = [
        _r(rid=i, hour=i, temperature=20.0 + (i % 3) * 0.5) for i in range(24, 0, -1)
    ]
    history.insert(0, _r(rid=99, hour=25, wind=5.0, precip=0.0, temperature=20.0))
    new = _r(rid=100, hour=26, temperature=35.0, wind=60.0, precip=5.0, code=95)
    latest = {
        "Ottawa": new,
        "Toronto": _r(rid=200, city="Toronto", hour=26, code=95),
        "Vancouver": _r(rid=300, city="Vancouver", hour=26, code=95),
    }
    events = candidate_events(
        reading=new,
        history=history,
        latest_per_city=latest,
        all_city_names=("Ottawa", "Toronto", "Vancouver"),
    )
    types = {e.event_type for e in events}
    assert {"temperature_anomaly", "wind_spike", "precip_onset", "severe_weather", "synchronized_weather"} <= types
