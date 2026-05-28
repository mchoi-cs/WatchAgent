"""FastAPI entry point.

The ``lifespan`` handler wires the dependency graph: storage opens its
SQLite connection, the Open-Meteo client opens its connection pool, and the
poller starts as a background task. On shutdown we tear everything down in
reverse order.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.routes import router
from .config import get_settings
from .logging_setup import configure_logging
from .openmeteo import OpenMeteoClient
from .poller import Poller
from .storage import Storage

logger = logging.getLogger("watchagent.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("starting watchagent", extra={"db_path": settings.db_path})

    storage = Storage(settings.db_path)
    await storage.connect()
    client = OpenMeteoClient()
    poller = Poller(storage=storage, client=client, settings=settings)

    app.state.settings = settings
    app.state.storage = storage
    app.state.client = client
    app.state.poller = poller

    poller.start()
    try:
        yield
    finally:
        logger.info("stopping watchagent")
        await poller.stop()
        await client.aclose()
        await storage.close()


app = FastAPI(
    title="WatchAgent",
    description=(
        "Weather monitor + notable-event detector for Ottawa, Toronto, and "
        "Vancouver."
    ),
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)
