"""Background poller.

Owns the polling loop: for each city, fetch current weather, dedup-insert the
reading, run detectors, apply cooldown, persist events. A single failed city
never breaks the cycle — that constraint is the ``poller-error-handling.mdc``
rule made code.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from . import events as event_logic
from .config import CITIES, City, Settings
from .openmeteo import OpenMeteoClient, OpenMeteoError, Reading
from .storage import NewEvent, Storage, StoredReading

logger = logging.getLogger("watchagent.poller")


class Poller:
    """Long-running poller; ``start()`` returns the asyncio ``Task``."""

    def __init__(
        self,
        storage: Storage,
        client: OpenMeteoClient,
        settings: Settings,
        cities: tuple[City, ...] = CITIES,
    ) -> None:
        self._storage = storage
        self._client = client
        self._settings = settings
        self._cities = cities
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="watchagent-poller")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            self._task = None

    async def _run(self) -> None:
        logger.info(
            "poller started",
            extra={"interval_s": self._settings.poll_interval_seconds},
        )
        while not self._stop.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._settings.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass
        logger.info("poller stopped")

    async def poll_once(self) -> None:
        """One cycle across all cities — also the entry point tests use."""
        results = await asyncio.gather(
            *(self._poll_city(city) for city in self._cities),
            return_exceptions=False,
        )
        new_count = sum(1 for r in results if r is not None)
        logger.info("poll cycle complete", extra={"new_readings": new_count})

    async def _poll_city(self, city: City) -> StoredReading | None:
        reading = await self._fetch_with_retries(city)
        if reading is None:
            return None
        stored = await self._storage.store_reading(reading)
        if stored is None:
            logger.debug(
                "duplicate reading skipped",
                extra={
                    "city": city.name,
                    "observed_at": reading.observed_at.isoformat(),
                },
            )
            return None
        await self._evaluate_events(stored)
        logger.info(
            "new reading stored",
            extra={
                "city": stored.city,
                "observed_at": stored.observed_at.isoformat(),
                "temperature_c": stored.temperature_c,
                "weather_code": stored.weather_code,
            },
        )
        return stored

    async def _fetch_with_retries(self, city: City) -> Reading | None:
        max_retries = max(0, self._settings.poll_max_retries)
        backoff = max(0.0, self._settings.poll_retry_backoff)
        attempt = 0
        while True:
            try:
                return await self._client.fetch_current(city)
            except (httpx.HTTPError, OpenMeteoError) as exc:
                status = _status_of(exc)
                attempt += 1
                if attempt > max_retries:
                    logger.warning(
                        "open-meteo fetch failed (giving up)",
                        extra={
                            "city": city.name,
                            "http_status": status,
                            "retry": attempt - 1,
                            "error": str(exc),
                        },
                    )
                    return None
                logger.warning(
                    "open-meteo fetch failed (retrying)",
                    extra={
                        "city": city.name,
                        "http_status": status,
                        "retry": attempt,
                        "error": str(exc),
                    },
                )
                await asyncio.sleep(backoff * (2 ** (attempt - 1)))

    async def _evaluate_events(self, reading: StoredReading) -> None:
        history = await self._storage.readings_for_city(
            reading.city, limit=event_logic.ROLLING_WINDOW + 1
        )
        # history is newest-first and already contains the new reading.
        prior = [r for r in history if r.id != reading.id]

        latest_per_city = await self._storage.latest_reading_per_city()
        latest_per_city[reading.city] = reading

        all_names = tuple(c.name for c in self._cities)
        candidates = event_logic.candidate_events(
            reading=reading,
            history=prior,
            latest_per_city=latest_per_city,
            all_city_names=all_names,
        )
        if not candidates:
            return

        last_seen = {
            (ev.city, ev.event_type): await self._storage.last_event_at(
                ev.city, ev.event_type
            )
            for ev in candidates
        }
        keepers = event_logic.apply_cooldown(candidates, last_seen)
        if not keepers:
            return
        stored = await self._storage.store_events(keepers)
        for ev in stored:
            logger.info(
                "event fired",
                extra={
                    "city": ev.city,
                    "event_type": ev.event_type,
                    "severity": ev.severity,
                    "observed_at": ev.observed_at.isoformat(),
                },
            )


def _status_of(exc: BaseException) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return None


__all__ = ["Poller", "NewEvent"]
