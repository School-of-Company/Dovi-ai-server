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
    retriever = None
    if settings.rag_enabled:
        # qdrant-client는 numpy를 끌어오는데, 이를 지원 안 하는 CPU에서는 import만
        # 해도 죽는다(RAG를 안 켜는 배포에까지 그 위험을 지우지 않도록 지연 import).
        from qdrant_client import QdrantClient

        from app.rag.embeddings import CodeRankEmbedClient
        from app.rag.retriever import ProjectContextRetriever
        from app.rag.vector_store import QdrantVectorStore

        embedder = CodeRankEmbedClient(settings.embedding_model)
        qdrant_client = QdrantClient(url=settings.qdrant_url)
        vector_store = QdrantVectorStore(
            qdrant_client, settings.rag_collection_name, vector_size=embedder.dimension
        )
        retriever = ProjectContextRetriever(embedder, vector_store)

    pipeline = ReviewPipeline(
        llm_client,
        model_version=settings.llm_model,
        prompt_version="v1",
        retriever=retriever,
    )

    comment_answer_pipeline = CommentAnswerPipeline(llm_client)

    kafka_producer = create_producer(settings)
    kafka_consumer = create_consumer(settings)
    comment_answer_kafka_consumer = create_comment_answer_consumer(settings)
    await kafka_producer.start()
    await kafka_consumer.start()
    await comment_answer_kafka_consumer.start()

    redis_client = create_redis_client(settings)
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
        kafka_consumer, pipeline, event_producer, dedup_store
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
    tasks = (consumer_task, comment_answer_task)

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
        await kafka_producer.stop()
        await redis_client.aclose()
        await llm_client.aclose()
        if qdrant_client is not None:
            qdrant_client.close()


settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)

app.include_router(health_router)
