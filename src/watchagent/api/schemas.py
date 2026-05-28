"""Pydantic response models — these are the API's contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Reading(BaseModel):
    id: int
    city: str
    observed_at: datetime
    temperature_c: float
    apparent_temperature_c: float
    precipitation_mm: float
    wind_speed_kmh: float
    weather_code: int
    fetched_at: datetime


class Event(BaseModel):
    id: int
    city: str
    observed_at: datetime
    event_type: str
    severity: str
    value: float | None
    baseline: float | None
    reason: str
    reading_id: int | None


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    readings_stored: int = Field(ge=0)
    events_stored: int = Field(ge=0)


class ReadingsResponse(BaseModel):
    readings: list[Reading]


class EventsResponse(BaseModel):
    events: list[Event]
