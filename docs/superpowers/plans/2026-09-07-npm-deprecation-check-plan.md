# npm 의존성 Deprecated 감지 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PR diff의 `package-lock.json`에 새로 추가/변경되는 npm 의존성이 deprecated인지 확인해, 코드가 결정론적으로(LLM 호출 없이) `ReviewComment`를 만들어 리뷰에 포함시킨다.

**Architecture:** `app/context/`에 4개 파일(패치 파서 → registry 클라이언트 → Redis 캐시 → 이 셋을 엮는 resolver)을 추가하고, `ReviewPipeline.run()`이 LLM 호출 직후·`filter_reviews()` 이전에 resolver 결과를 `output.reviews`에 합친다. severity를 항상 `minor`로 고정해 기존 라우팅(critical/major만 `_verify()` 검증, 나머지는 summary bullet)을 그대로 태우므로 `result_filter.py`/프롬프트는 손대지 않는다.

**Tech Stack:** httpx(이미 의존성 존재), redis.asyncio(이미 의존성 존재), pytest + httpx.MockTransport

**Spec:** `docs/superpowers/specs/2026-09-07-npm-deprecation-check-design.md`

## Global Constraints

- npm `package-lock.json`(lockfileVersion 2/3, `node_modules/<name>` 키 구조)만 지원한다. yarn.lock/pnpm-lock.yaml/lockfileVersion 1은 스코프 밖 — 파서가 그런 입력을 만나면 예외 없이 빈 리스트를 반환해야 한다.
- `event.changedFiles[].content`는 항상 `null`이다(github-app이 절대 채워주지 않는다, 실측 확인됨) — `patch` 텍스트만으로 파싱해야 한다. `content`를 참조하는 코드를 작성하지 않는다.
- 모든 외부 I/O(registry 호출, Redis)는 best-effort다: 어떤 예외도 파이프라인 밖으로 던지면 안 된다. 실패하면 그 항목만 스킵하고 계속 진행한다.
- scoped 패키지명(`@scope/name`)을 registry URL에 넣을 때 `/`만 `%2F`로 인코딩하고 `@`는 그대로 둔다(`urllib.parse.quote(name, safe="@")`). `safe=""`를 쓰면 항상 404가 난다.
- 새로 생성하는 `ReviewComment`는 항상 `severity="minor"`, `confidence=1.0`으로 고정한다. severity를 다른 값으로 바꾸지 않는다(그러면 `_verify()` 2차 검증 경로를 타게 되는데, 이미 결정론적으로 확정된 사실이라 검증 대상이 아니다).
- `dependency_check_enabled: bool = False`를 기본값으로 한다(기존 `rag_enabled`/`notion_sync_enabled`와 동일 패턴) — 이 기능이 꺼진 배포/테스트 환경에서 아무 영향이 없어야 한다.

---

### Task 1: npm lockfile patch 파서

**Files:**
- Create: `app/context/npm_lockfile_diff.py`
- Test: `tests/test_npm_lockfile_diff.py`

**Interfaces:**
- Produces: `DependencyChange` dataclass(`name: str`, `version: str`, `evidence_line: str`, `new_file_line: int`), `extract_dependency_changes(patch: str) -> list[DependencyChange]` — Task 4가 그대로 사용한다.

이 태스크는 순수 텍스트 파싱만 다룬다. 외부 I/O 없음.

**패치 포맷 배경** (실제 프로덕션 Kafka 이벤트에서 캡처한 진짜 `package-lock.json` diff):
```
@@ -8472,9 +8473,9 @@
       }
     },
     "node_modules/axios": {
-      "version": "1.19.0",
-      "resolved": "https://registry.npmjs.org/axios/-/axios-1.19.0.tgz",
-      "integrity": "sha512-ht/iuYZXEjFxLH/Hkezgd7m6JKlHHXEUSneaDz8uZe1Gj5QZtCnpyDsckvAiEnT89OEbCLmnte4R4sn7P0EKFw==",
+      "version": "1.20.0",
+      "resolved": "https://registry.npmjs.org/axios/-/axios-1.20.0.tgz",
+      "integrity": "sha512-r8aOh8j9cGKpgQAqpzrUHnSIc6a59Y3Xf/cv8sy1DrHCkZHzQGEuoq1tARk6qSyDdtQGSDgpb9kFlruzPvrgwg==",
       "license": "MIT",
       "dependencies": {
         "follow-redirects": "^1.16.0",
```
`node_modules/<name>` 키는 컨텍스트(변경 없음) 라인으로 남아있고, 그 안의 `version`/`resolved`/`integrity` 필드만 `-`/`+`로 바뀐다. 새 패키지가 통째로 추가되면 `node_modules/<name>` 키 자체가 `+` 라인이 된다. transitive dependency는 `"node_modules/parent/node_modules/child"`처럼 `node_modules/`가 여러 번 반복될 수 있다 — 패키지명은 **마지막** `node_modules/` 다음부터다.

