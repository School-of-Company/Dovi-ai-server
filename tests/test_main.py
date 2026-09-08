import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.kafka.consumer import ReviewRequestConsumer
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


class FakeNpmRegistryClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.closed = False

    async def check_deprecation(self, name: str, version: str) -> object:
        raise AssertionError("should not be called in this test")

    async def aclose(self) -> None:
        self.closed = True


async def test_lifespan_wires_dependency_resolver_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("DEPENDENCY_CHECK_ENABLED", "true")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")

    fake_producer = FakeStartStop()
    fake_consumer = FakeConsumerSource()
    fake_comment_answer_consumer = FakeConsumerSource()
    fake_npm_registry_client = FakeNpmRegistryClient()
    monkeypatch.setattr("app.main.create_producer", lambda settings: fake_producer)
    monkeypatch.setattr("app.main.create_consumer", lambda settings: fake_consumer)
    monkeypatch.setattr(
        "app.main.create_comment_answer_consumer",
        lambda settings: fake_comment_answer_consumer,
    )
    monkeypatch.setattr(
        "app.context.npm_registry_client.NpmRegistryClient",
        lambda *args, **kwargs: fake_npm_registry_client,
    )

    captured_kwargs: dict[str, object] = {}
    original_init = ReviewPipeline.__init__

    def capturing_init(self: ReviewPipeline, *args: object, **kwargs: object) -> None:
        captured_kwargs.update(kwargs)
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ReviewPipeline, "__init__", capturing_init)

    try:
        async with lifespan(app):
            await asyncio.sleep(0.05)

        assert captured_kwargs.get("dependency_resolver") is not None
        assert fake_npm_registry_client.closed
    finally:
        get_settings.cache_clear()


async def test_lifespan_leaves_dependency_resolver_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")
    # DEPENDENCY_CHECK_ENABLED를 아예 설정하지 않는다 (기본 False 확인)

    fake_producer = FakeStartStop()
    fake_consumer = FakeConsumerSource()
    fake_comment_answer_consumer = FakeConsumerSource()
    monkeypatch.setattr("app.main.create_producer", lambda settings: fake_producer)
    monkeypatch.setattr("app.main.create_consumer", lambda settings: fake_consumer)
    monkeypatch.setattr(
        "app.main.create_comment_answer_consumer",
        lambda settings: fake_comment_answer_consumer,
    )

    captured_kwargs: dict[str, object] = {}
    original_init = ReviewPipeline.__init__

    def capturing_init(self: ReviewPipeline, *args: object, **kwargs: object) -> None:
        captured_kwargs.update(kwargs)
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ReviewPipeline, "__init__", capturing_init)

    try:
        async with lifespan(app):
            await asyncio.sleep(0.05)

        assert captured_kwargs.get("dependency_resolver") is None
    finally:
        get_settings.cache_clear()


class FakeGithubReleaseClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.closed = False

    async def find_release_notes(self, owner_repo: str, name: str, version: str) -> object:
        raise AssertionError("should not be called in this test")

    async def aclose(self) -> None:
        self.closed = True


async def test_lifespan_wires_official_docs_workflow_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("OFFICIAL_DOCS_WORKFLOW_ENABLED", "true")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")

    fake_producer = FakeStartStop()
    fake_consumer = FakeConsumerSource()
    fake_comment_answer_consumer = FakeConsumerSource()
    fake_github_release_client = FakeGithubReleaseClient()
    monkeypatch.setattr("app.main.create_producer", lambda settings: fake_producer)
    monkeypatch.setattr("app.main.create_consumer", lambda settings: fake_consumer)
    monkeypatch.setattr(
        "app.main.create_comment_answer_consumer",
        lambda settings: fake_comment_answer_consumer,
    )
    monkeypatch.setattr(
        "app.context.github_release_client.GithubReleaseClient",
        lambda **kwargs: fake_github_release_client,
    )

    captured_kwargs: dict[str, object] = {}
    original_init = ReviewPipeline.__init__

    def capturing_init(self: ReviewPipeline, *args: object, **kwargs: object) -> None:
        captured_kwargs.update(kwargs)
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ReviewPipeline, "__init__", capturing_init)

    try:
        async with lifespan(app):
            await asyncio.sleep(0.05)

        assert captured_kwargs.get("official_docs_workflow") is not None
        assert fake_github_release_client.closed
    finally:
        get_settings.cache_clear()


