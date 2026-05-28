"""HTTP routes.

These routes never touch ``aiosqlite`` directly — that constraint is what the
``db-access.mdc`` rule enforces. They go through :class:`Storage`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ..config import CITY_NAMES
from ..storage import Storage
from .schemas import (
    Event,
    EventsResponse,
    HealthResponse,
    Reading,
    ReadingsResponse,
)

router = APIRouter()

MAX_LIMIT = 500
DEFAULT_LIMIT = 50


def _storage(request: Request) -> Storage:
    storage: Storage | None = getattr(request.app.state, "storage", None)
    if storage is None:
        raise HTTPException(status_code=503, detail="storage not initialised")
    return storage


def _validate_city(city: str | None) -> str | None:
    if city is None:
        return None
    if city not in CITY_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown city '{city}'; valid: {sorted(CITY_NAMES)}",
        )
    return city


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    storage = _storage(request)
    return HealthResponse(
        status="ok",
        readings_stored=await storage.count_readings(),
        events_stored=await storage.count_events(),
    )


@router.get("/readings", response_model=ReadingsResponse)
async def readings(
    request: Request,
    city: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> ReadingsResponse:
    storage = _storage(request)
    city = _validate_city(city)
    rows = await storage.recent_readings(city=city, limit=limit)
    return ReadingsResponse(readings=[Reading.model_validate(r.to_dict()) for r in rows])


@router.get("/events", response_model=EventsResponse)
async def events(
    request: Request,
    city: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> EventsResponse:
    storage = _storage(request)
    city = _validate_city(city)
    rows = await storage.recent_events(city=city, limit=limit)
    return EventsResponse(events=[Event.model_validate(r.to_dict()) for r in rows])
