from app.review.dedup import RedisDedupStore


class FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def set(
        self, name: str, value: str, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if nx and name in self._store:
            return None
        self._store[name] = value
        return True

    async def get(self, name: str) -> str | None:
        return self._store.get(name)

    async def delete(self, *names: str) -> int:
        count = 0
        for name in names:
            if name in self._store:
                del self._store[name]
                count += 1
        return count


def _store(redis: FakeRedis | None = None) -> RedisDedupStore:
    return RedisDedupStore(redis or FakeRedis(), ttl_seconds=60)


async def test_try_start_succeeds_for_new_job() -> None:
    assert await _store().try_start("1:2:sha") is True


async def test_try_start_fails_for_in_progress_job() -> None:
    store = _store()
    await store.try_start("1:2:sha")

    assert await store.try_start("1:2:sha") is False


async def test_try_start_fails_for_completed_job() -> None:
    store = _store()
    await store.try_start("1:2:sha")
    await store.mark_completed("1:2:sha")

    assert await store.try_start("1:2:sha") is False


async def test_mark_failed_allows_retry() -> None:
    store = _store()
    await store.try_start("1:2:sha")
    await store.mark_failed("1:2:sha")

    assert await store.try_start("1:2:sha") is True


async def test_different_jobs_are_independent() -> None:
    store = _store()
    await store.try_start("1:2:sha")

    assert await store.try_start("9:9:other") is True