- [ ] **Step 1: Write the failing test**

`tests/test_npm_lockfile_diff.py`:
```python
from app.context.npm_lockfile_diff import DependencyChange, extract_dependency_changes

# 실제 프로덕션 PR의 package-lock.json diff에서 캡처한 patch (2026-09 관측).
# 버전 bump(axios), 신규 패키지 추가(expo-linear-gradient),
# 중첩 transitive dependency(lint-staged/node_modules/picomatch) 세 케이스를 커버한다.
_REAL_PATCH_SAMPLE = """\
@@ -8472,9 +8473,9 @@
       }
     },
     "node_modules/axios": {
-      "version": "1.19.0",
-      "resolved": "https://registry.npmjs.org/axios/-/axios-1.19.0.tgz",
-      "integrity": "sha512-ht/iuYZXEjFxLH/Hkezgd7m6JKlHHXEUSneaDz8uZe1Gj5QZtCnpyDsckvAiEnT89OEbCLmnte4R4sn7P0EKFw==",
+      "version": "1.20.0",
+      "resolved": "https://registry.npmjs.org/axios/-/axios-1.20.0.tgz",
+      "integrity": "sha512-r8aOh8j9cGKpgQAqpzrUHnSIc6a59Y3Xf/cv8sy1DrHCkZHzQGEuoq1tARk6qSyDdtQGSDgpb9kFlruzPvrgwg==",
       "license": "MIT",
       "dependencies": {
         "follow-redirects": "^1.16.0",
@@ -11948,6 +11949,17 @@
         "react": "*"
       }
     },
+    "node_modules/expo-linear-gradient": {
+      "version": "56.0.4",
+      "resolved": "https://registry.npmjs.org/expo-linear-gradient/-/expo-linear-gradient-56.0.4.tgz",
+      "integrity": "sha512-KUp1dNSRtuMyiExhf6FJf5YUtmw2cRaPytl10HQi7isj5Yac38udmD55T2tglNYTZlvgT5+oflpyFoH15hmOcw==",
+      "license": "MIT",
+      "peerDependencies": {
+        "expo": "*",
+        "react": "*",
+        "react-native": "*"
+      }
+    },
     "node_modules/expo-linking": {
       "version": "56.0.17",
       "resolved": "https://registry.npmjs.org/expo-linking/-/expo-linking-56.0.17.tgz",
@@ -16296,9 +16308,9 @@
       }
     },
     "node_modules/lint-staged/node_modules/picomatch": {
-      "version": "4.0.5",
-      "resolved": "https://registry.npmjs.org/picomatch/-/picomatch-4.0.5.tgz",
-      "integrity": "sha512-RvwwcruNjI1ncT5xRakeyS9Lf8lcItv34KD+aif+VH9kduAyfYBipGh12274xtenIPZ119/R9BdTBa8gAwSh0A==",
+      "version": "4.0.7",
+      "resolved": "https://registry.npmjs.org/picomatch/-/picomatch-4.0.7.tgz",
+      "integrity": "sha512-qcJu88Q2IWqJsDD529JKMdwGm/dvInW4HvQnRwiH9JtihJvzGOscDtHE3x1pBKeUOTysQ8kVmLnJ2kJu7yhcGA==",
       "dev": true,
       "license": "MIT",
       "engines": {
"""


def test_extracts_version_bump() -> None:
    changes = extract_dependency_changes(_REAL_PATCH_SAMPLE)
    axios = next(c for c in changes if c.name == "axios")
    assert axios.version == "1.20.0"
    assert axios.new_file_line == 8476
    assert axios.evidence_line == '+      "version": "1.20.0",'


def test_extracts_newly_added_package() -> None:
    changes = extract_dependency_changes(_REAL_PATCH_SAMPLE)
    new_pkg = next(c for c in changes if c.name == "expo-linear-gradient")
    assert new_pkg.version == "56.0.4"
    assert new_pkg.new_file_line == 11953


def test_extracts_nested_transitive_dependency_by_last_segment() -> None:
    changes = extract_dependency_changes(_REAL_PATCH_SAMPLE)
    nested = next(c for c in changes if c.name == "picomatch")
    assert nested.version == "4.0.7"
    assert nested.new_file_line == 16311


def test_ignores_unchanged_context_version_lines() -> None:
    # expo-linking은 이 patch에서 안 바뀌었다(컨텍스트로만 등장) — 잡히면 안 된다.
    changes = extract_dependency_changes(_REAL_PATCH_SAMPLE)
    assert not any(c.name == "expo-linking" for c in changes)


def test_returns_empty_list_for_non_lockfile_text() -> None:
    assert extract_dependency_changes("just some random text\nno diff here") == []


def test_returns_empty_list_for_empty_patch() -> None:
    assert extract_dependency_changes("") == []


def test_dependency_change_is_a_plain_dataclass() -> None:
    change = DependencyChange(
        name="axios", version="1.20.0", evidence_line='"version": "1.20.0"', new_file_line=1
    )
    assert change.name == "axios"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_npm_lockfile_diff.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.context.npm_lockfile_diff'`

