import asyncio
import logging
from collections.abc import Awaitable
from typing import Protocol

from pydantic import ValidationError

from app.kafka.consumer import MessageSource
from app.review.schema import ReviewRequestedEvent

logger = logging.getLogger(__name__)


class RedisHashLike(Protocol):
    def incr(self, name: str) -> Awaitable[object]: ...

    def hsetnx(self, name: str, key: str, value: str) -> Awaitable[object]: ...

    def hgetall(self, name: str) -> Awaitable[object]: ...

    def expire(self, name: str, time: int) -> Awaitable[object]: ...


def _is_head_request(event: ReviewRequestedEvent) -> bool:
    # `/dovi review`·멘션 재요청은 reviewJobId에 접미사(`_c{commentId}`)가 붙고 같은 head에서
    # 의도적으로 여러 번 돌 수 있으므로, head 추적에도 건너뛰기에도 쓰지 않는다.
    return event.review_job_id == f"{event.repository_id}:{event.pr_number}:{event.head_sha}"


class PrHeadTracker:
    """PR별로 요청이 도착한 head를 순서대로 기록해, 더 새로운 head의 요청이 이미 큐에
    들어온 오래된 요청을 알아낸다.

    리뷰 컨슈머는 메시지를 한 건씩만 꺼내서 큐 뒤쪽을 볼 수 없으므로, 같은 토픽을 별도
    그룹으로 읽는 수신 전용 컨슈머(PrHeadTrackerConsumer)가 도착 순서를 기록한다.
    기록이 아직 없으면(수신 컨슈머가 뒤처졌거나 꺼져 있으면) 건너뛰지 않는다.
    """

    def __init__(
        self,
        redis: RedisHashLike,
        *,
        key_prefix: str = "ai-review:pr-heads:",
        ttl_seconds: int = 86400,
    ) -> None:
        self._redis = redis
        self._key_prefix = key_prefix
        self._ttl_seconds = ttl_seconds

    def _key(self, event: ReviewRequestedEvent) -> str:
        return f"{self._key_prefix}{event.repository_id}:{event.pr_number}"

    async def record(self, event: ReviewRequestedEvent) -> None:
        if not _is_head_request(event):
            return
        key = self._key(event)
        seq = await self._redis.incr(f"{self._key_prefix}seq")
        await self._redis.hsetnx(key, event.head_sha, str(seq))
        await self._redis.expire(key, self._ttl_seconds)

    async def is_superseded(self, event: ReviewRequestedEvent) -> bool:
        if not _is_head_request(event):
            return False
        try:
            raw = await self._redis.hgetall(self._key(event))
        except Exception:
            logger.exception(
                "redis pr-heads lookup failed reviewJobId=%s", event.review_job_id
            )
            return False
        heads = {_text(k): int(_text(v)) for k, v in dict(raw).items()}  # type: ignore[call-overload]
        own = heads.get(event.head_sha)
        if own is None:
            return False
        return any(seq > own for seq in heads.values())


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class PrHeadTrackerConsumer:
    def __init__(self, source: MessageSource, tracker: PrHeadTracker) -> None:
        self._source = source
        self._tracker = tracker

    async def run(self, shutdown: asyncio.Event | None = None) -> None:
        async for message in self._source:
            await self.handle(message.value)
            await self._source.commit()
            if shutdown is not None and shutdown.is_set():
                return

    async def handle(self, raw: bytes) -> None:
        try:
            event = ReviewRequestedEvent.model_validate_json(raw)
        except ValidationError:
            return
        try:
            await self._tracker.record(event)
        except Exception:
            logger.exception(
                "failed to record pr head reviewJobId=%s", event.review_job_id
            )
