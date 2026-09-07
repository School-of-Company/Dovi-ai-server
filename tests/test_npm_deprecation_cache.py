from app.context.npm_deprecation_cache import CachedResult, RedisNpmDeprecationCache


class FakeRedis:
    """실제 redis.asyncio.Redis(decode_responses 미설정)는 bytes를 반환하므로,
    그 경계를 테스트가 실제로 검증하도록 bytes로 저장/반환한다."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def set(self, name: str, value: str, nx: bool = False, ex: int | None = None) -> object:
        self.store[name] = value.encode()
        return True

    async def get(self, name: str) -> object:
        return self.store.get(name)


async def test_set_then_get_deprecated_result() -> None:
    cache = RedisNpmDeprecationCache(FakeRedis())
    await cache.set("axios", "1.20.0", CachedResult(deprecated=True, message="use fetch"))

    result = await cache.get("axios", "1.20.0")

    assert result == CachedResult(deprecated=True, message="use fetch")


async def test_set_then_get_non_deprecated_result() -> None:
    cache = RedisNpmDeprecationCache(FakeRedis())
    await cache.set("axios", "1.20.0", CachedResult(deprecated=False, message=None))

    result = await cache.get("axios", "1.20.0")

    assert result == CachedResult(deprecated=False, message=None)


async def test_get_returns_none_when_not_cached() -> None:
    cache = RedisNpmDeprecationCache(FakeRedis())
    assert await cache.get("axios", "1.20.0") is None


async def test_key_is_namespaced_by_name_and_version() -> None:
    redis = FakeRedis()
    cache = RedisNpmDeprecationCache(redis)
    await cache.set("@tanstack/query-core", "5.102.8", CachedResult(deprecated=False, message=None))

    assert "ai-review:npm-deprecation:@tanstack/query-core@5.102.8" in redis.store


async def test_get_returns_none_for_corrupted_cache_entry() -> None:
    # 캐시에 잘못된 JSON이 들어있어도 예외를 던지지 않고 캐시 미스로 취급한다.
    redis = FakeRedis()
    cache = RedisNpmDeprecationCache(redis)
    redis.store[cache._key("axios", "1.20.0")] = b"not valid json"

    assert await cache.get("axios", "1.20.0") is None
