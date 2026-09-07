from app.context.dependency_resolver import DependencyResolver
from app.context.npm_deprecation_cache import CachedResult
from app.review.schema import ChangedFile

_AXIOS_BUMP_PATCH = """\
@@ -8472,9 +8473,9 @@
       }
     },
     "node_modules/axios": {
-      "version": "1.19.0",
-      "resolved": "https://registry.npmjs.org/axios/-/axios-1.19.0.tgz",
-      "integrity": "sha512-old==",
+      "version": "1.20.0",
+      "resolved": "https://registry.npmjs.org/axios/-/axios-1.20.0.tgz",
+      "integrity": "sha512-new==",
       "license": "MIT",
       "dependencies": {
         "follow-redirects": "^1.16.0",
"""


class FakeRegistryClient:
    def __init__(self, responses: dict[tuple[str, str], str | None]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    async def get_deprecation_message(self, name: str, version: str) -> str | None:
        self.calls.append((name, version))
        return self._responses.get((name, version))


class RaisingRegistryClient:
    async def get_deprecation_message(self, name: str, version: str) -> str | None:
        raise RuntimeError("network exploded")


class FakeCache:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], CachedResult] = {}

    async def get(self, name: str, version: str) -> CachedResult | None:
        return self.store.get((name, version))

    async def set(self, name: str, version: str, result: CachedResult) -> None:
        self.store[(name, version)] = result


class RaisingGetCache:
    async def get(self, name: str, version: str) -> CachedResult | None:
        raise RuntimeError("cache get exploded")

    async def set(self, name: str, version: str, result: CachedResult) -> None:
        pass


class RaisingSetCache:
    async def get(self, name: str, version: str) -> CachedResult | None:
        return None

    async def set(self, name: str, version: str, result: CachedResult) -> None:
        raise RuntimeError("cache set exploded")


def _lockfile_change(patch: str) -> ChangedFile:
    return ChangedFile(file_path="package-lock.json", status="modified", patch=patch)


async def test_creates_finding_for_deprecated_dependency() -> None:
    registry = FakeRegistryClient({("axios", "1.20.0"): "axios 1.x is deprecated, use fetch"})
    resolver = DependencyResolver(registry, FakeCache())

    findings = await resolver.find_deprecated_dependencies([_lockfile_change(_AXIOS_BUMP_PATCH)])

    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "minor"
    assert finding.confidence == 1.0
    assert finding.file_path == "package-lock.json"
    assert finding.line == 8476
    assert "axios" in finding.title
    assert "axios 1.x is deprecated, use fetch" in finding.message
    assert finding.evidence == ['+      "version": "1.20.0",']


async def test_no_finding_when_not_deprecated() -> None:
    registry = FakeRegistryClient({("axios", "1.20.0"): None})
    resolver = DependencyResolver(registry, FakeCache())

    findings = await resolver.find_deprecated_dependencies([_lockfile_change(_AXIOS_BUMP_PATCH)])

    assert findings == []


async def test_uses_cache_before_calling_registry() -> None:
    cache = FakeCache()
    await cache.set("axios", "1.20.0", CachedResult(deprecated=True, message="cached msg"))
    registry = FakeRegistryClient({})
    resolver = DependencyResolver(registry, cache)

    findings = await resolver.find_deprecated_dependencies([_lockfile_change(_AXIOS_BUMP_PATCH)])

    assert len(findings) == 1
    assert "cached msg" in findings[0].message
    assert registry.calls == []  # 캐시 hit이라 registry를 아예 안 불렀다


async def test_writes_result_back_to_cache_on_miss() -> None:
    cache = FakeCache()
    registry = FakeRegistryClient({("axios", "1.20.0"): "deprecated msg"})
    resolver = DependencyResolver(registry, cache)

    await resolver.find_deprecated_dependencies([_lockfile_change(_AXIOS_BUMP_PATCH)])

    assert cache.store[("axios", "1.20.0")] == CachedResult(
        deprecated=True, message="deprecated msg"
    )


async def test_ignores_non_lockfile_changed_files() -> None:
    resolver = DependencyResolver(FakeRegistryClient({}), FakeCache())
    other_file = ChangedFile(file_path="src/app.ts", status="modified", patch="+ console.log(1)")

    findings = await resolver.find_deprecated_dependencies([other_file])

    assert findings == []


async def test_best_effort_swallows_registry_exceptions() -> None:
    resolver = DependencyResolver(RaisingRegistryClient(), FakeCache())

    findings = await resolver.find_deprecated_dependencies([_lockfile_change(_AXIOS_BUMP_PATCH)])

    assert findings == []


async def test_falls_back_to_registry_when_cache_get_raises() -> None:
    registry = FakeRegistryClient({("axios", "1.20.0"): "axios 1.x is deprecated, use fetch"})
    resolver = DependencyResolver(registry, RaisingGetCache())

    findings = await resolver.find_deprecated_dependencies([_lockfile_change(_AXIOS_BUMP_PATCH)])

    assert len(findings) == 1
    assert "axios 1.x is deprecated, use fetch" in findings[0].message
    # 캐시 read 실패는 미스로 취급하고 registry로 폴백
    assert registry.calls == [("axios", "1.20.0")]


async def test_keeps_finding_when_cache_set_raises() -> None:
    registry = FakeRegistryClient({("axios", "1.20.0"): "axios 1.x is deprecated, use fetch"})
    resolver = DependencyResolver(registry, RaisingSetCache())

    findings = await resolver.find_deprecated_dependencies([_lockfile_change(_AXIOS_BUMP_PATCH)])

    assert len(findings) == 1
    assert "axios 1.x is deprecated, use fetch" in findings[0].message
