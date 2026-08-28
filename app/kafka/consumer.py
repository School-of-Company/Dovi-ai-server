import logging
from collections.abc import AsyncIterator
from typing import Protocol

from pydantic import ValidationError

from app.kafka.producer import ReviewEventProducer
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
    ) -> None:
        self._source = source
        self._pipeline = pipeline
        self._producer = producer

    async def run(self) -> None:
        async for message in self._source:
            await self.handle(message.value)
            await self._source.commit()

    async def handle(self, raw: bytes) -> None:
        try:
            event = ReviewRequestedEvent.model_validate_json(raw)
        except ValidationError:
            logger.exception("invalid ReviewRequestedEvent payload, skipping")
            return

        result = await self._pipeline.run(event)

        if isinstance(result, ReviewCompletedEvent):
            await self._producer.publish_completed(result)
        else:
            await self._producer.publish_failed(result)