- [ ] **Step 3: Write minimal implementation**

`app/context/npm_lockfile_diff.py`:
```python
from __future__ import annotations

import re
from dataclasses import dataclass

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<new_start>\d+)(?:,\d+)? @@")
_NODE_MODULES_KEY = re.compile(r'"node_modules/(?P<path>[^"]+)":\s*\{')
_VERSION_LINE = re.compile(r'^\+\s*"version":\s*"(?P<version>[^"]+)",?\s*$')


@dataclass
class DependencyChange:
    name: str
    version: str
    evidence_line: str
    new_file_line: int


def extract_dependency_changes(patch: str) -> list[DependencyChange]:
    """package-lock.json(lockfileVersion 2/3) patch에서 새로 추가/변경된
    (패키지명, 버전) 쌍을 뽑는다.

    지원하지 않는 포맷(yarn.lock, lockfileVersion 1 등)이나 빈 입력은 예외 없이
    빈 리스트를 반환한다 — "node_modules/<name>": { 키 구조를 못 찾으면 그냥
    아무것도 안 잡힐 뿐이다.
    """
    changes: list[DependencyChange] = []
    new_line = 0
    current_name: str | None = None

    for raw_line in patch.splitlines():
        header_match = _HUNK_HEADER.match(raw_line)
        if header_match:
            new_line = int(header_match.group("new_start"))
            current_name = None
            continue

        if not raw_line:
            continue

        prefix = raw_line[0]
        content = raw_line[1:] if prefix in ("+", "-", " ") else raw_line

        key_match = _NODE_MODULES_KEY.search(content)
        if key_match:
            # 중첩 transitive dependency("node_modules/parent/node_modules/child")는
            # 마지막 node_modules/ 다음부터가 실제 패키지명이다.
            current_name = key_match.group("path").rsplit("node_modules/", 1)[-1]

        if prefix == "+":
            version_match = _VERSION_LINE.match(raw_line)
            if version_match and current_name is not None:
                changes.append(
                    DependencyChange(
                        name=current_name,
                        version=version_match.group("version"),
                        evidence_line=raw_line,
                        new_file_line=new_line,
                    )
                )
            new_line += 1
        elif prefix == " ":
            new_line += 1
        # prefix == "-": 삭제된 라인은 new-file에 없으므로 new_line을 건드리지 않는다.

    return changes
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_npm_lockfile_diff.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add app/context/npm_lockfile_diff.py tests/test_npm_lockfile_diff.py
git commit -m "feat :: npm lockfile patch에서 의존성 변경 추출하는 파서 추가"
```

---

### Task 2: npm registry 클라이언트

**Files:**
- Create: `app/context/npm_registry_client.py`
- Test: `tests/test_npm_registry_client.py`

**Interfaces:**
- Consumes: 없음(외부 HTTP만)
- Produces: `NpmRegistryClient` — `async def get_deprecation_message(self, name: str, version: str) -> str | None`. Task 4가 사용한다.

- [ ] **Step 1: Write the failing test**