async def test_lifespan_leaves_official_docs_workflow_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")
    # OFFICIAL_DOCS_WORKFLOW_ENABLED를 아예 설정하지 않는다 (기본 False 확인)

    fake_producer = FakeStartStop()
    fake_consumer = FakeConsumerSource()
    fake_comment_answer_consumer = FakeConsumerSource()
    monkeypatch.setattr("app.main.create_producer", lambda settings: fake_producer)
    monkeypatch.setattr("app.main.create_consumer", lambda settings: fake_consumer)
    monkeypatch.setattr(
        "app.main.create_comment_answer_consumer",
        lambda settings: fake_comment_answer_consumer,
    )

    captured_kwargs: dict[str, object] = {}
    original_init = ReviewPipeline.__init__

    def capturing_init(self: ReviewPipeline, *args: object, **kwargs: object) -> None:
        captured_kwargs.update(kwargs)
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ReviewPipeline, "__init__", capturing_init)

    try:
        async with lifespan(app):
            await asyncio.sleep(0.05)

        assert captured_kwargs.get("official_docs_workflow") is None
    finally:
        get_settings.cache_clear()


async def test_lifespan_shares_npm_registry_client_across_both_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # dependency_check_enabled와 official_docs_workflow_enabled를 둘 다 켰을 때
    # NpmRegistryClient가 한 번만 생성되는지(패키지당 registry 호출 통합 계약)
    # 확인한다.
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("DEPENDENCY_CHECK_ENABLED", "true")
    monkeypatch.setenv("OFFICIAL_DOCS_WORKFLOW_ENABLED", "true")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")

    fake_producer = FakeStartStop()
    fake_consumer = FakeConsumerSource()
    fake_comment_answer_consumer = FakeConsumerSource()
    fake_npm_registry_client = FakeNpmRegistryClient()
    fake_github_release_client = FakeGithubReleaseClient()
    construction_count = 0

    def _construct_npm_registry_client(*args: object, **kwargs: object) -> object:
        nonlocal construction_count
        construction_count += 1
        return fake_npm_registry_client

    monkeypatch.setattr("app.main.create_producer", lambda settings: fake_producer)
    monkeypatch.setattr("app.main.create_consumer", lambda settings: fake_consumer)
    monkeypatch.setattr(
        "app.main.create_comment_answer_consumer",
        lambda settings: fake_comment_answer_consumer,
    )
    monkeypatch.setattr(
        "app.context.npm_registry_client.NpmRegistryClient", _construct_npm_registry_client
    )
    monkeypatch.setattr(
        "app.context.github_release_client.GithubReleaseClient",
        lambda **kwargs: fake_github_release_client,
    )

    captured_kwargs: dict[str, object] = {}
    original_init = ReviewPipeline.__init__

    def capturing_init(self: ReviewPipeline, *args: object, **kwargs: object) -> None:
        captured_kwargs.update(kwargs)
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ReviewPipeline, "__init__", capturing_init)

    try:
        async with lifespan(app):
            await asyncio.sleep(0.05)

        assert construction_count == 1
        assert captured_kwargs.get("dependency_resolver") is not None
        assert captured_kwargs.get("official_docs_workflow") is not None
    finally:
        get_settings.cache_clear()


class FakeEvaluationRepository:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def save_completed(self, event: object) -> None:
        pass

    async def save_failed(self, event: object) -> None:
        pass

    async def upsert_feedback(self, feedback: object) -> None:
        pass


class FakeConnection:
    def __init__(self, *, raise_on_execute: bool = False) -> None:
        self.raise_on_execute = raise_on_execute
        self.executed = False

    async def execute(self, statement: object) -> None:
        self.executed = True
        if self.raise_on_execute:
            raise RuntimeError("relation 'review_jobs' does not exist")


class _FakeConnectContext:
    def __init__(self, engine: "FakeEngine") -> None:
        self._engine = engine

    async def __aenter__(self) -> FakeConnection:
        if self._engine.raise_on_connect:
            raise RuntimeError("could not connect to server")
        return self._engine.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeEngine:
    def __init__(
        self, *, raise_on_connect: bool = False, raise_on_execute: bool = False
    ) -> None:
        self.disposed = False
        self.raise_on_connect = raise_on_connect
        self.connection = FakeConnection(raise_on_execute=raise_on_execute)

    def connect(self) -> _FakeConnectContext:
        return _FakeConnectContext(self)

    async def dispose(self) -> None:
        self.disposed = True


