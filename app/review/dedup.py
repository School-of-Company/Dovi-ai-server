import logging
from collections.abc import Awaitable
from typing import Protocol

from redis.asyncio import Redis

from app.core.config import Settings

logger = logging.getLogger(__name__)

_IN_PROGRESS = "in_progress"
_COMPLETED = "completed"


class RedisLike(Protocol):
    def set(
        self, name: str, value: str, nx: bool = False, ex: int | None = None
    ) -> Awaitable[object]: ...

    def get(self, name: str) -> Awaitable[object]: ...

    def delete(self, *names: str) -> Awaitable[object]: ...


class DedupStore(Protocol):
    async def try_start(self, review_job_id: str) -> bool: ...

    async def mark_completed(self, review_job_id: str) -> None: ...

    async def mark_failed(self, review_job_id: str) -> None: ...


class RedisDedupStore:
    """reviewJobId 기준 중복 처리를 막는다 (노션 19절).

    - 이미 처리 중이거나 완료된 작업이면 try_start()가 False를 반환해 skip한다.
    - 실패한 작업은 상태를 지워 재시도(redelivery) 시 다시 처리될 수 있게 한다.
    """

    def __init__(
        self,
        redis: RedisLike,
        *,
        # Redis를 Dovi-github-app과 공유하는데, 그쪽도 review:state:{reviewJobId}
        # 키를 자체 상태 추적용으로 써서 실제로 충돌한 적이 있다 (#40).
        key_prefix: str = "ai-review:dedup:",
        ttl_seconds: int = 86400,
    ) -> None:
        self._redis = redis
        self._key_prefix = key_prefix
        self._ttl_seconds = ttl_seconds

    def _key(self, review_job_id: str) -> str:
        return f"{self._key_prefix}{review_job_id}"

    async def try_start(self, review_job_id: str) -> bool:
        try:
            acquired = await self._redis.set(
                self._key(review_job_id), _IN_PROGRESS, nx=True, ex=self._ttl_seconds
            )
        except Exception:
            logger.exception(
                "redis try_start failed for reviewJobId=%s", review_job_id
            )
            raise
        return bool(acquired)

    async def mark_completed(self, review_job_id: str) -> None:
        try:
            await self._redis.set(
                self._key(review_job_id), _COMPLETED, ex=self._ttl_seconds
            )
        except Exception:
            logger.exception(
                "redis mark_completed failed for reviewJobId=%s", review_job_id
            )
            raise

    async def mark_failed(self, review_job_id: str) -> None:
        try:
            await self._redis.delete(self._key(review_job_id))
        except Exception:
            logger.exception(
                "redis mark_failed failed for reviewJobId=%s", review_job_id
            )
            raise


def create_redis_client(settings: Settings) -> Redis:
    return Redis.from_url(settings.redis_url)


def create_dedup_store(settings: Settings, redis: RedisLike) -> RedisDedupStore:
    return RedisDedupStore(redis, ttl_seconds=settings.review_dedup_ttl_seconds)