`tests/test_npm_registry_client.py`:
```python
from collections.abc import Callable

import httpx
import pytest

from app.context.npm_registry_client import NpmRegistryClient


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> NpmRegistryClient:
    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport)
    return NpmRegistryClient(client=async_client)


async def test_returns_deprecation_message_when_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/axios/1.20.0"
        return httpx.Response(200, json={"deprecated": "use fetch instead"})

    client = _client(handler)
    result = await client.get_deprecation_message("axios", "1.20.0")
    assert result == "use fetch instead"


async def test_returns_none_when_not_deprecated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "axios", "version": "1.20.0"})

    client = _client(handler)
    result = await client.get_deprecation_message("axios", "1.20.0")
    assert result is None


async def test_encodes_scoped_package_name_correctly() -> None:
    captured_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_paths.append(request.url.path)
        return httpx.Response(200, json={})

    client = _client(handler)
    await client.get_deprecation_message("@tanstack/query-core", "5.102.8")

    # @는 그대로, /만 %2F로 인코딩되어야 한다 (safe="" 였다면 %40tanstack...이 되어
    # registry가 항상 404를 반환했을 것 — 실제로 겪은 버그).
    assert captured_paths == ["/@tanstack%2Fquery-core/5.102.8"]


async def test_returns_none_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = _client(handler)
    result = await client.get_deprecation_message("nonexistent-package", "1.0.0")
    assert result is None


async def test_returns_none_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    client = _client(handler)
    result = await client.get_deprecation_message("axios", "1.20.0")
    assert result is None


async def test_returns_none_on_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    result = await client.get_deprecation_message("axios", "1.20.0")
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_npm_registry_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.context.npm_registry_client'`

- [ ] **Step 3: Write minimal implementation**

`app/context/npm_registry_client.py`:
```python
from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_REGISTRY_BASE_URL = "https://registry.npmjs.org"


class NpmRegistryClient:
    """npm registry에서 특정 (패키지명, 버전)의 deprecated 여부를 조회한다.

    best-effort: 네트워크 실패/타임아웃/404 등 어떤 이유로든 조회에 실패하면
    예외를 던지지 않고 None(= "확인 불가, deprecated 아님으로 간주하지 않음")을
    반환한다. 호출자가 실패와 "deprecated 아님"을 구분할 필요가 없도록 의도한
    설계다 — 둘 다 "이번엔 finding을 만들지 않는다"로 처리하면 되기 때문.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=_REGISTRY_BASE_URL, timeout=timeout_seconds
        )

    async def get_deprecation_message(self, name: str, version: str) -> str | None:
        # scoped 패키지(@scope/name)는 /만 %2F로 인코딩하고 @는 그대로 둔다.
        # safe=""로 @까지 인코딩하면 registry가 다른 경로로 취급해 항상 404가 난다.
        encoded_name = quote(name, safe="@")
        encoded_version = quote(version, safe="")
        path = f"/{encoded_name}/{encoded_version}"
        try:
            response = await self._client.get(path)
        except httpx.HTTPError:
            logger.warning(
                "npm registry lookup failed name=%s version=%s", name, version, exc_info=True
            )
            return None

        if response.status_code != 200:
            return None

        try:
            data = response.json()
        except ValueError:
            return None

        deprecated = data.get("deprecated")
        return deprecated if isinstance(deprecated, str) else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_npm_registry_client.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add app/context/npm_registry_client.py tests/test_npm_registry_client.py
git commit -m "feat :: npm registry deprecated 조회 클라이언트 추가"
```

---

### Task 3: Redis 캐시

**Files:**
- Create: `app/context/npm_deprecation_cache.py`
- Test: `tests/test_npm_deprecation_cache.py`

**Interfaces:**
- Consumes: 없음(Redis만)
- Produces: `RedisNpmDeprecationCache` — `async def get(self, name: str, version: str) -> CachedResult | None`, `async def set(self, name: str, version: str, result: CachedResult) -> None`, `CachedResult` dataclass(`deprecated: bool`, `message: str | None`). Task 4가 사용한다.

`app/context/api_spec_link_store.py`의 `RedisNotionLinkStore`와 동일한 구조를 따른다(같은 `RedisLike` Protocol 재사용).

- [ ] **Step 1: Write the failing test**

`tests/test_npm_deprecation_cache.py`:
```python
import json

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_npm_deprecation_cache.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.context.npm_deprecation_cache'`

- [ ] **Step 3: Write minimal implementation**

