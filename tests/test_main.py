import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app, lifespan
from app.review.pipeline import ReviewPipeline


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


class FakeEmbedder:
    @property
    def dimension(self) -> int:
        return 4

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return [0.0, 0.0, 0.0, 0.0]


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
    # FakeConsumerSource는 메시지를 절대 내보내지 않아 shutdown 유예시간을
    # 항상 소진하므로, 기본값(130s)이 아니라 짧은 값으로 테스트 속도를 보장한다.
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")

    fake_producer = FakeStartStop()
    fake_consumer = FakeConsumerSource()
    fake_comment_answer_consumer = FakeConsumerSource()
    monkeypatch.setattr("app.main.create_producer", lambda settings: fake_producer)
    monkeypatch.setattr("app.main.create_consumer", lambda settings: fake_consumer)
    monkeypatch.setattr(
        "app.main.create_comment_answer_consumer",
        lambda settings: fake_comment_answer_consumer,
    )

    try:
        async with lifespan(app):
            await asyncio.sleep(0.05)
            assert fake_producer.started
            assert fake_consumer.started
            assert fake_comment_answer_consumer.started

        assert fake_producer.stopped
        assert fake_consumer.stopped
        assert fake_comment_answer_consumer.stopped
    finally:
        get_settings.cache_clear()


class FakeQdrantClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


async def test_lifespan_wires_rag_retriever_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RAG_ENABLED=true 배선 경로(embedder/qdrant_client/vector_store/retriever 조립,
    # 종료 시 qdrant_client.close())는 이 테스트 전까지 어떤 테스트에서도 실행되지
    # 않았다 — 실제 모델/Qdrant 서버 없이 이 경로가 에러 없이 도는지 확인한다.
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("RAG_ENABLED", "true")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")

    fake_producer = FakeStartStop()
    fake_consumer = FakeConsumerSource()
    fake_comment_answer_consumer = FakeConsumerSource()
    fake_qdrant_client = FakeQdrantClient()
    monkeypatch.setattr("app.main.create_producer", lambda settings: fake_producer)
    monkeypatch.setattr("app.main.create_consumer", lambda settings: fake_consumer)
    monkeypatch.setattr(
        "app.main.create_comment_answer_consumer",
        lambda settings: fake_comment_answer_consumer,
    )
    monkeypatch.setattr(
        "app.rag.embeddings.CodeRankEmbedClient", lambda model_name: FakeEmbedder()
    )
    monkeypatch.setattr("qdrant_client.QdrantClient", lambda url: fake_qdrant_client)

    captured_kwargs: dict[str, object] = {}
    original_init = ReviewPipeline.__init__

    def capturing_init(self: ReviewPipeline, *args: object, **kwargs: object) -> None:
        captured_kwargs.update(kwargs)
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ReviewPipeline, "__init__", capturing_init)

    try:
        async with lifespan(app):
            await asyncio.sleep(0.05)

        assert captured_kwargs.get("retriever") is not None
        assert fake_qdrant_client.closed
    finally:
        get_settings.cache_clear()
