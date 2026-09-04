from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol, cast


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
    ) -> None:
        self._redis = redis
        self._key_prefix = key_prefix

    def _key(self, repository_id: int) -> str:
        return f"{self._key_prefix}{repository_id}"

    async def save(self, *, repository_id: int, notion_database_url: str) -> None:
        await self._redis.set(self._key(repository_id), notion_database_url)

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
            repository_id = int(key_str.removeprefix(self._key_prefix))
            url = await self.get(repository_id=repository_id)
            if url is not None:
                result.append((repository_id, url))
        return result