`app/context/npm_deprecation_cache.py`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_npm_deprecation_cache.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/context/npm_deprecation_cache.py tests/test_npm_deprecation_cache.py
git commit -m "feat :: npm deprecated 조회 결과 Redis 캐시 추가"
```

---

### Task 4: DependencyResolver (조정자)

**Files:**
- Create: `app/context/dependency_resolver.py`
- Test: `tests/test_dependency_resolver.py`

**Interfaces:**
- Consumes: `extract_dependency_changes`(Task 1), `NpmRegistryClient.get_deprecation_message`(Task 2), `RedisNpmDeprecationCache.get`/`.set`(Task 3), `app.review.schema.ChangedFile`/`ReviewComment`
- Produces: `DependencyResolver` — `async def find_deprecated_dependencies(self, changed_files: list[ChangedFile]) -> list[ReviewComment]`. Task 5가 파이프라인에서 사용한다.

**Files는 `package-lock.json`만 처리한다** — `changed_files`를 순회하며 `PurePosixPath(f.file_path).name == "package-lock.json"`인 것만 파싱한다.

- [ ] **Step 1: Write the failing test**

`tests/test_dependency_resolver.py`:
```python
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

    assert cache.store[("axios", "1.20.0")] == CachedResult(deprecated=True, message="deprecated msg")


async def test_ignores_non_lockfile_changed_files() -> None:
    resolver = DependencyResolver(FakeRegistryClient({}), FakeCache())
    other_file = ChangedFile(file_path="src/app.ts", status="modified", patch="+ console.log(1)")

    findings = await resolver.find_deprecated_dependencies([other_file])

    assert findings == []


async def test_best_effort_swallows_registry_exceptions() -> None:
    resolver = DependencyResolver(RaisingRegistryClient(), FakeCache())

    findings = await resolver.find_deprecated_dependencies([_lockfile_change(_AXIOS_BUMP_PATCH)])

    assert findings == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dependency_resolver.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.context.dependency_resolver'`

- [ ] **Step 3: Write minimal implementation**

`app/context/dependency_resolver.py`:
```python
from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import Protocol

from app.context.npm_deprecation_cache import CachedResult
from app.context.npm_lockfile_diff import extract_dependency_changes
from app.review.schema import ChangedFile, ReviewComment

logger = logging.getLogger(__name__)

_LOCKFILE_NAME = "package-lock.json"


class RegistryClient(Protocol):
    async def get_deprecation_message(self, name: str, version: str) -> str | None: ...


class DeprecationCache(Protocol):
    async def get(self, name: str, version: str) -> CachedResult | None: ...
    async def set(self, name: str, version: str, result: CachedResult) -> None: ...


class DependencyResolver:
    """package-lock.json 변경분에서 deprecated 의존성을 찾아 ReviewComment로
    만든다. LLM을 거치지 않는 결정론적 판단이다 — registry가 deprecated 필드로
    직접 알려주는 사실이라 재해석의 여지가 없다.

    best-effort: 개별 패키지 조회가 실패해도 나머지는 계속 진행하고, 전체가
    실패해도 예외를 던지지 않고 빈 리스트를 반환한다(리뷰 자체를 막지 않는다).
    """

    def __init__(self, registry_client: RegistryClient, cache: DeprecationCache) -> None:
        self._registry_client = registry_client
        self._cache = cache

    async def find_deprecated_dependencies(
        self, changed_files: list[ChangedFile]
    ) -> list[ReviewComment]:
        findings: list[ReviewComment] = []
        for file in changed_files:
            if PurePosixPath(file.file_path).name != _LOCKFILE_NAME:
                continue
            findings.extend(await self._check_lockfile(file))
        return findings

    async def _check_lockfile(self, file: ChangedFile) -> list[ReviewComment]:
        try:
            changes = extract_dependency_changes(file.patch)
        except Exception:
            logger.warning("failed to parse lockfile patch path=%s", file.file_path, exc_info=True)
            return []

        findings: list[ReviewComment] = []
        for change in changes:
            message = await self._get_deprecation_message(change.name, change.version)
            if message is None:
                continue
            findings.append(
                ReviewComment(
                    severity="minor",
                    confidence=1.0,
                    file_path=file.file_path,
                    line=change.new_file_line,
                    title=f"deprecated 패키지 추가/변경됨: {change.name}@{change.version}",
                    message=f"npm registry: '{message}'",
                    evidence=[change.evidence_line],
                    suggested_fix=None,
                )
            )
        return findings

    async def _get_deprecation_message(self, name: str, version: str) -> str | None:
        try:
            cached = await self._cache.get(name, version)
        except Exception:
            logger.warning("npm deprecation cache read failed name=%s version=%s", name, version, exc_info=True)
            cached = None

        if cached is not None:
            return cached.message if cached.deprecated else None

        try:
            message = await self._registry_client.get_deprecation_message(name, version)
        except Exception:
            logger.warning("npm registry lookup failed name=%s version=%s", name, version, exc_info=True)
            return None

        try:
            await self._cache.set(
                name, version, CachedResult(deprecated=message is not None, message=message)
            )
        except Exception:
            logger.warning("npm deprecation cache write failed name=%s version=%s", name, version, exc_info=True)

        return message
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_dependency_resolver.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add app/context/dependency_resolver.py tests/test_dependency_resolver.py
git commit -m "feat :: package-lock.json deprecated 의존성 조정자 추가"
```

