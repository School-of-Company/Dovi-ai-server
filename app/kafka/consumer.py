import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Protocol

from pydantic import ValidationError

from app.kafka.producer import ReviewEventProducer
from app.review.dedup import DedupStore
from app.review.pipeline import ReviewPipeline
from app.review.schema import ReviewCompletedEvent, ReviewRequestedEvent

logger = logging.getLogger(__name__)


class KafkaMessage(Protocol):
    value: bytes


class MessageSource(Protocol):
    def __aiter__(self) -> AsyncIterator[KafkaMessage]: ...

    async def commit(self) -> None: ...


class ReviewRequestConsumer:
    """pr.review.requested 이벤트를 소비해 리뷰 파이프라인을 실행하고 결과를 발행한다.

    MessageSource는 aiokafka.AIOKafkaConsumer가 구조적으로 만족하는 최소 인터페이스다.
    수동 커밋을 사용한다 — 처리(발행까지) 완료 후에만 커밋해 크래시 시 메시지 유실을 막는다.
    """

    def __init__(
        self,
        source: MessageSource,
        pipeline: ReviewPipeline,
        producer: ReviewEventProducer,
        dedup: DedupStore,
    ) -> None:
        self._source = source
        self._pipeline = pipeline
        self._producer = producer
        self._dedup = dedup

    async def run(self, shutdown: asyncio.Event | None = None) -> None:
        """shutdown이 주어지면, 처리 중이던 메시지를 커밋까지 마친 뒤 다음 메시지를
        가져오지 않고 반환한다 (graceful shutdown — app/main.py의 lifespan 참고)."""
        async for message in self._source:
            await self.handle(message.value)
            await self._source.commit()
            if shutdown is not None and shutdown.is_set():
                return

    async def handle(self, raw: bytes) -> None:
        try:
            event = ReviewRequestedEvent.model_validate_json(raw)
        except ValidationError:
            logger.exception("invalid ReviewRequestedEvent payload, skipping")
            return

        if not await self._dedup.try_start(event.review_job_id):
            logger.info(
                "skipping duplicate or in-progress reviewJobId=%s", event.review_job_id
            )
            return

        try:
            result = await self._pipeline.run(event)
            if isinstance(result, ReviewCompletedEvent):
                await self._producer.publish_completed(result)
                await self._dedup.mark_completed(event.review_job_id)
            else:
                await self._producer.publish_failed(result)
                await self._dedup.mark_failed(event.review_job_id)
        except asyncio.CancelledError:
            # graceful shutdown 유예시간을 넘겨 강제 취소된 경우 — 락을 풀어
            # 재전달된 메시지를 새 인스턴스가 TTL을 기다리지 않고 재처리하게 한다.
            await self._dedup.mark_failed(event.review_job_id)
            raise
