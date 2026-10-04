import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


class _IdleTrackerSource:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def __aiter__(self) -> "_IdleTrackerSource":
        return self

    async def __anext__(self) -> object:
        await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def commit(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _fake_pr_head_tracker_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.main.create_pr_head_tracker_consumer", lambda settings: _IdleTrackerSource()
    )