---

### Task 5: 파이프라인 배선 + 설정

**Files:**
- Modify: `app/core/config.py`
- Modify: `app/review/pipeline.py`
- Test: `tests/test_review_pipeline.py` (기존 파일에 테스트 추가)

**Interfaces:**
- Consumes: `DependencyResolver.find_deprecated_dependencies`(Task 4)
- Produces: `ReviewPipeline.__init__`의 새 `dependency_resolver` 파라미터. Task 6(main.py)이 사용한다.

- [ ] **Step 1: Write the failing test**

`tests/test_review_pipeline.py` 상단 import에 추가(기존 import 블록 뒤):
```python
from app.review.schema import ChangedFile
```
(이미 import돼 있으면 생략)

파일 끝에 추가:
```python
class FakeDependencyResolver:
    def __init__(self, findings: list[ReviewComment]) -> None:
        self._findings = findings
        self.received_changed_files: list[ChangedFile] | None = None

    async def find_deprecated_dependencies(
        self, changed_files: list[ChangedFile]
    ) -> list[ReviewComment]:
        self.received_changed_files = changed_files
        return self._findings


async def test_run_includes_dependency_resolver_findings_in_summary() -> None:
    dependency_finding = ReviewComment(
        severity="minor",
        confidence=1.0,
        file_path="package-lock.json",
        line=42,
        title="deprecated 패키지 추가/변경됨: axios@1.20.0",
        message="npm registry: 'deprecated'",
        evidence=['+      "version": "1.20.0",'],
    )
    fake_resolver = FakeDependencyResolver([dependency_finding])
    llm = FakeLLM(ReviewModelOutput(summary="정상 diff입니다.", reviews=[]))
    pipeline = ReviewPipeline(
        llm, model_version="v1", prompt_version="v1", dependency_resolver=fake_resolver
    )
    event = _event()  # 기존 헬퍼(tests/test_review_pipeline.py:94) 재사용

    result = await pipeline.run(event)

    assert isinstance(result, ReviewCompletedEvent)
    assert "deprecated 패키지 추가/변경됨: axios@1.20.0" in result.summary
    # severity=minor라 인라인 코멘트(reviews[])가 아니라 summary bullet로만 나타난다
    assert result.reviews == []
    assert fake_resolver.received_changed_files == event.changed_files


async def test_run_works_without_dependency_resolver() -> None:
    # dependency_resolver=None(기본값)이면 기존 동작 그대로 — 회귀 방지.
    llm = FakeLLM(ReviewModelOutput(summary="정상 diff입니다.", reviews=[]))
    pipeline = ReviewPipeline(llm, model_version="v1", prompt_version="v1")
    event = _event()

    result = await pipeline.run(event)

    assert isinstance(result, ReviewCompletedEvent)
    assert result.summary == "정상 diff입니다."
```

`_event()`(tests/test_review_pipeline.py:94)와 `FakeLLM`(같은 파일:50)은 이미 존재하는 헬퍼 — 그대로 재사용한다. `FakeLLM(output=...)`은 첫 위치 인자로도 받으므로 `FakeLLM(ReviewModelOutput(...))` 그대로 쓰면 된다.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_review_pipeline.py -k dependency_resolver -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'dependency_resolver'`

- [ ] **Step 3: Write minimal implementation**

`app/core/config.py`에 `notion_sync_enabled` 블록 바로 아래 추가:
```python
    # 기본 False: registry 조회가 필요 없는 레포/환경에서도 앱이 정상 기동해야 한다.
    dependency_check_enabled: bool = False
