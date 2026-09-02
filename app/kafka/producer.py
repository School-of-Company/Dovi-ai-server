import logging
from typing import Any, Protocol

from app.comment_answer.schema import CommentAnswerCompletedEvent, CommentAnswerFailedEvent
from app.review.schema import ReviewCompletedEvent, ReviewFailedEvent

logger = logging.getLogger(__name__)


class MessageSender(Protocol):
    async def send_and_wait(
        self, topic: str, value: bytes, key: bytes | None = None
    ) -> Any: ...


class EventPublisher(Protocol):
    """ReviewRequestConsumer가 필요로 하는 최소 인터페이스. ReviewEventProducer가
    구조적으로 만족하며, 테스트에서는 fake로 대체할 수 있다."""

    async def publish_completed(self, event: ReviewCompletedEvent) -> None: ...

    async def publish_failed(self, event: ReviewFailedEvent) -> None: ...


class ReviewEventProducer:
    """review 완료/실패 이벤트를 Kafka에 발행한다.

    MessageSender는 aiokafka.AIOKafkaProducer가 구조적으로 만족하는 최소 인터페이스다.
    """

    def __init__(
        self,
        sender: MessageSender,
        *,
        completed_topic: str,
        failed_topic: str,
    ) -> None:
        self._sender = sender
        self._completed_topic = completed_topic
        self._failed_topic = failed_topic

    async def publish_completed(self, event: ReviewCompletedEvent) -> None:
        await self._send(self._completed_topic, event)

    async def publish_failed(self, event: ReviewFailedEvent) -> None:
        await self._send(self._failed_topic, event)

    async def _send(
        self, topic: str, event: ReviewCompletedEvent | ReviewFailedEvent
    ) -> None:
        key = event.review_job_id.encode("utf-8")
        value = event.model_dump_json(by_alias=True).encode("utf-8")
        try:
            await self._sender.send_and_wait(topic, value=value, key=key)
        except Exception:
            logger.exception(
                "failed to publish reviewJobId=%s topic=%s",
                event.review_job_id,
                topic,
            )
            raise
        logger.info(
            "published reviewJobId=%s topic=%s", event.review_job_id, topic
        )


class CommentAnswerEventPublisher(Protocol):
    """CommentAnswerConsumer가 필요로 하는 최소 인터페이스."""

    async def publish_completed(self, event: CommentAnswerCompletedEvent) -> None: ...

    async def publish_failed(self, event: CommentAnswerFailedEvent) -> None: ...


class CommentAnswerEventProducer:
    """코멘트 스레드 Q&A 답변/실패 이벤트를 Kafka에 발행한다."""

    def __init__(
        self,
        sender: MessageSender,
        *,
        completed_topic: str,
        failed_topic: str,
    ) -> None:
        self._sender = sender
        self._completed_topic = completed_topic
        self._failed_topic = failed_topic

    async def publish_completed(self, event: CommentAnswerCompletedEvent) -> None:
        await self._send(self._completed_topic, event)

    async def publish_failed(self, event: CommentAnswerFailedEvent) -> None:
        await self._send(self._failed_topic, event)

    async def _send(
        self, topic: str, event: CommentAnswerCompletedEvent | CommentAnswerFailedEvent
    ) -> None:
        key = event.comment_job_id.encode("utf-8")
        value = event.model_dump_json(by_alias=True).encode("utf-8")
        try:
            await self._sender.send_and_wait(topic, value=value, key=key)
        except Exception:
            logger.exception(
                "failed to publish commentJobId=%s topic=%s",
                event.comment_job_id,
                topic,
            )
            raise
        logger.info(
            "published commentJobId=%s topic=%s", event.comment_job_id, topic
        )
