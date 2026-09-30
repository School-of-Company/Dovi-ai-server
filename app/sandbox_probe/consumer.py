import asyncio
import logging
from collections.abc import Awaitable
from typing import Protocol

from pydantic import ValidationError

from app.kafka.consumer import MessageSource
from app.review.dedup import DedupStore
from app.sandbox_probe.schema import (
    SandboxProbeCompletedEvent,
    SandboxProbeRequestedEvent,
)

logger = logging.getLogger(__name__)

_ATTEMPTS_KEY_PREFIX = "ai-review:sandbox-probe-attempts:"
_ATTEMPTS_TTL_SECONDS = 86400


class ProbeRunner(Protocol):
    async def run(self, event: SandboxProbeRequestedEvent) -> SandboxProbeCompletedEvent: ...


class ProbeEventPublisher(Protocol):
    async def publish_completed(self, event: SandboxProbeCompletedEvent) -> None: ...


class AttemptCounter(Protocol):
    def incr(self, name: str) -> Awaitable[int]: ...

    def expire(self, name: str, time: int) -> Awaitable[object]: ...


class SandboxProbeConsumer:
    """pr.sandbox.probe.requested 이벤트를 소비해 샌드박스 프로브를 실행하고 결과를 발행한다.

    CommentAnswerConsumer와 같은 수동 커밋 / graceful shutdown / dedup 규약을 따른다.
    프로세스가 도중에 죽어도 남는 Redis 카운터로 시도 횟수를 세어, 워커를 계속 죽이는
    잡(poison job)은 한도를 넘으면 실행하지 않고 inconclusive로 마감한다.
    """

    def __init__(
        self,
        source: MessageSource,
        runner: ProbeRunner,
        producer: ProbeEventPublisher,
        dedup: DedupStore,
        counter: AttemptCounter,
        *,
        max_attempts: int = 2,
    ) -> None:
        self._source = source
        self._runner = runner
        self._producer = producer
        self._dedup = dedup
        self._counter = counter
        self._max_attempts = max_attempts

    async def run(self, shutdown: asyncio.Event | None = None) -> None:
        async for message in self._source:
            await self.handle(message.value)
            await self._source.commit()
            if shutdown is not None and shutdown.is_set():
                return

    async def handle(self, raw: bytes) -> None:
        try:
            event = SandboxProbeRequestedEvent.model_validate_json(raw)
        except ValidationError:
            logger.exception("invalid SandboxProbeRequestedEvent payload, skipping")
            return

        if not await self._dedup.try_start(event.review_job_id):
            logger.info(
                "skipping duplicate or in-progress reviewJobId=%s", event.review_job_id
            )
            return

        try:
            result = await self._execute(event)
            await self._producer.publish_completed(result)
            await self._dedup.mark_completed(event.review_job_id)
        except asyncio.CancelledError:
            await self._dedup.mark_failed(event.review_job_id)
            raise
        except Exception:
            await self._dedup.mark_failed(event.review_job_id)
            raise

    async def _execute(self, event: SandboxProbeRequestedEvent) -> SandboxProbeCompletedEvent:
        key = f"{_ATTEMPTS_KEY_PREFIX}{event.review_job_id}"
        attempts = await self._counter.incr(key)
        if attempts == 1:
            await self._counter.expire(key, _ATTEMPTS_TTL_SECONDS)
        if attempts > self._max_attempts:
            logger.warning(
                "sandbox probe attempts exceeded reviewJobId=%s attempts=%d",
                event.review_job_id,
                attempts,
            )
            return SandboxProbeCompletedEvent(
                review_job_id=event.review_job_id,
                repository_id=event.repository_id,
                pr_number=event.pr_number,
                head_sha=event.head_sha,
                status="inconclusive",
                evidence="반복 실패로 샌드박스 검증을 중단했습니다.",
            )
        return await self._runner.run(event)
