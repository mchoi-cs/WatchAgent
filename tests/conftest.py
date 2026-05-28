"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from watchagent.storage import Storage


@pytest_asyncio.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    db_path = tmp_path / "test.db"
    s = Storage(str(db_path))
    await s.connect()
    try:
        yield s
    finally:
        await s.close()