```

`app/review/pipeline.py`에서 세 군데 수정:

1. import 블록에 추가:
```python
from app.review.schema import (
    ChangedFile,
    FailureReason,
    ReviewComment,
    ReviewCompletedEvent,
    ReviewFailedEvent,
    ReviewRequestedEvent,
    ReviewTarget,
    VerificationResult,
)
```
(`ChangedFile` 추가, 나머지는 기존 그대로)

2. `ContextRetriever`/`ApiSpecContextRetriever` Protocol 정의 근처에 추가:
```python
class DependencyContextResolver(Protocol):
    async def find_deprecated_dependencies(
        self, changed_files: list[ChangedFile]
    ) -> list[ReviewComment]:
        """changed_files 중 lockfile 변경분에서 deprecated 의존성을 찾는다.

        실패 시 빈 리스트를 반환한다(리뷰 자체를 막지 않는다).
        """
        ...
```

3. `ReviewPipeline.__init__`과 `run()` 수정:
```python
    def __init__(
        self,
        llm: ReviewLLM,
        *,
        model_version: str,
        prompt_version: str,
        max_tokens: int = 1500,
        verify_max_tokens: int = 800,
        retriever: ContextRetriever | None = None,
        notion_link_store: NotionLinkStore | None = None,
        api_spec_retriever: ApiSpecContextRetriever | None = None,
        dependency_resolver: DependencyContextResolver | None = None,
    ) -> None:
        self._llm = llm
        self._model_version = model_version
        self._prompt_version = prompt_version
        self._max_tokens = max_tokens
        self._verify_max_tokens = verify_max_tokens
        self._retriever = retriever
        self._notion_link_store = notion_link_store
        self._api_spec_retriever = api_spec_retriever
        self._dependency_resolver = dependency_resolver
```

`run()`의 LLM 호출 성공 분기(`reviews = filter_reviews(output.reviews)` 바로 앞)에 삽입:
```python
            if self._dependency_resolver is not None:
                try:
                    dependency_findings = (
                        await self._dependency_resolver.find_deprecated_dependencies(
                            event.changed_files
                        )
                    )
                except Exception:
                    logger.warning(
                        "dependency resolver failed reviewJobId=%s",
                        event.review_job_id,
                        exc_info=True,
                    )
                    dependency_findings = []
                output.reviews.extend(dependency_findings)

            reviews = filter_reviews(output.reviews)
```

(resolver 내부에서도 이미 best-effort로 예외를 삼키지만, 파이프라인 쪽에도 한 번 더 방어막을 둔다 — resolver 구현이 바뀌어도 리뷰 전체가 절대 죽지 않도록.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_review_pipeline.py -v`
Expected: PASS (기존 테스트 전부 + 새 테스트 2개)

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py app/review/pipeline.py tests/test_review_pipeline.py
git commit -m "feat :: DependencyResolver를 리뷰 파이프라인에 배선"
```

---

### Task 6: main.py lifespan 배선

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_main.py` (기존 파일에 테스트 추가)

**Interfaces:**
- Consumes: `NpmRegistryClient`(Task 2), `RedisNpmDeprecationCache`(Task 3), `DependencyResolver`(Task 4)
- Produces: 없음(최종 배선 지점)

- [ ] **Step 1: Write the failing test**

`tests/test_main.py`의 `test_lifespan_wires_rag_retriever_when_enabled` 아래에 추가:
```python
async def test_lifespan_wires_dependency_resolver_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("DEPENDENCY_CHECK_ENABLED", "true")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")

    fake_producer = FakeStartStop()
    fake_consumer = FakeConsumerSource()
    fake_comment_answer_consumer = FakeConsumerSource()
    monkeypatch.setattr("app.main.create_producer", lambda settings: fake_producer)
    monkeypatch.setattr("app.main.create_consumer", lambda settings: fake_consumer)
    monkeypatch.setattr(
        "app.main.create_comment_answer_consumer",
        lambda settings: fake_comment_answer_consumer,
    )

    captured_kwargs: dict[str, object] = {}
    original_init = ReviewPipeline.__init__

    def capturing_init(self: ReviewPipeline, *args: object, **kwargs: object) -> None:
        captured_kwargs.update(kwargs)
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ReviewPipeline, "__init__", capturing_init)

    try:
        async with lifespan(app):
            await asyncio.sleep(0.05)

        assert captured_kwargs.get("dependency_resolver") is not None
    finally:
        get_settings.cache_clear()


async def test_lifespan_leaves_dependency_resolver_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")
    # DEPENDENCY_CHECK_ENABLED를 아예 설정하지 않는다 (기본 False 확인)

    fake_producer = FakeStartStop()
    fake_consumer = FakeConsumerSource()
    fake_comment_answer_consumer = FakeConsumerSource()
    monkeypatch.setattr("app.main.create_producer", lambda settings: fake_producer)
    monkeypatch.setattr("app.main.create_consumer", lambda settings: fake_consumer)
    monkeypatch.setattr(
        "app.main.create_comment_answer_consumer",
        lambda settings: fake_comment_answer_consumer,
    )

    captured_kwargs: dict[str, object] = {}
    original_init = ReviewPipeline.__init__

    def capturing_init(self: ReviewPipeline, *args: object, **kwargs: object) -> None:
        captured_kwargs.update(kwargs)
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ReviewPipeline, "__init__", capturing_init)

    try:
        async with lifespan(app):
            await asyncio.sleep(0.05)

        assert captured_kwargs.get("dependency_resolver") is None
    finally:
        get_settings.cache_clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main.py -k dependency_resolver -v`
