import pytest

from app.context.dependency_resolver import DependencyResolver
from app.context.maven_central_client import RelocationLookupResult
from app.context.npm_deprecation_cache import CachedResult
from app.context.npm_lockfile_diff import DependencyChange
from app.context.npm_registry_client import DeprecationLookupResult
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

_DUPLICATE_PAIR_PATCH = """\
@@ -100,6 +100,6 @@
       }
     },
     "node_modules/picomatch": {
-      "version": "3.0.0",
+      "version": "4.0.7",
       "license": "MIT",
       "engines": {
@@ -200,6 +200,6 @@
       }
     },
     "node_modules/lint-staged/node_modules/picomatch": {
-      "version": "3.0.0",
+      "version": "4.0.7",
       "license": "MIT",
       "engines": {
"""


class FakeRegistryClient:
    def __init__(self, responses: dict[tuple[str, str], str | None]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    async def check_deprecation(self, name: str, version: str) -> DeprecationLookupResult:
        self.calls.append((name, version))
        return DeprecationLookupResult(ok=True, message=self._responses.get((name, version)))


class NotOkRegistryClient:
    """네트워크 예외 없이, 조회 자체가 실패했음(ok=False)을 반환하는 더블."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def check_deprecation(self, name: str, version: str) -> DeprecationLookupResult:
        self.calls.append((name, version))
        return DeprecationLookupResult(ok=False, message=None)


class RaisingRegistryClient:
    async def check_deprecation(self, name: str, version: str) -> DeprecationLookupResult:
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


_GSON_BUMP_PATCH = """\
@@ -30,7 +30,7 @@
 dependencies {
     // JSON & Validation
-    implementation("com.google.code.gson:gson:2.8.9")
+    implementation("com.google.code.gson:gson:2.13.1")
"""


def _gradle_change(patch: str, file_name: str = "build.gradle.kts") -> ChangedFile:
    return ChangedFile(file_path=file_name, status="modified", patch=patch)


class FakeMavenClient:
    def __init__(self, responses: dict[tuple[str, str], str | None]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    async def check_relocation(self, name: str, version: str) -> RelocationLookupResult:
        self.calls.append((name, version))
        return RelocationLookupResult(ok=True, relocated_to=self._responses.get((name, version)))


class RaisingMavenClient:
    async def check_relocation(self, name: str, version: str) -> RelocationLookupResult:
        raise RuntimeError("network exploded")


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


async def test_registry_failure_is_not_cached() -> None:
    # 일시적 registry 실패(ok=False, 예외가 아니라 정상적으로 반환된 실패 응답)는
    # "deprecated 아님"으로 30일 캐싱되면 안 된다 — 캐시 저장소가 비어 있어야 한다.
    cache = FakeCache()
    registry = NotOkRegistryClient()
    resolver = DependencyResolver(registry, cache)

    findings = await resolver.find_deprecated_dependencies([_lockfile_change(_AXIOS_BUMP_PATCH)])

    assert findings == []
    assert cache.store == {}


async def test_deduplicates_same_name_version_pair_across_lockfile() -> None:
    # 같은 (name, version)이 hoisted top-level 복사본과 nested transitive
    # 복사본으로 한 patch 안에 두 번 등장해도, finding과 registry 호출은 한 번만
    # 발생해야 한다.
    registry = FakeRegistryClient({("picomatch", "4.0.7"): "deprecated msg"})
    resolver = DependencyResolver(registry, FakeCache())

    findings = await resolver.find_deprecated_dependencies(
        [_lockfile_change(_DUPLICATE_PAIR_PATCH)]
    )

    assert len(findings) == 1
    assert registry.calls == [("picomatch", "4.0.7")]


async def test_caps_number_of_distinct_pairs_checked_per_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changes = [
        DependencyChange(
            name=f"pkg-{i}",
            version="1.0.0",
            evidence_line=f'+      "version": "1.0.0", // {i}',
            new_file_line=i,
        )
        for i in range(75)
    ]
    monkeypatch.setattr(
        "app.context.dependency_resolver.extract_dependency_changes", lambda patch: changes
    )
    registry = FakeRegistryClient({})
    resolver = DependencyResolver(registry, FakeCache())

    await resolver.find_deprecated_dependencies([_lockfile_change("irrelevant patch text")])

    assert len(registry.calls) == 50


async def test_sanitizes_untrusted_registry_message() -> None:
    # registry의 deprecated 필드는 패키지 게시자가 자유롭게 쓰는 텍스트이므로
    # 줄바꿈/백틱 주입과 과도한 길이를 그대로 PR 코멘트에 노출하면 안 된다.
    malicious_message = "line one\nline two\n`code fence`\n" + ("x" * 250)
    registry = FakeRegistryClient({("axios", "1.20.0"): malicious_message})
    resolver = DependencyResolver(registry, FakeCache())

    findings = await resolver.find_deprecated_dependencies([_lockfile_change(_AXIOS_BUMP_PATCH)])

    assert len(findings) == 1
    message = findings[0].message
    assert "\n" not in message
    assert "`" not in message
    assert len(message) <= 200 + len("npm registry: ''") + len("...")


async def test_creates_finding_for_relocated_gradle_dependency() -> None:
    maven = FakeMavenClient(
        {("com.google.code.gson:gson", "2.13.1"): "com.google.new:gson-renamed"}
    )
    resolver = DependencyResolver(
        FakeRegistryClient({}), FakeCache(), maven_client=maven, maven_cache=FakeCache()
    )

    findings = await resolver.find_deprecated_dependencies([_gradle_change(_GSON_BUMP_PATCH)])

    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "minor"
    assert finding.confidence == 1.0
    assert finding.file_path == "build.gradle.kts"
    assert "com.google.code.gson:gson" in finding.title
    assert "com.google.new:gson-renamed" in finding.message
    assert maven.calls == [("com.google.code.gson:gson", "2.13.1")]


async def test_no_finding_when_gradle_dependency_not_relocated() -> None:
    maven = FakeMavenClient({})
    resolver = DependencyResolver(
        FakeRegistryClient({}), FakeCache(), maven_client=maven, maven_cache=FakeCache()
    )

    findings = await resolver.find_deprecated_dependencies([_gradle_change(_GSON_BUMP_PATCH)])

    assert findings == []


async def test_ignores_gradle_build_file_when_maven_client_not_configured() -> None:
    # maven_client를 안 넘기면(기본 None) Gradle build 파일은 그냥 무시된다 —
    # npm 전용으로 계속 동작해야 한다.
    resolver = DependencyResolver(FakeRegistryClient({}), FakeCache())

    findings = await resolver.find_deprecated_dependencies([_gradle_change(_GSON_BUMP_PATCH)])

    assert findings == []


async def test_supports_groovy_build_gradle_file_name() -> None:
    maven = FakeMavenClient({("com.google.code.gson:gson", "2.13.1"): "moved:gson"})
    resolver = DependencyResolver(
        FakeRegistryClient({}), FakeCache(), maven_client=maven, maven_cache=FakeCache()
    )

    findings = await resolver.find_deprecated_dependencies(
        [_gradle_change(_GSON_BUMP_PATCH, file_name="build.gradle")]
    )

    assert len(findings) == 1


async def test_gradle_best_effort_swallows_maven_exceptions() -> None:
    resolver = DependencyResolver(
        FakeRegistryClient({}),
        FakeCache(),
        maven_client=RaisingMavenClient(),
        maven_cache=FakeCache(),
    )

    findings = await resolver.find_deprecated_dependencies([_gradle_change(_GSON_BUMP_PATCH)])

    assert findings == []


async def test_gradle_uses_maven_cache_before_calling_client() -> None:
    maven_cache = FakeCache()
    await maven_cache.set(
        "com.google.code.gson:gson", "2.13.1", CachedResult(deprecated=True, message="cached:moved")
    )
    maven = FakeMavenClient({})
    resolver = DependencyResolver(
        FakeRegistryClient({}), FakeCache(), maven_client=maven, maven_cache=maven_cache
    )

    findings = await resolver.find_deprecated_dependencies([_gradle_change(_GSON_BUMP_PATCH)])

    assert len(findings) == 1
    assert "cached:moved" in findings[0].message
    assert maven.calls == []


async def test_gradle_relocation_failure_is_not_cached() -> None:
    class NotOkMavenClient:
        async def check_relocation(self, name: str, version: str) -> RelocationLookupResult:
            return RelocationLookupResult(ok=False, relocated_to=None)

    maven_cache = FakeCache()
    resolver = DependencyResolver(
        FakeRegistryClient({}),
        FakeCache(),
        maven_client=NotOkMavenClient(),
        maven_cache=maven_cache,
    )

    findings = await resolver.find_deprecated_dependencies([_gradle_change(_GSON_BUMP_PATCH)])

    assert findings == []
    assert maven_cache.store == {}
