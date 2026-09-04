from app.context.api_spec_link_store import RedisNotionLinkStore


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, name: str, value: str, nx: bool = False, ex: int | None = None) -> object:
        self.store[name] = value
        return True

    async def get(self, name: str) -> object:
        return self.store.get(name)

    async def delete(self, *names: str) -> object:
        for name in names:
            self.store.pop(name, None)
        return len(names)

    async def keys(self, pattern: str) -> object:
        import fnmatch
        return [k for k in self.store.keys() if fnmatch.fnmatch(k, pattern)]


async def test_save_and_get_link() -> None:
    redis = FakeRedis()
    store = RedisNotionLinkStore(redis)

    await store.save(repository_id=42, notion_database_url="https://notion.so/abc")

    assert await store.get(repository_id=42) == "https://notion.so/abc"


async def test_get_returns_none_when_not_saved() -> None:
    store = RedisNotionLinkStore(FakeRedis())
    assert await store.get(repository_id=999) is None


async def test_list_all_returns_every_saved_repository() -> None:
    redis = FakeRedis()
    store = RedisNotionLinkStore(redis)
    await store.save(repository_id=1, notion_database_url="https://notion.so/a")
    await store.save(repository_id=2, notion_database_url="https://notion.so/b")

    result = await store.list_all()

    assert dict(result) == {1: "https://notion.so/a", 2: "https://notion.so/b"}


def test_key_prefix_is_namespaced_to_avoid_collision_with_github_app() -> None:
    # Redis를 Dovi-github-app과 공유하다가 review:state:{reviewJobId} 키 충돌(#40)이
    # 났던 전례가 있어서, prefix를 명시적으로 고정해 회귀를 막는다.
    store = RedisNotionLinkStore(FakeRedis())
    assert store._key(42) == "ai-review:notion-api-spec-link:42"
