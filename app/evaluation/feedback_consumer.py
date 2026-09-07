import asyncio
import logging

from pydantic import ValidationError

from app.evaluation.repository import EvaluationRepository
from app.evaluation.schema import ReviewFeedbackEvent
from app.kafka.consumer import MessageSource

logger = logging.getLogger(__name__)


class ReviewFeedbackConsumer:
    """pr.comment.reflected 이벤트를 소비해 review_feedback을 갱신한다.

    발행 측(GitHub App 팀)이 아직 구현되지 않아 실제 프로덕션 트래픽은
    없다 — consumer 로직만 미리 준비해둔다. dedup은 두지 않는다
    (upsert_feedback이 멱등이라 재전달되어도 안전).
    """

    def __init__(self, source: MessageSource, repository: EvaluationRepository) -> None:
        self._source = source
        self._repository = repository

    async def run(self, shutdown: asyncio.Event | None = None) -> None:
        async for message in self._source:
            await self.handle(message.value)
            await self._source.commit()
            if shutdown is not None and shutdown.is_set():
                return

    async def handle(self, raw: bytes) -> None:
        try:
            event = ReviewFeedbackEvent.model_validate_json(raw)
        except ValidationError:
            logger.exception("invalid ReviewFeedbackEvent payload, skipping")
            return
        await self._repository.upsert_feedback(event)