Expected: FAIL — `captured_kwargs.get("dependency_resolver")`가 애초에 `ReviewPipeline.__init__`에 그 파라미터가 없어 `assert ... is not None`이 실패(Task 5를 먼저 끝냈으면 파라미터 자체는 있지만 main.py가 아직 안 넘겨서 `None`이라 실패)

- [ ] **Step 3: Write minimal implementation**

`app/main.py`의 import 블록에 추가:
```python
from app.review.pipeline import ReviewPipeline
```
(이미 있음 — 아래 새 import만 추가)
```python
from app.review.dedup import (
    create_comment_answer_dedup_store,
    create_dedup_store,
    create_redis_client,
)
```
(이미 있음, 변경 없음)

`lifespan()` 안, `redis_client = create_redis_client(settings)` 라인 바로 다음(그리고 `notion_link_store` 블록보다 먼저든 나중이든 상관없음 — 서로 의존하지 않으므로 `notion_link_store` 블록 바로 아래에 추가):
```python
    dependency_resolver = None
    if settings.dependency_check_enabled:
        from app.context.dependency_resolver import DependencyResolver
        from app.context.npm_deprecation_cache import RedisNpmDeprecationCache
        from app.context.npm_registry_client import NpmRegistryClient

        npm_registry_client = NpmRegistryClient()
        npm_deprecation_cache = RedisNpmDeprecationCache(redis_client)  # type: ignore[arg-type]
        dependency_resolver = DependencyResolver(npm_registry_client, npm_deprecation_cache)
```

`ReviewPipeline(...)` 생성 호출에 kwarg 추가:
```python
    pipeline = ReviewPipeline(
        llm_client,
        model_version=settings.llm_model,
        prompt_version="v1",
        retriever=retriever,
        api_spec_retriever=api_spec_retriever,
        notion_link_store=notion_link_store,
        dependency_resolver=dependency_resolver,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_main.py -v`
Expected: PASS (기존 테스트 전부 + 새 테스트 2개)

- [ ] **Step 5: Run full suite + lint**

```bash
uv run pytest -q
uv run ruff check .
uv run python -c "import app.main; import sys; print('qdrant_client' in sys.modules)"
```
Expected: 전체 테스트 통과, ruff 통과, `qdrant_client` import는 `False`(이 기능은 httpx/redis만 쓰므로 무거운 의존성 회귀와 무관하지만, 이 레포의 표준 검증 절차이므로 항상 확인한다).

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_main.py
git commit -m "feat :: DependencyResolver를 main.py lifespan에 배선"
```

---

## 최종 확인 (전체 완료 후)

- [ ] `uv run pytest -q` 전체 통과
- [ ] `uv run ruff check .` 통과
- [ ] `uv run mypy .` 통과(권장)
- [ ] `docs/superpowers/specs/2026-09-07-npm-deprecation-check-design.md`의 모든 요구사항이 위 6개 태스크로 커버됐는지 재확인:
  - patch 파싱(content 없이) — Task 1
  - registry 조회 + scoped 패키지 인코딩 — Task 2
  - 30일 TTL 캐시 — Task 3
  - best-effort 조정 — Task 4
  - severity=minor로 기존 라우팅 재사용, LLM 미개입 — Task 5
  - `dependency_check_enabled` 플래그, main.py 배선 — Task 5, 6
