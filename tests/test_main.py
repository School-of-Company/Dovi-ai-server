import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app, lifespan


class FakeStartStop:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send_and_wait(
        self, topic: str, value: bytes, key: bytes | None = None
    ) -> None:
        raise AssertionError("no message should be sent in this test")


class FakeConsumerSource(FakeStartStop):
    async def __aiter__(self) -> AsyncIterator[Any]:
        while True:
            await asyncio.sleep(3600)
            yield  # pragma: no cover

    async def commit(self) -> None:
        pass


def test_health_check_with_consumer_disabled() -> None:
    # lifespan을 실제로 태우는 유일한 테스트 — kafka_consumer_enabled 기본값(False)에서
    # Kafka/LLM에 전혀 연결을 시도하지 않고 앱이 정상 기동/종료되는지 확인한다.
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_lifespan_starts_and_cancels_consumer_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")

    fake_producer = FakeStartStop()
    fake_consumer = FakeConsumerSource()
    monkeypatch.setattr("app.main.create_producer", lambda settings: fake_producer)
    monkeypatch.setattr("app.main.create_consumer", lambda settings: fake_consumer)

    try:
        async with lifespan(app):
            await asyncio.sleep(0.05)
            assert fake_producer.started
            assert fake_consumer.started

        assert fake_producer.stopped
        assert fake_consumer.stopped
    finally:
        get_settings.cache_clear()
