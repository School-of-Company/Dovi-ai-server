import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import get_settings
from app.kafka.client import create_consumer, create_producer
from app.kafka.consumer import ReviewRequestConsumer
from app.kafka.producer import ReviewEventProducer
from app.llm.openai_compatible_client import OpenAICompatibleLLMClient
from app.review.pipeline import ReviewPipeline

logger = logging.getLogger(__name__)


async def _run_consumer_forever(review_consumer: ReviewRequestConsumer) -> None:
    while True:
        try:
            await review_consumer.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("review consumer loop crashed, restarting in 5s")
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
    pipeline = ReviewPipeline(
        llm_client, model_version=settings.llm_model, prompt_version="v1"
    )

    kafka_producer = create_producer(settings)
    kafka_consumer = create_consumer(settings)
    await kafka_producer.start()
    await kafka_consumer.start()

    event_producer = ReviewEventProducer(
        kafka_producer,
        completed_topic=settings.kafka_review_completed_topic,
        failed_topic=settings.kafka_review_failed_topic,
    )
    review_consumer = ReviewRequestConsumer(kafka_consumer, pipeline, event_producer)
    consumer_task = asyncio.create_task(_run_consumer_forever(review_consumer))

    try:
        yield
    finally:
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass
        await kafka_consumer.stop()
        await kafka_producer.stop()
        await llm_client.aclose()


settings = get_settings()

app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)

app.include_router(health_router)
