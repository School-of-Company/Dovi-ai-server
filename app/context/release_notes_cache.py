from __future__ import annotations

import json
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


class RedisLike(Protocol):
    def set(self, name: str, value: str, ex: int | None = None) -> Awaitable[object]: ...

    def get(self, name: str) -> Awaitable[object]: ...


@dataclass
class CachedReleaseNotes:
    notes: str | None  # None이면 "이 버전엔 릴리즈노트가 없음을 확인했다"는 뜻


class RedisReleaseNotesCache:
    """(owner/repo, version)별 릴리즈 노트 조회 결과를 캐싱한다.

    "찾음"과 "이 버전엔 릴리즈노트가 없음"을 둘 다 캐싱한다 — 호출자
    (OfficialDocsWorkflow)가 GithubReleaseClient의 ok=True 결과만 캐싱하므로
    (일시적 조회 실패는 캐싱 대상에서 이미 제외됨), 여기 저장되는 값은 항상
    해당 버전에 대해 불변인 사실이다. 30일 TTL(RedisNpmDeprecationCache와
    동일 정책 — 버전 자체는 불변이지만 무기한 캐싱은 하지 않는다).
    """

    def __init__(
        self,
        redis: RedisLike,
        *,
        key_prefix: str = "ai-review:release-notes:",
        ttl_seconds: int = 2592000,
    ) -> None:
        self._redis = redis
        self._key_prefix = key_prefix
        self._ttl_seconds = ttl_seconds

    def _key(self, owner_repo: str, version: str) -> str:
        return f"{self._key_prefix}{owner_repo}@{version}"

    async def get(self, owner_repo: str, version: str) -> CachedReleaseNotes | None:
        value = await self._redis.get(self._key(owner_repo, version))
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode()
        if not isinstance(value, str):
            return None
        try:
            data = json.loads(value)
        except ValueError:
            logger.warning("corrupted release notes cache entry, treating as miss")
            return None
        return CachedReleaseNotes(notes=data.get("notes"))

    async def set(self, owner_repo: str, version: str, notes: str | None) -> None:
        payload = json.dumps({"notes": notes})
        await self._redis.set(self._key(owner_repo, version), payload, ex=self._ttl_seconds)
