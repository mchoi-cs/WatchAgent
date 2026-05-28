"""Runtime configuration.

All knobs that vary between dev / docker / CI live here so the rest of the
codebase never reads ``os.environ`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class City:
    name: str
    latitude: float
    longitude: float


CITIES: tuple[City, ...] = (
    City("Ottawa", 45.42, -75.69),
    City("Toronto", 43.70, -79.42),
    City("Vancouver", 49.25, -123.12),
)

CITY_NAMES: frozenset[str] = frozenset(c.name for c in CITIES)


class Settings(BaseSettings):
    """Process-wide settings, loaded from env / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    poll_interval_seconds: int = 300
    poll_max_retries: int = 3
    poll_retry_backoff: float = 1.0
    db_path: str = "./data/weather.db"
    log_level: str = "INFO"


def get_settings() -> Settings:
    """Single entry point so tests can monkeypatch this if needed."""
    return Settings()
