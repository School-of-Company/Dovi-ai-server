import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from fastapi import FastAPI

from app.api.health import router as health_router
from app.comment_answer.consumer import CommentAnswerConsumer
from app.comment_answer.pipeline import CommentAnswerPipeline
from app.common.logger import configure_logging
from app.core.config import get_settings
from app.kafka.client import (
    create_comment_answer_consumer,
    create_consumer,
    create_producer,
    create_review_feedback_consumer,
)
from app.kafka.consumer import ReviewRequestConsumer
from app.kafka.producer import CommentAnswerEventProducer, ReviewEventProducer
from app.llm.openai_compatible_client import OpenAICompatibleLLMClient
from app.review.dedup import (
    create_comment_answer_dedup_store,
    create_dedup_store,
    create_redis_client,
)
from app.review.pipeline import ReviewPipeline

logger = logging.getLogger(__name__)


class _RunnableConsumer(Protocol):
    async def run(self, shutdown: asyncio.Event | None = None) -> None: ...


async def _run_consumer_forever(
    consumer: _RunnableConsumer, shutdown: asyncio.Event, *, name: str
) -> None:
    while not shutdown.is_set():
        try:
            await consumer.run(shutdown)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s consumer loop crashed, restarting in 5s", name)
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    if not settings.kafka_consumer_enabled:
        yield
        return

    llm_client = OpenAICompatibleLLMClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )

    qdrant_client = None
    npm_registry_client = None
    retriever = None
    api_spec_retriever = None
    if settings.rag_enabled:
        # qdrant-client는 numpy를 끌어오는데, 이를 지원 안 하는 CPU에서는 import만
        # 해도 죽는다(RAG를 안 켜는 배포에까지 그 위험을 지우지 않도록 지연 import).
        from qdrant_client import QdrantClient

        from app.rag.embeddings import CodeRankEmbedClient
        from app.rag.reranker import CrossEncoderReranker
        from app.rag.retriever import ProjectContextRetriever
        from app.rag.vector_store import QdrantVectorStore

        embedder = CodeRankEmbedClient(settings.embedding_model)
        reranker = CrossEncoderReranker(settings.reranker_model)
        qdrant_client = QdrantClient(url=settings.qdrant_url)
        vector_store = QdrantVectorStore(
            qdrant_client, settings.rag_collection_name, vector_size=embedder.dimension
        )
        retriever = ProjectContextRetriever(embedder, vector_store, reranker=reranker)

        # API 명세 검색은 RAG 인프라(embedder/qdrant_client)가 이미 있어야 의미가
        # 있으므로 rag_enabled 조건 안에서만 notion_sync_enabled를 추가로 확인한다.
        if settings.notion_sync_enabled:
            from app.context.api_spec_retriever import ApiSpecRetriever
            from app.rag.api_spec_vector_store import ApiSpecVectorStore

            api_spec_vector_store = ApiSpecVectorStore(
                qdrant_client, settings.api_spec_collection_name, vector_size=embedder.dimension
            )
            api_spec_retriever = ApiSpecRetriever(embedder, api_spec_vector_store)

    # notion_link_store가 redis_client를 필요로 하므로 pipeline 생성보다 먼저 만든다.
    redis_client = create_redis_client(settings)

    notion_link_store = None
    if settings.notion_sync_enabled:
        from app.context.api_spec_link_store import RedisNotionLinkStore

        # redis.asyncio.Redis의 실제 타입 스텁이 RedisLike보다 훨씬 넓어 구조적으로
        # 완전히 일치하지 않지만, set/get/keys를 문자열 인자로만 호출하므로 런타임에는 호환된다.
        notion_link_store = RedisNotionLinkStore(redis_client)  # type: ignore[arg-type]

    if settings.dependency_check_enabled or settings.official_docs_workflow_enabled:
        from app.context.npm_registry_client import NpmRegistryClient

        npm_registry_client = NpmRegistryClient()

    dependency_resolver = None
    if settings.dependency_check_enabled:
        from app.context.dependency_resolver import DependencyResolver
        from app.context.npm_deprecation_cache import RedisNpmDeprecationCache

        # redis.asyncio.Redis의 실제 타입 스텁이 RedisLike보다 훨씬 넓어 구조적으로
        # 완전히 일치하지 않지만, set/get을 문자열 인자로만 호출하므로 런타임에는 호환된다.
        npm_deprecation_cache = RedisNpmDeprecationCache(redis_client)  # type: ignore[arg-type]
        assert npm_registry_client is not None
        dependency_resolver = DependencyResolver(npm_registry_client, npm_deprecation_cache)

    github_release_client = None
    official_docs_workflow = None
    if settings.official_docs_workflow_enabled:
        from app.context.github_release_client import GithubReleaseClient
        from app.context.official_docs_workflow import OfficialDocsWorkflow
        from app.context.release_notes_cache import RedisReleaseNotesCache

        github_release_client = GithubReleaseClient(token=settings.github_token)
        release_notes_cache = RedisReleaseNotesCache(redis_client)
        assert npm_registry_client is not None
        official_docs_workflow = OfficialDocsWorkflow(
            npm_registry_client, github_release_client, release_notes_cache
        )

    evaluation_repository = None
    evaluation_engine = None
    if settings.evaluation_enabled:
        from app.evaluation.db import create_engine, create_session_factory
        from app.evaluation.repository import SqlAlchemyEvaluationRepository

        evaluation_engine = create_engine(settings)
        session_factory = create_session_factory(evaluation_engine)
        evaluation_repository = SqlAlchemyEvaluationRepository(session_factory)

        # 스펙 판정: evaluation_enabled=true인데 DB/스키마가 없으면 다른 필수
        # 인프라와 동급으로 기동 실패시킨다(조용한 무동작 대신) — create_async_engine()은
        # lazy connect라 여기서 명시적으로 확인해야 한다. review_jobs를 직접
        # 건드리는 쿼리라 "연결은 되지만 마이그레이션 미적용" 케이스도 같이 잡는다.
        from sqlalchemy import select

        from app.evaluation.models import ReviewJobRow

        async with evaluation_engine.connect() as conn:
            await conn.execute(select(ReviewJobRow.review_job_id).limit(1))

    pipeline = ReviewPipeline(
        llm_client,
        model_version=settings.llm_model,
        prompt_version="v1",
        retriever=retriever,
        api_spec_retriever=api_spec_retriever,
        notion_link_store=notion_link_store,
        dependency_resolver=dependency_resolver,
        official_docs_workflow=official_docs_workflow,
    )

    comment_answer_pipeline = CommentAnswerPipeline(llm_client)

    kafka_producer = create_producer(settings)
    kafka_consumer = create_consumer(settings)
    comment_answer_kafka_consumer = create_comment_answer_consumer(settings)
    await kafka_producer.start()
    await kafka_consumer.start()
    await comment_answer_kafka_consumer.start()

    review_feedback_kafka_consumer = None
    if settings.evaluation_enabled:
        review_feedback_kafka_consumer = create_review_feedback_consumer(settings)
        await review_feedback_kafka_consumer.start()

    # redis.asyncio.Redis의 실제 타입 스텁이 RedisLike보다 훨씬 넓어 구조적으로
    # 완전히 일치하지 않지만, set/get/delete를 문자열 인자로만 호출하므로 런타임에는 호환된다.
    dedup_store = create_dedup_store(settings, redis_client)  # type: ignore[arg-type]
    comment_answer_dedup_store = create_comment_answer_dedup_store(
        settings, redis_client  # type: ignore[arg-type]
    )

    event_producer = ReviewEventProducer(
        kafka_producer,
        completed_topic=settings.kafka_review_completed_topic,
        failed_topic=settings.kafka_review_failed_topic,
    )
    comment_answer_event_producer = CommentAnswerEventProducer(
        kafka_producer,
        completed_topic=settings.kafka_comment_answer_completed_topic,
        failed_topic=settings.kafka_comment_answer_failed_topic,
    )
    review_consumer = ReviewRequestConsumer(
        kafka_consumer,
        pipeline,
        event_producer,
        dedup_store,
        evaluation_repository=evaluation_repository,
    )
    comment_answer_consumer = CommentAnswerConsumer(
        comment_answer_kafka_consumer,
        comment_answer_pipeline,
        comment_answer_event_producer,
        comment_answer_dedup_store,
    )
    shutdown_event = asyncio.Event()
    consumer_task = asyncio.create_task(
        _run_consumer_forever(review_consumer, shutdown_event, name="review")
    )
    comment_answer_task = asyncio.create_task(
        _run_consumer_forever(
            comment_answer_consumer, shutdown_event, name="comment-answer"
        )
    )
    tasks: list[asyncio.Task[None]] = [consumer_task, comment_answer_task]
    if review_feedback_kafka_consumer is not None:
        from app.evaluation.feedback_consumer import ReviewFeedbackConsumer

        assert evaluation_repository is not None
        review_feedback_consumer = ReviewFeedbackConsumer(
            review_feedback_kafka_consumer, evaluation_repository
        )
        review_feedback_task = asyncio.create_task(
            _run_consumer_forever(
                review_feedback_consumer, shutdown_event, name="review-feedback"
            )
        )
        tasks.append(review_feedback_task)

    try:
        yield
    finally:
        # 처리 중인 메시지가 있으면 커밋까지 마무리할 시간을 준다. 유예시간을
        # 넘기면 강제 취소하되, 락 해제는 각 consumer.handle()이 책임진다.
        shutdown_event.set()
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks), timeout=settings.graceful_shutdown_seconds
            )
        except (TimeoutError, asyncio.CancelledError):
            pass
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await kafka_consumer.stop()
        await comment_answer_kafka_consumer.stop()
        if review_feedback_kafka_consumer is not None:
            await review_feedback_kafka_consumer.stop()
        await kafka_producer.stop()
        await redis_client.aclose()
        await llm_client.aclose()
        if qdrant_client is not None:
            qdrant_client.close()
        if npm_registry_client is not None:
            await npm_registry_client.aclose()
        if github_release_client is not None:
            await github_release_client.aclose()
        if evaluation_engine is not None:
            await evaluation_engine.dispose()


settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)

app.include_router(health_router)
