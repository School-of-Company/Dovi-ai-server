from app.context.release_notes_cache import CachedReleaseNotes, RedisReleaseNotesCache


class FakeRedis:
    """실제 redis.asyncio.Redis(decode_responses 미설정)는 bytes를 반환하므로,
    그 경계를 테스트가 실제로 검증하도록 bytes로 저장/반환한다."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def set(self, name: str, value: str, ex: int | None = None) -> object:
        self.store[name] = value.encode()
        return True

    async def get(self, name: str) -> object:
        return self.store.get(name)


async def test_set_then_get_found_notes() -> None:
    cache = RedisReleaseNotesCache(FakeRedis())
    await cache.set("axios/axios", "1.20.0", "release notes text")

    result = await cache.get("axios/axios", "1.20.0")

    assert result == CachedReleaseNotes(notes="release notes text")


async def test_set_then_get_confirmed_absent_notes() -> None:
    cache = RedisReleaseNotesCache(FakeRedis())
    await cache.set("axios/axios", "1.20.0", None)

    result = await cache.get("axios/axios", "1.20.0")

    assert result == CachedReleaseNotes(notes=None)


async def test_get_returns_none_when_not_cached() -> None:
    cache = RedisReleaseNotesCache(FakeRedis())
    assert await cache.get("axios/axios", "1.20.0") is None


async def test_key_is_namespaced_by_owner_repo_and_version() -> None:
    redis = FakeRedis()
    cache = RedisReleaseNotesCache(redis)
    await cache.set("axios/axios", "1.20.0", "notes")

    assert "ai-review:release-notes:axios/axios@1.20.0" in redis.store


async def test_get_returns_none_for_corrupted_cache_entry() -> None:
    redis = FakeRedis()
    cache = RedisReleaseNotesCache(redis)
    redis.store[cache._key("axios/axios", "1.20.0")] = b"not valid json"

    assert await cache.get("axios/axios", "1.20.0") is None
