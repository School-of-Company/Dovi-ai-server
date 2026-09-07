from __future__ import annotations

import json
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


class RedisLike(Protocol):
    def set(
        self, name: str, value: str, nx: bool = False, ex: int | None = None
    ) -> Awaitable[object]: ...

    def get(self, name: str) -> Awaitable[object]: ...


@dataclass
class CachedResult:
    deprecated: bool
    message: str | None


class RedisNpmDeprecationCache:
    """(패키지명, 버전)별 deprecated 조회 결과를 캐싱한다.

    같은 버전을 여러 PR이 반복해서 건드릴 때마다 registry를 다시 조회하지
    않기 위함. 버전 자체는 불변이지만 deprecated 플래그는 이미 배포된 버전에도
    나중에 붙을 수 있어(예: 보안 문제로 사후 deprecate) 무기한 캐싱은 하지 않고
    30일 TTL을 둔다(RedisNotionLinkStore와 동일 정책).
    """

    def __init__(
        self,
        redis: RedisLike,
        *,
        key_prefix: str = "ai-review:npm-deprecation:",
        ttl_seconds: int = 2592000,
    ) -> None:
        self._redis = redis
        self._key_prefix = key_prefix
        self._ttl_seconds = ttl_seconds

    def _key(self, name: str, version: str) -> str:
        return f"{self._key_prefix}{name}@{version}"

    async def get(self, name: str, version: str) -> CachedResult | None:
        value = await self._redis.get(self._key(name, version))
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode()
        if not isinstance(value, str):
            return None
        try:
            data = json.loads(value)
        except ValueError:
            logger.warning("corrupted npm deprecation cache entry, treating as miss")
            return None
        return CachedResult(
            deprecated=bool(data.get("deprecated")), message=data.get("message")
        )

    async def set(self, name: str, version: str, result: CachedResult) -> None:
        payload = json.dumps({"deprecated": result.deprecated, "message": result.message})
        await self._redis.set(self._key(name, version), payload, ex=self._ttl_seconds)
