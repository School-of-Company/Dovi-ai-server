import asyncio
import logging

from pydantic import ValidationError

from app.comment_answer.pipeline import CommentAnswerPipeline
from app.comment_answer.schema import (
    CommentAnswerCompletedEvent,
    CommentAnswerRequestedEvent,
)
from app.kafka.consumer import MessageSource
from app.kafka.producer import CommentAnswerEventPublisher
from app.review.dedup import DedupStore

logger = logging.getLogger(__name__)


class CommentAnswerConsumer:
    """pr.comment.answer.requested 이벤트를 소비해 답변을 생성하고 발행한다.

    ReviewRequestConsumer와 동일한 graceful shutdown / dedup / 수동 커밋 규약을
    따른다 (app/kafka/consumer.py 참고).
    """

    def __init__(
        self,
        source: MessageSource,
        pipeline: CommentAnswerPipeline,
        producer: CommentAnswerEventPublisher,
        dedup: DedupStore,
    ) -> None:
        self._source = source
        self._pipeline = pipeline
        self._producer = producer
        self._dedup = dedup

    async def run(self, shutdown: asyncio.Event | None = None) -> None:
        async for message in self._source:
            await self.handle(message.value)
            await self._source.commit()
            if shutdown is not None and shutdown.is_set():
                return

    async def handle(self, raw: bytes) -> None:
        try:
            event = CommentAnswerRequestedEvent.model_validate_json(raw)
        except ValidationError:
            logger.exception("invalid CommentAnswerRequestedEvent payload, skipping")
            return

        if not await self._dedup.try_start(event.comment_job_id):
            logger.info(
                "skipping duplicate or in-progress commentJobId=%s",
                event.comment_job_id,
            )
            return

        try:
            result = await self._pipeline.run(event)
            if isinstance(result, CommentAnswerCompletedEvent):
                await self._producer.publish_completed(result)
                await self._dedup.mark_completed(event.comment_job_id)
            else:
                await self._producer.publish_failed(result)
                await self._dedup.mark_failed(event.comment_job_id)
        except asyncio.CancelledError:
            # graceful shutdown 유예시간을 넘겨 강제 취소된 경우 — 락을 풀어
            # 재전달된 메시지를 새 인스턴스가 TTL을 기다리지 않고 재처리하게 한다.
            await self._dedup.mark_failed(event.comment_job_id)
            raise