async def test_lifespan_wires_evaluation_repository_and_feedback_consumer_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("EVALUATION_ENABLED", "true")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")

    fake_producer = FakeStartStop()
    fake_consumer = FakeConsumerSource()
    fake_comment_answer_consumer = FakeConsumerSource()
    fake_feedback_consumer = FakeConsumerSource()
    fake_engine = FakeEngine()
    monkeypatch.setattr("app.main.create_producer", lambda settings: fake_producer)
    monkeypatch.setattr("app.main.create_consumer", lambda settings: fake_consumer)
    monkeypatch.setattr(
        "app.main.create_comment_answer_consumer",
        lambda settings: fake_comment_answer_consumer,
    )
    monkeypatch.setattr(
        "app.main.create_review_feedback_consumer",
        lambda settings: fake_feedback_consumer,
    )
    monkeypatch.setattr("app.evaluation.db.create_engine", lambda settings: fake_engine)
    monkeypatch.setattr(
        "app.evaluation.repository.SqlAlchemyEvaluationRepository",
        lambda session_factory: FakeEvaluationRepository(),
    )

    # ReviewPipeline이 아니라 ReviewRequestConsumer가 evaluation_repository를
    # 받는 쪽이므로 그쪽 kwargs를 캡처해 실제 배선을 검증한다.
    captured_kwargs: dict[str, object] = {}
    original_init = ReviewRequestConsumer.__init__

    def capturing_init(
        self: ReviewRequestConsumer, *args: object, **kwargs: object
    ) -> None:
        captured_kwargs.update(kwargs)
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ReviewRequestConsumer, "__init__", capturing_init)

    try:
        async with lifespan(app):
            await asyncio.sleep(0.05)
            assert fake_feedback_consumer.started

        assert captured_kwargs.get("evaluation_repository") is not None
        # DB/스키마 확인 쿼리가 실제로 실행됐는지 (fail-fast probe 배선 확인)
        assert fake_engine.connection.executed
        assert fake_feedback_consumer.stopped
        assert fake_engine.disposed
    finally:
        get_settings.cache_clear()


def _patch_kafka_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.main.create_producer", lambda settings: FakeStartStop())
    monkeypatch.setattr("app.main.create_consumer", lambda settings: FakeConsumerSource())
    monkeypatch.setattr(
        "app.main.create_comment_answer_consumer",
        lambda settings: FakeConsumerSource(),
    )
    monkeypatch.setattr(
        "app.main.create_review_feedback_consumer",
        lambda settings: FakeConsumerSource(),
    )


@pytest.mark.parametrize(
    ("raise_on_connect", "raise_on_execute"),
    [(True, False), (False, True)],
)
async def test_lifespan_fails_fast_when_evaluation_db_unreachable(
    monkeypatch: pytest.MonkeyPatch, raise_on_connect: bool, raise_on_execute: bool
) -> None:
    # evaluation_enabled=true인데 DB에 못 붙거나(연결 실패) review_jobs가 없으면
    # (마이그레이션 미적용) 조용히 기동해서 평가 레코드를 영구히 버리는 대신
    # 기동 자체가 실패해야 한다.
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("EVALUATION_ENABLED", "true")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")

    _patch_kafka_fakes(monkeypatch)
    fake_engine = FakeEngine(
        raise_on_connect=raise_on_connect, raise_on_execute=raise_on_execute
    )
    monkeypatch.setattr("app.evaluation.db.create_engine", lambda settings: fake_engine)

    try:
        with pytest.raises(RuntimeError):
            async with lifespan(app):
                pytest.fail("lifespan must not yield when the DB probe fails")
    finally:
        get_settings.cache_clear()


async def test_lifespan_leaves_evaluation_repository_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")
    # EVALUATION_ENABLED를 아예 설정하지 않는다 (기본 False 확인)

    fake_producer = FakeStartStop()
    fake_consumer = FakeConsumerSource()
    fake_comment_answer_consumer = FakeConsumerSource()
    monkeypatch.setattr("app.main.create_producer", lambda settings: fake_producer)
    monkeypatch.setattr("app.main.create_consumer", lambda settings: fake_consumer)
    monkeypatch.setattr(
        "app.main.create_comment_answer_consumer",
        lambda settings: fake_comment_answer_consumer,
    )
    # evaluation_enabled 가드가 사라지면 조용히 통과하지 않고 실패해야 한다.
    monkeypatch.setattr(
        "app.evaluation.db.create_engine",
        lambda settings: pytest.fail("must not be called when evaluation_enabled is False"),
    )

    try:
        async with lifespan(app):
            await asyncio.sleep(0.05)
    finally:
        get_settings.cache_clear()
