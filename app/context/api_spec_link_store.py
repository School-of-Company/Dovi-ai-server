from __future__ import annotations

import logging
from collections.abc import Awaitable
from typing import Protocol, cast

logger = logging.getLogger(__name__)


class RedisLike(Protocol):
    def set(
        self, name: str, value: str, nx: bool = False, ex: int | None = None
    ) -> Awaitable[object]: ...

    def get(self, name: str) -> Awaitable[object]: ...

    def keys(self, pattern: str) -> Awaitable[object]: ...


class NotionLinkStore(Protocol):
    async def save(self, *, repository_id: int, notion_database_url: str) -> None: ...
    async def get(self, *, repository_id: int) -> str | None: ...
    async def list_all(self) -> list[tuple[int, str]]: ...


class RedisNotionLinkStore:
    """레포별로 발견된 DOVI.md의 Notion API 명세 DB 링크를 저장한다.

    sync_api_spec.py가 이 목록을 읽어 주기적으로 Notion을 동기화한다 — PR 리뷰
    시점엔 Notion을 직접 조회하지 않는다는 설계 원칙(7.3절)을 지키기 위함이다.
    """

    def __init__(
        self,
        redis: RedisLike,
        *,
        key_prefix: str = "ai-review:notion-api-spec-link:",
        # DOVI.md에서 Notion 링크가 제거된 뒤에도 Redis에 무기한 남아 사후 sync
        # cron이 죽은 DB를 계속 조회하는 것을 막기 위한 TTL. 링크가 살아있는 한
        # _maybe_save_notion_link가 매 PR 리뷰마다 save()를 다시 호출해 TTL을
        # 자연스럽게 갱신하므로, 실사용 중인 링크는 만료되지 않는다.
        ttl_seconds: int = 2592000,
    ) -> None:
        self._redis = redis
        self._key_prefix = key_prefix
        self._ttl_seconds = ttl_seconds

    def _key(self, repository_id: int) -> str:
        return f"{self._key_prefix}{repository_id}"

    async def save(self, *, repository_id: int, notion_database_url: str) -> None:
        await self._redis.set(
            self._key(repository_id), notion_database_url, ex=self._ttl_seconds
        )

    async def get(self, *, repository_id: int) -> str | None:
        value = await self._redis.get(self._key(repository_id))
        if isinstance(value, bytes):
            return value.decode()
        return value if isinstance(value, str) else None

    async def list_all(self) -> list[tuple[int, str]]:
        keys = await self._redis.keys(f"{self._key_prefix}*")
        result: list[tuple[int, str]] = []
        keys_list = cast(list[bytes | str], keys)
        for key in keys_list:
            key_str = key if isinstance(key, str) else key.decode()
            suffix = key_str.removeprefix(self._key_prefix)
            try:
                repository_id = int(suffix)
            except ValueError:
                logger.warning("malformed notion link key skipped key=%s", key_str)
                continue
            url = await self.get(repository_id=repository_id)
            if url is not None:
                result.append((repository_id, url))
        return result
