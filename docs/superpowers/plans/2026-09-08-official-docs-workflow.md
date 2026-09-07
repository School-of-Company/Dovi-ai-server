# 5단계 — 공식문서 Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 버전이 바뀐 모든 npm 의존성에 대해 GitHub 릴리즈 노트/CHANGELOG
근거를 모아, 메인 리뷰 LLM이 breaking change 여부를 직접 판단할 수 있는
텍스트를 리뷰 프롬프트에 추가한다.

**Architecture:** `NpmRegistryClient`를 확장해 GitHub repo URL을 함께
얻고, 새 `GithubReleaseClient`가 태그 3종 시도 → CHANGELOG.md 폴백
순서로 릴리즈 노트를 찾는다. `OfficialDocsWorkflow`가 이를 조율해
판단 없이 근거 텍스트만 만들고, `ReviewPipeline`이 `api_spec_context`와
동일한 패턴으로 이 텍스트를 프롬프트에 이어붙인다.

**Tech Stack:** httpx(신규 의존성 없음), Redis(기존 캐시 패턴 재사용).

**Spec:** `docs/superpowers/specs/2026-09-07-official-docs-workflow-design.md`

## Global Constraints

- 트리거: 버전이 바뀐 모든 npm 의존성(deprecated 여부 무관) — 4단계보다
  넓은 범위.
- 문서 수집 범위: GitHub Releases API + `CHANGELOG.md`만. 임의 사이트
  크롤링은 스코프 밖.
- 버전 범위: 타겟(신규) 버전 하나의 릴리즈 노트만. range coverage는
  스코프 밖.
- LangGraph 미도입 — 일반 async 코드로 구현.
- npm registry 호출은 패키지당 1번으로 통합(deprecated 확인과 GitHub
  repo URL 추출을 같은 호출에서 처리) — 패키지당 2번 호출하지 않는다.
- `GITHUB_TOKEN`(read-only, public repo) 신규 설정 추가, 미설정이면
  미인증으로 동작(기능을 끄지 않음).
- `OfficialDocsWorkflow`는 **판단하지 않는다** — breaking change 여부
  단정 없이 근거 텍스트만 만든다. 최종 판단은 메인 리뷰 LLM.
- best-effort 전 구간: 어떤 실패든 예외를 밖으로 던지지 않고 빈
  문자열/스킵으로 처리 — 리뷰 자체를 절대 막지 않는다.
- 캐시는 "찾음"과 "이 버전엔 릴리즈노트 없음"을 둘 다 캐싱하되, 조회
  자체가 실패(네트워크/timeout)한 경우는 캐싱하지 않는다.

---

### Task 1: `NpmRegistryClient` 확장 — GitHub repo URL 추출

**Files:**
- Modify: `app/context/npm_registry_client.py`
- Test: `tests/test_npm_registry_client.py`

**Interfaces:**
- Consumes: 없음(기존 파일 확장)
- Produces: `DeprecationLookupResult.github_repo: str | None` 신규 필드
  (Task 4가 사용) — 기존 `ok`/`message` 필드와 기존 4단계 코드
  (`app/context/dependency_resolver.py`)의 동작은 그대로 유지된다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_npm_registry_client.py` 파일 끝에 추가(기존 `_client()`
헬퍼를 그대로 재사용):

```python
async def test_extracts_github_repo_from_git_plus_https_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "repository": {"type": "git", "url": "git+https://github.com/axios/axios.git"}
            },
        )

    client = _client(handler)
    result = await client.check_deprecation("axios", "1.20.0")
    assert result.github_repo == "axios/axios"


async def test_extracts_github_repo_from_git_protocol_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"repository": {"url": "git://github.com/axios/axios.git"}})

    client = _client(handler)
    result = await client.check_deprecation("axios", "1.20.0")
    assert result.github_repo == "axios/axios"


async def test_extracts_github_repo_from_plain_https_url_string() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"repository": "https://github.com/axios/axios"})

    client = _client(handler)
    result = await client.check_deprecation("axios", "1.20.0")
    assert result.github_repo == "axios/axios"


async def test_github_repo_is_none_when_repository_field_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "axios"})

    client = _client(handler)
    result = await client.check_deprecation("axios", "1.20.0")
    assert result.github_repo is None


async def test_github_repo_is_none_when_repository_is_not_github() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"repository": {"url": "https://gitlab.com/foo/bar.git"}})

    client = _client(handler)
    result = await client.check_deprecation("axios", "1.20.0")
    assert result.github_repo is None
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_npm_registry_client.py -v`
Expected: 새 5개 테스트 FAIL — `AttributeError: 'DeprecationLookupResult' object has no attribute 'github_repo'`

- [ ] **Step 3: `app/context/npm_registry_client.py` 수정**

파일 상단 import에 `re` 추가:

```python
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx
```

`_REGISTRY_BASE_URL = "https://registry.npmjs.org"` 다음 줄에 추가:

```python
_GITHUB_REPO_PATTERN = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")


def _extract_github_repo(repository_field: object) -> str | None:
    url: object = None
    if isinstance(repository_field, dict):
        url = repository_field.get("url")
    elif isinstance(repository_field, str):
        url = repository_field
    if not isinstance(url, str):
        return None
    match = _GITHUB_REPO_PATTERN.search(url)
    if match is None:
        return None
    return f"{match.group(1)}/{match.group(2)}"
```

`DeprecationLookupResult`를 다음으로 교체:

```python
@dataclass
class DeprecationLookupResult:
    """registry 조회의 성공/실패와 결과를 분리해서 표현한다.

    `ok=False`는 네트워크/타임아웃/404/malformed JSON 등 조회 자체가 실패했다는
    뜻이고, `ok=True`인데 `message=None`은 조회는 성공했지만 deprecated가 아니라는
    뜻이다. 호출자(DependencyResolver)가 이 둘을 구분해야 실패를 "deprecated
    아님"으로 잘못 캐싱하지 않는다.

    `github_repo`는 registry의 `repository.url` 필드에서 뽑은 "owner/repo" —
    GitHub이 아니거나 필드가 없으면 None (5단계 OfficialDocsWorkflow가 사용).
    """

    ok: bool
    message: str | None
    github_repo: str | None = None
```

`check_deprecation()`의 마지막 두 줄(성공 경로 return 직전)을 찾는다:

```python
        deprecated = data.get("deprecated") if isinstance(data, dict) else None
        message = deprecated if isinstance(deprecated, str) else None
        return DeprecationLookupResult(ok=True, message=message)
```

다음으로 교체:

```python
        deprecated = data.get("deprecated") if isinstance(data, dict) else None
        message = deprecated if isinstance(deprecated, str) else None
        github_repo = (
            _extract_github_repo(data.get("repository")) if isinstance(data, dict) else None
        )
        return DeprecationLookupResult(ok=True, message=message, github_repo=github_repo)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_npm_registry_client.py -v`
Expected: PASS (기존 테스트 포함 전부 — `github_repo`에 기본값 `None`이
있어 기존 `DeprecationLookupResult(ok=True, message=...)` 형태의 동등
비교가 전부 그대로 통과한다)

- [ ] **Step 5: 전체 테스트 스위트 회귀 확인**

Run: `uv run pytest && uv run ruff check . && uv run mypy .`
Expected: 전부 PASS/clean (4단계의 `dependency_resolver.py`는
`github_repo` 필드를 아예 참조하지 않으므로 동작 변화 없음)

- [ ] **Step 6: Commit**

```bash
git add app/context/npm_registry_client.py tests/test_npm_registry_client.py
git commit -m "feat :: NpmRegistryClient가 registry 응답에서 GitHub repo URL도 추출"
```

---

### Task 2: `GithubReleaseClient` (신규)

**Files:**
- Create: `app/context/github_release_client.py`
- Test: `tests/test_github_release_client.py`

**Interfaces:**
- Consumes: 없음
- Produces: `GithubReleaseClient(token="", timeout_seconds=3.0, client=None, raw_client=None)`
  with `async def find_release_notes(owner_repo: str, name: str, version: str) -> ReleaseNotesResult`,
  `async def aclose() -> None`; `ReleaseNotesResult(ok: bool, notes: str | None)` (Task 4가 사용)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_github_release_client.py`:

```python
from collections.abc import Callable

import httpx

from app.context.github_release_client import GithubReleaseClient, ReleaseNotesResult


def _client(
    api_handler: Callable[[httpx.Request], httpx.Response],
    raw_handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> GithubReleaseClient:
    api_transport = httpx.MockTransport(api_handler)
    api_client = httpx.AsyncClient(transport=api_transport, base_url="https://api.github.com")
    raw_transport = httpx.MockTransport(raw_handler or (lambda r: httpx.Response(404)))
    raw_client = httpx.AsyncClient(
        transport=raw_transport, base_url="https://raw.githubusercontent.com"
    )
    return GithubReleaseClient(client=api_client, raw_client=raw_client)


async def test_finds_release_by_v_prefixed_tag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/axios/axios/releases/tags/v1.20.0"
        return httpx.Response(200, json={"body": "release notes for 1.20.0"})

    client = _client(handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")
    assert result == ReleaseNotesResult(ok=True, notes="release notes for 1.20.0")


async def test_falls_back_to_name_at_version_tag() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("query-core@5.102.8"):
            return httpx.Response(200, json={"body": "monorepo release notes"})
        return httpx.Response(404)

    client = _client(handler)
    result = await client.find_release_notes("TanStack/query", "query-core", "5.102.8")

    assert result == ReleaseNotesResult(ok=True, notes="monorepo release notes")
    assert calls == [
        "/repos/TanStack/query/releases/tags/v5.102.8",
        "/repos/TanStack/query/releases/tags/query-core@5.102.8",
    ]


async def test_falls_back_to_bare_version_tag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/1.20.0"):
            return httpx.Response(200, json={"body": "bare version notes"})
        return httpx.Response(404)

    client = _client(handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")
    assert result == ReleaseNotesResult(ok=True, notes="bare version notes")


async def test_falls_back_to_changelog_when_no_tag_matches() -> None:
    def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    def raw_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/axios/axios/main/CHANGELOG.md"
        return httpx.Response(
            200,
            text=(
                "# Changelog\n\n"
                "## 1.20.0\n\n"
                "- Fixed a bug\n\n"
                "## 1.19.0\n\n"
                "- Old release\n"
            ),
        )

    client = _client(api_handler, raw_handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")
    assert result == ReleaseNotesResult(ok=True, notes="- Fixed a bug")


async def test_tries_master_branch_when_main_missing() -> None:
    def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    def raw_handler(request: httpx.Request) -> httpx.Response:
        if "/main/" in request.url.path:
            return httpx.Response(404)
        assert request.url.path == "/axios/axios/master/CHANGELOG.md"
        return httpx.Response(200, text="## 1.20.0\n\nmaster branch notes\n")

    client = _client(api_handler, raw_handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")
    assert result == ReleaseNotesResult(ok=True, notes="master branch notes")


async def test_returns_ok_with_none_notes_when_nothing_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = _client(handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")
    assert result == ReleaseNotesResult(ok=True, notes=None)


async def test_returns_not_ok_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    result = await client.find_release_notes("axios/axios", "axios", "1.20.0")
    assert result.ok is False
    assert result.notes is None
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_github_release_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.context.github_release_client'`

- [ ] **Step 3: `app/context/github_release_client.py` 작성**

```python
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_GITHUB_API_BASE_URL = "https://api.github.com"
_RAW_CONTENT_BASE_URL = "https://raw.githubusercontent.com"
_CHANGELOG_BRANCHES = ("main", "master")


@dataclass
class ReleaseNotesResult:
    """`ok=False`는 네트워크/타임아웃 등으로 조회 자체를 완료하지 못했다는 뜻이다
    (이 경우 호출자는 캐시에 남기면 안 된다 — 일시적 실패일 뿐이다).
    `ok=True`이고 `notes=None`이면 태그/CHANGELOG를 전부 확인했지만 이 버전의
    릴리즈 노트를 확인상 찾지 못했다는 뜻이라 안전하게 캐싱할 수 있다."""

    ok: bool
    notes: str | None


def _build_changelog_section_pattern(version: str) -> re.Pattern[str]:
    escaped = re.escape(version)
    return re.compile(rf"^##\s+\[?{escaped}\]?.*$\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)


def _extract_changelog_section(text: str, version: str) -> str | None:
    pattern = _build_changelog_section_pattern(version)
    match = pattern.search(text)
    if match is None:
        return None
    section = match.group(1).strip()
    return section if section else None


class GithubReleaseClient:
    """owner/repo + 버전으로 GitHub 릴리즈 노트 또는 CHANGELOG.md 섹션을 찾는다.

    best-effort: 모든 실패(네트워크/인증/404/rate limit)는 예외를 던지지 않고
    ReleaseNotesResult로 표현한다 — 개별 패키지 조회 실패가 OfficialDocsWorkflow
    전체를 막지 않게 하기 위함이다.
    """

    def __init__(
        self,
        *,
        token: str = "",
        timeout_seconds: float = 3.0,
        client: httpx.AsyncClient | None = None,
        raw_client: httpx.AsyncClient | None = None,
    ) -> None:
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.AsyncClient(
            base_url=_GITHUB_API_BASE_URL, timeout=timeout_seconds, headers=headers
        )
        # CHANGELOG raw fetch는 별도 호스트(raw.githubusercontent.com)라 별도
        # AsyncClient를 쓴다 — 공개 레포의 raw 파일은 인증 없이도 조회 가능하다.
        self._raw_client = raw_client or httpx.AsyncClient(
            base_url=_RAW_CONTENT_BASE_URL, timeout=timeout_seconds
        )

    async def find_release_notes(
        self, owner_repo: str, name: str, version: str
    ) -> ReleaseNotesResult:
        had_transient_failure = False
        for tag in (f"v{version}", f"{name}@{version}", version):
            notes, failed = await self._try_tag(owner_repo, tag)
            if notes is not None:
                return ReleaseNotesResult(ok=True, notes=notes)
            had_transient_failure = had_transient_failure or failed

        changelog_notes, changelog_failed = await self._try_changelog(owner_repo, version)
        if changelog_notes is not None:
            return ReleaseNotesResult(ok=True, notes=changelog_notes)
        had_transient_failure = had_transient_failure or changelog_failed

        return ReleaseNotesResult(ok=not had_transient_failure, notes=None)

    async def _try_tag(self, owner_repo: str, tag: str) -> tuple[str | None, bool]:
        try:
            response = await self._client.get(f"/repos/{owner_repo}/releases/tags/{tag}")
        except httpx.HTTPError:
            logger.warning(
                "github release lookup failed owner_repo=%s tag=%s",
                owner_repo,
                tag,
                exc_info=True,
            )
            return None, True
        if response.status_code != 200:
            return None, False
        try:
            data = response.json()
        except ValueError:
            return None, False
        body = data.get("body") if isinstance(data, dict) else None
        return (body if isinstance(body, str) and body.strip() else None), False

    async def _try_changelog(self, owner_repo: str, version: str) -> tuple[str | None, bool]:
        had_transient_failure = False
        for branch in _CHANGELOG_BRANCHES:
            try:
                response = await self._raw_client.get(f"/{owner_repo}/{branch}/CHANGELOG.md")
            except httpx.HTTPError:
                logger.warning(
                    "changelog fetch failed owner_repo=%s branch=%s",
                    owner_repo,
                    branch,
                    exc_info=True,
                )
                had_transient_failure = True
                continue
            if response.status_code != 200:
                continue
            section = _extract_changelog_section(response.text, version)
            if section is not None:
                return section, False
        return None, had_transient_failure

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._raw_client.aclose()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_github_release_client.py -v`
Expected: PASS (8개 전부)

- [ ] **Step 5: 전체 테스트 스위트 회귀 확인**

Run: `uv run pytest && uv run ruff check . && uv run mypy .`
Expected: 전부 PASS/clean

- [ ] **Step 6: Commit**

```bash
git add app/context/github_release_client.py tests/test_github_release_client.py
git commit -m "feat :: GitHub 릴리즈 노트/CHANGELOG 조회하는 GithubReleaseClient 추가"
```

---

### Task 3: `RedisReleaseNotesCache` (신규)

**Files:**
- Create: `app/context/release_notes_cache.py`
- Test: `tests/test_release_notes_cache.py`

**Interfaces:**
- Consumes: 없음
- Produces: `CachedReleaseNotes(notes: str | None)`,
  `RedisReleaseNotesCache(redis, key_prefix="ai-review:release-notes:", ttl_seconds=2592000)`
  with `async def get(owner_repo: str, version: str) -> CachedReleaseNotes | None`
  (None = 캐시 미스), `async def set(owner_repo: str, version: str, notes: str | None) -> None`
  (Task 4가 사용)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_release_notes_cache.py`:

```python
from app.context.release_notes_cache import CachedReleaseNotes, RedisReleaseNotesCache


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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_release_notes_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.context.release_notes_cache'`

- [ ] **Step 3: `app/context/release_notes_cache.py` 작성**

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
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_release_notes_cache.py -v`
Expected: PASS (5개 전부)

- [ ] **Step 5: 전체 테스트 스위트 회귀 확인**

Run: `uv run pytest && uv run ruff check . && uv run mypy .`
Expected: 전부 PASS/clean

- [ ] **Step 6: Commit**

```bash
git add app/context/release_notes_cache.py tests/test_release_notes_cache.py
git commit -m "feat :: 릴리즈 노트 조회 결과 캐싱하는 RedisReleaseNotesCache 추가"
```

---

### Task 4: `OfficialDocsWorkflow` (신규 — 조정자)

**Files:**
- Create: `app/context/official_docs_workflow.py`
- Test: `tests/test_official_docs_workflow.py`

**Interfaces:**
- Consumes: `app.context.npm_lockfile_diff.extract_dependency_changes/DependencyChange` (기존),
  `app.context.npm_registry_client.DeprecationLookupResult` (Task 1),
  `app.context.release_notes_cache.CachedReleaseNotes` (Task 3),
  `app.review.schema.ChangedFile` (기존)
- Produces: `app.context.official_docs_workflow.OfficialDocsWorkflow(registry_client, release_client, cache)`
  with `async def build_evidence(changed_files: list[ChangedFile]) -> str` (Task 5가 사용)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_official_docs_workflow.py`:

```python
from app.context.npm_registry_client import DeprecationLookupResult
from app.context.official_docs_workflow import OfficialDocsWorkflow
from app.context.release_notes_cache import CachedReleaseNotes
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
    def __init__(self, results: dict[tuple[str, str], DeprecationLookupResult]) -> None:
        self._results = results

    async def check_deprecation(self, name: str, version: str) -> DeprecationLookupResult:
        return self._results.get((name, version), DeprecationLookupResult(ok=False, message=None))


class FakeReleaseClient:
    def __init__(self, results: dict[tuple[str, str], object]) -> None:
        self._results = results
        self.received: list[tuple[str, str, str]] = []

    async def find_release_notes(self, owner_repo: str, name: str, version: str) -> object:
        self.received.append((owner_repo, name, version))
        return self._results[(owner_repo, version)]


class _Result:
    def __init__(self, ok: bool, notes: str | None) -> None:
        self.ok = ok
        self.notes = notes


class FakeCache:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], CachedReleaseNotes] = {}
        self.set_calls: list[tuple[str, str, str | None]] = []

    async def get(self, owner_repo: str, version: str) -> CachedReleaseNotes | None:
        return self.store.get((owner_repo, version))

    async def set(self, owner_repo: str, version: str, notes: str | None) -> None:
        self.set_calls.append((owner_repo, version, notes))
        self.store[(owner_repo, version)] = CachedReleaseNotes(notes=notes)


async def test_build_evidence_includes_release_notes_for_bumped_dependency() -> None:
    registry = FakeRegistryClient(
        {("axios", "1.20.0"): DeprecationLookupResult(ok=True, message=None, github_repo="axios/axios")}
    )
    release_client = FakeReleaseClient(
        {("axios/axios", "1.20.0"): _Result(ok=True, notes="Fixed a security issue")}
    )
    workflow = OfficialDocsWorkflow(registry, release_client, FakeCache())
    changed_files = [
        ChangedFile(file_path="package-lock.json", status="modified", patch=_AXIOS_BUMP_PATCH)
    ]

    evidence = await workflow.build_evidence(changed_files)

    assert "axios@1.20.0" in evidence
    assert "Fixed a security issue" in evidence
    assert "공식 릴리즈 노트" in evidence


async def test_build_evidence_returns_empty_string_when_no_lockfile_changed() -> None:
    workflow = OfficialDocsWorkflow(FakeRegistryClient({}), FakeReleaseClient({}), FakeCache())
    changed_files = [ChangedFile(file_path="app/main.py", status="modified", patch="@@ -1 +1 @@")]

    assert await workflow.build_evidence(changed_files) == ""


async def test_build_evidence_skips_package_when_registry_has_no_github_repo() -> None:
    registry = FakeRegistryClient(
        {("axios", "1.20.0"): DeprecationLookupResult(ok=True, message=None, github_repo=None)}
    )
    workflow = OfficialDocsWorkflow(registry, FakeReleaseClient({}), FakeCache())
    changed_files = [
        ChangedFile(file_path="package-lock.json", status="modified", patch=_AXIOS_BUMP_PATCH)
    ]

    assert await workflow.build_evidence(changed_files) == ""


async def test_build_evidence_uses_cache_and_skips_release_client() -> None:
    registry = FakeRegistryClient(
        {("axios", "1.20.0"): DeprecationLookupResult(ok=True, message=None, github_repo="axios/axios")}
    )
    release_client = FakeReleaseClient({})  # 호출되면 KeyError로 즉시 실패 — 캐시 hit 검증용
    cache = FakeCache()
    cache.store[("axios/axios", "1.20.0")] = CachedReleaseNotes(notes="cached notes")
    workflow = OfficialDocsWorkflow(registry, release_client, cache)
    changed_files = [
        ChangedFile(file_path="package-lock.json", status="modified", patch=_AXIOS_BUMP_PATCH)
    ]

    evidence = await workflow.build_evidence(changed_files)

    assert "cached notes" in evidence
    assert release_client.received == []


async def test_build_evidence_does_not_cache_transient_release_client_failure() -> None:
    registry = FakeRegistryClient(
        {("axios", "1.20.0"): DeprecationLookupResult(ok=True, message=None, github_repo="axios/axios")}
    )
    release_client = FakeReleaseClient({("axios/axios", "1.20.0"): _Result(ok=False, notes=None)})
    cache = FakeCache()
    workflow = OfficialDocsWorkflow(registry, release_client, cache)
    changed_files = [
        ChangedFile(file_path="package-lock.json", status="modified", patch=_AXIOS_BUMP_PATCH)
    ]

    evidence = await workflow.build_evidence(changed_files)

    assert evidence == ""
    assert cache.set_calls == []


async def test_build_evidence_caches_confirmed_absent_notes() -> None:
    registry = FakeRegistryClient(
        {("axios", "1.20.0"): DeprecationLookupResult(ok=True, message=None, github_repo="axios/axios")}
    )
    release_client = FakeReleaseClient({("axios/axios", "1.20.0"): _Result(ok=True, notes=None)})
    cache = FakeCache()
    workflow = OfficialDocsWorkflow(registry, release_client, cache)
    changed_files = [
        ChangedFile(file_path="package-lock.json", status="modified", patch=_AXIOS_BUMP_PATCH)
    ]

    evidence = await workflow.build_evidence(changed_files)

    assert evidence == ""
    assert cache.set_calls == [("axios/axios", "1.20.0", None)]
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_official_docs_workflow.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.context.official_docs_workflow'`

- [ ] **Step 3: `app/context/official_docs_workflow.py` 작성**

```python
from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import Protocol

from app.context.npm_lockfile_diff import DependencyChange, extract_dependency_changes
from app.context.npm_registry_client import DeprecationLookupResult
from app.context.release_notes_cache import CachedReleaseNotes
from app.review.schema import ChangedFile

logger = logging.getLogger(__name__)

_LOCKFILE_NAME = "package-lock.json"

# 프롬프트에 그대로 들어가는 텍스트라 4단계(deprecated boolean 확인, 상한 50개)
# 보다 훨씬 작게 잡는다 — 토큰 예산을 직접 소비하기 때문이다.
_MAX_PACKAGES = 10
_MAX_NOTES_CHARS_PER_PACKAGE = 800
_MAX_TOTAL_CHARS = 3000
_HEADER = "\n\n#### 의존성 버전 변경 근거 (공식 릴리즈 노트)"


class RegistryClient(Protocol):
    async def check_deprecation(self, name: str, version: str) -> DeprecationLookupResult: ...


class ReleaseNotesResultLike(Protocol):
    ok: bool
    notes: str | None


class ReleaseClient(Protocol):
    async def find_release_notes(
        self, owner_repo: str, name: str, version: str
    ) -> ReleaseNotesResultLike: ...


class ReleaseNotesCache(Protocol):
    async def get(self, owner_repo: str, version: str) -> CachedReleaseNotes | None: ...
    async def set(self, owner_repo: str, version: str, notes: str | None) -> None: ...


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


class OfficialDocsWorkflow:
    """package-lock.json 변경분의 npm 의존성에 대해 GitHub 릴리즈 노트/CHANGELOG를
    찾아 근거 텍스트를 만든다. 판단은 하지 않는다 — breaking change 여부는 메인
    리뷰 LLM이 이 텍스트를 보고 직접 판단한다(7.5절: Workflow = 근거 수집,
    Code Review Model = 최종 판단).

    best-effort: 실패해도 빈 문자열을 반환한다(리뷰 자체를 막지 않는다).
    """

    def __init__(
        self,
        registry_client: RegistryClient,
        release_client: ReleaseClient,
        cache: ReleaseNotesCache,
    ) -> None:
        self._registry_client = registry_client
        self._release_client = release_client
        self._cache = cache

    async def build_evidence(self, changed_files: list[ChangedFile]) -> str:
        entries: list[str] = []
        for file in changed_files:
            if PurePosixPath(file.file_path).name != _LOCKFILE_NAME:
                continue
            entries.extend(await self._collect_lockfile_evidence(file))
        if not entries:
            return ""
        return _HEADER + "\n" + self._assemble(entries)

    async def _collect_lockfile_evidence(self, file: ChangedFile) -> list[str]:
        try:
            changes = extract_dependency_changes(file.patch)
        except Exception:
            logger.warning(
                "failed to parse lockfile patch path=%s", file.file_path, exc_info=True
            )
            return []

        deduped: list[DependencyChange] = []
        seen: set[tuple[str, str]] = set()
        for change in changes:
            key = (change.name, change.version)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(change)

        if len(deduped) > _MAX_PACKAGES:
            logger.info(
                "official docs workflow capped at %d packages for %s, skipping %d remaining",
                _MAX_PACKAGES,
                file.file_path,
                len(deduped) - _MAX_PACKAGES,
            )
            deduped = deduped[:_MAX_PACKAGES]

        entries: list[str] = []
        for change in deduped:
            notes = await self._find_notes(change.name, change.version)
            if notes is None:
                continue
            entries.append(
                f"{change.name}@{change.version}:\n"
                f"{_truncate(notes, _MAX_NOTES_CHARS_PER_PACKAGE)}"
            )
        return entries

    async def _find_notes(self, name: str, version: str) -> str | None:
        try:
            lookup = await self._registry_client.check_deprecation(name, version)
        except Exception:
            logger.warning(
                "npm registry lookup failed name=%s version=%s", name, version, exc_info=True
            )
            return None
        if not lookup.ok or lookup.github_repo is None:
            return None

        owner_repo = lookup.github_repo
        try:
            cached = await self._cache.get(owner_repo, version)
        except Exception:
            logger.warning(
                "release notes cache read failed owner_repo=%s version=%s",
                owner_repo,
                version,
                exc_info=True,
            )
            cached = None
        if cached is not None:
            return cached.notes

        try:
            result = await self._release_client.find_release_notes(owner_repo, name, version)
        except Exception:
            logger.warning(
                "github release lookup failed owner_repo=%s version=%s",
                owner_repo,
                version,
                exc_info=True,
            )
            return None

        if not result.ok:
            return None  # 일시적 실패 — 캐시에 남기지 않는다

        try:
            await self._cache.set(owner_repo, version, result.notes)
        except Exception:
            logger.warning(
                "release notes cache write failed owner_repo=%s version=%s",
                owner_repo,
                version,
                exc_info=True,
            )

        return result.notes

    def _assemble(self, entries: list[str]) -> str:
        assembled: list[str] = []
        total = 0
        for entry in entries:
            remaining = _MAX_TOTAL_CHARS - total
            if remaining <= 0:
                break
            if len(entry) > remaining:
                entry = entry[:remaining].rstrip() + "..."
            assembled.append(entry)
            total += len(entry)
        return "\n\n".join(assembled)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_official_docs_workflow.py -v`
Expected: PASS (6개 전부)

- [ ] **Step 5: 전체 테스트 스위트 회귀 확인**

Run: `uv run pytest && uv run ruff check . && uv run mypy .`
Expected: 전부 PASS/clean

- [ ] **Step 6: Commit**

```bash
git add app/context/official_docs_workflow.py tests/test_official_docs_workflow.py
git commit -m "feat :: OfficialDocsWorkflow로 의존성 버전 변경 근거(릴리즈 노트) 생성"
```

---

### Task 5: 파이프라인 연결 + 설정/배선

**Files:**
- Modify: `app/review/pipeline.py`
- Modify: `app/core/config.py`
- Modify: `app/main.py`
- Modify: `.env.example`
- Test: `tests/test_review_pipeline.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `app.context.official_docs_workflow.OfficialDocsWorkflow` (Task 4),
  `app.context.github_release_client.GithubReleaseClient` (Task 2),
  `app.context.release_notes_cache.RedisReleaseNotesCache` (Task 3),
  `app.context.npm_registry_client.NpmRegistryClient` (Task 1, 기존 클래스)
- Produces: `ReviewPipeline.__init__`의 새 파라미터
  `official_docs_workflow: OfficialDocsContextBuilder | None = None`,
  `Settings.official_docs_workflow_enabled: bool`, `Settings.github_token: str`

- [ ] **Step 1: 실패하는 테스트 작성 — `tests/test_review_pipeline.py`**

파일 끝(`class FakeDependencyResolver` 이후 관련 테스트들 다음)에 추가:

```python
class FakeOfficialDocsWorkflow:
    def __init__(self, evidence: str) -> None:
        self._evidence = evidence
        self.received_changed_files: list[ChangedFile] | None = None

    async def build_evidence(self, changed_files: list[ChangedFile]) -> str:
        self.received_changed_files = changed_files
        return self._evidence


async def test_run_includes_official_docs_evidence_in_prompt() -> None:
    fake_workflow = FakeOfficialDocsWorkflow(
        "\n\n#### 의존성 버전 변경 근거 (공식 릴리즈 노트)\naxios@1.20.0:\nFixed a bug"
    )
    fake_llm = FakeLLM(ReviewModelOutput(summary="ok", reviews=[]))
    pipeline = ReviewPipeline(
        fake_llm, model_version="v", prompt_version="v1", official_docs_workflow=fake_workflow
    )
    event = _event()

    await pipeline.run(event)

    assert fake_llm.received is not None
    user_message = fake_llm.received[1]["content"]
    assert "공식 릴리즈 노트" in user_message
    assert "axios@1.20.0" in user_message
    assert fake_workflow.received_changed_files == event.changed_files


async def test_run_skips_official_docs_workflow_when_no_targets() -> None:
    # analyze()는 package-lock.json을 targets에서 제외하므로, lockfile만 바뀐
    # PR은 targets == []다 — official_docs_workflow는 코드 변경 자체가 있을 때만
    # 의미가 있으므로(4단계 dependency_resolver와 달리) 이 경로에서는 호출되지
    # 않아야 한다.
    fake_workflow = FakeOfficialDocsWorkflow("should not appear")
    fake_llm = FakeLLM(ReviewModelOutput(summary="unused", reviews=[]))
    pipeline = ReviewPipeline(
        fake_llm, model_version="v", prompt_version="v1", official_docs_workflow=fake_workflow
    )
    event = _event()
    event.changed_files = [
        ChangedFile(file_path="package-lock.json", status="modified", patch="@@ -1 +1 @@")
    ]

    await pipeline.run(event)

    assert fake_workflow.received_changed_files is None
    assert fake_llm.call_count == 0


async def test_run_continues_when_official_docs_workflow_raises() -> None:
    class BoomWorkflow:
        async def build_evidence(self, changed_files: list[ChangedFile]) -> str:
            raise RuntimeError("boom")

    fake_llm = FakeLLM(ReviewModelOutput(summary="ok", reviews=[]))
    pipeline = ReviewPipeline(
        fake_llm, model_version="v", prompt_version="v1", official_docs_workflow=BoomWorkflow()
    )

    result = await pipeline.run(_event())

    assert isinstance(result, ReviewCompletedEvent)
    assert result.summary == "ok"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_review_pipeline.py -v -k official_docs`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'official_docs_workflow'`

- [ ] **Step 3: `app/review/pipeline.py` 수정**

`DependencyContextResolver` Protocol 정의 바로 다음에 추가:

```python
class OfficialDocsContextBuilder(Protocol):
    async def build_evidence(self, changed_files: list[ChangedFile]) -> str:
        """changed_files 중 lockfile 변경분에서 GitHub 릴리즈 노트/CHANGELOG
        근거를 찾아 텍스트로 반환한다. 실패 시 빈 문자열을 반환한다(리뷰
        자체를 막지 않는다). 판단은 하지 않는다 — 텍스트 그대로 메인 리뷰
        LLM 프롬프트에 포함되며, breaking change 여부 판단은 LLM이 한다.
        """
        ...
```

`ReviewPipeline.__init__`의 시그니처와 본문을 다음으로 교체:

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
        official_docs_workflow: OfficialDocsContextBuilder | None = None,
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
        self._official_docs_workflow = official_docs_workflow
```

`run()`에서 다음 줄을 찾는다:

```python
        related_context = await self._retrieve_related_context(event.repository_id, targets)
        api_spec_context = await self._retrieve_api_spec_context(event, targets)
        messages = self._build_messages(event, targets, related_context, api_spec_context)
```

다음으로 교체:

```python
        related_context = await self._retrieve_related_context(event.repository_id, targets)
        api_spec_context = await self._retrieve_api_spec_context(event, targets)
        official_docs_context = await self._build_official_docs_context(event)
        messages = self._build_messages(
            event, targets, related_context, api_spec_context, official_docs_context
        )
```

`_retrieve_api_spec_context` 메서드 바로 다음에 새 메서드 추가:

```python
    async def _build_official_docs_context(self, event: ReviewRequestedEvent) -> str:
        """package-lock.json 변경분에 대한 GitHub 릴리즈 노트/CHANGELOG 근거를
        만든다. 판단은 하지 않는다 — 이 텍스트를 보고 breaking change 여부를
        판단하는 건 메인 리뷰 LLM의 몫이다(7.5절).
        """
        if self._official_docs_workflow is None:
            return ""
        try:
            return await self._official_docs_workflow.build_evidence(event.changed_files)
        except Exception:
            logger.warning("official docs workflow failed", exc_info=True)
            return ""
```

`_build_messages`의 시그니처와 본문을 다음으로 교체:

```python
    def _build_messages(
        self,
        event: ReviewRequestedEvent,
        targets: list[ReviewTarget],
        related_context: dict[str, list[ChunkSearchResult]],
        api_spec_context: str = "",
        official_docs_context: str = "",
    ) -> list[ChatMessage]:
        blocks = [
            self._render_target(t, related_context.get(t.file_path, [])) for t in targets
        ]
        context = build_context(event.context_files)
        diff_budget = max(0, _MAX_DIFF_TOTAL_CHARS - len(context))
        diff = _truncate_diff_blocks(blocks, max_total_chars=diff_budget)
        user = f"## Project Context\n{context}\n\n## Changes\n{diff}" if context else diff
        user += api_spec_context
        user += official_docs_context
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_review_pipeline.py -v`
Expected: PASS 전부 (기존 + 신규 3개)

- [ ] **Step 5: `app/core/config.py`에 설정 추가**

`dependency_check_enabled: bool = False` 다음, `evaluation_enabled` 블록
앞에 추가:

```python
    # 기본 False: GitHub API 호출이 필요 없는 레포/환경에서도 앱이 정상
    # 기동해야 한다. GITHUB_TOKEN 없이도 동작은 하지만(미인증 60회/시간),
    # 프로덕션에서는 GITHUB_TOKEN도 함께 설정하는 걸 권장한다.
    official_docs_workflow_enabled: bool = False
    github_token: str = ""
```

- [ ] **Step 6: `app/main.py` 수정**

`dependency_resolver = None` ... 블록 전체를 다음으로 교체 (`npm_registry_client`를
두 optional 기능이 공유하도록 재구성). **주의**: `npm_registry_client = None`
초기화는 이미 파일 앞부분(`qdrant_client = None` 옆)에 있으므로 여기서
다시 선언하지 않는다 — 아래 블록은 그 변수에 조건부로 값을 대입만 한다:

```python
    if settings.dependency_check_enabled or settings.official_docs_workflow_enabled:
        from app.context.npm_registry_client import NpmRegistryClient

        npm_registry_client = NpmRegistryClient()

    dependency_resolver = None
    if settings.dependency_check_enabled:
        from app.context.dependency_resolver import DependencyResolver
        from app.context.npm_deprecation_cache import RedisNpmDeprecationCache

        # redis.asyncio.Redis의 실제 타입 스텁이 RedisLike보다 훨씬 넓어 구조적으로
        # 완전히 일치하지 않지만, set/get을 문자열 인자로만 호출하므로 런타임에는 호환된다.
        npm_deprecation_cache = RedisNpmDeprecationCache(redis_client)  # type: ignore[arg-type]
        assert npm_registry_client is not None
        dependency_resolver = DependencyResolver(npm_registry_client, npm_deprecation_cache)

    github_release_client = None
    official_docs_workflow = None
    if settings.official_docs_workflow_enabled:
        from app.context.github_release_client import GithubReleaseClient
        from app.context.official_docs_workflow import OfficialDocsWorkflow
        from app.context.release_notes_cache import RedisReleaseNotesCache

        github_release_client = GithubReleaseClient(token=settings.github_token)
        # redis.asyncio.Redis의 실제 타입 스텁이 RedisLike보다 훨씬 넓어 구조적으로
        # 완전히 일치하지 않지만, set/get을 문자열 인자로만 호출하므로 런타임에는 호환된다.
        release_notes_cache = RedisReleaseNotesCache(redis_client)  # type: ignore[arg-type]
        assert npm_registry_client is not None
        official_docs_workflow = OfficialDocsWorkflow(
            npm_registry_client, github_release_client, release_notes_cache
        )
```

**주의**: 이 블록은 원래 `redis_client = create_redis_client(settings)`
(그리고 `notion_link_store` 블록) 다음, `evaluation_repository`/
`evaluation_engine` 블록 이전에 위치해야 한다 — 원래 `dependency_resolver`
블록이 있던 바로 그 자리다. 위치를 옮기지 않는다.

`pipeline = ReviewPipeline(` 호출에 파라미터 추가:

```python
    pipeline = ReviewPipeline(
        llm_client,
        model_version=settings.llm_model,
        prompt_version="v1",
        retriever=retriever,
        api_spec_retriever=api_spec_retriever,
        notion_link_store=notion_link_store,
        dependency_resolver=dependency_resolver,
        official_docs_workflow=official_docs_workflow,
    )
```

`finally:` 블록의 `if npm_registry_client is not None: await npm_registry_client.aclose()`
다음 줄에 추가:

```python
        if github_release_client is not None:
            await github_release_client.aclose()
```

- [ ] **Step 7: `tests/test_main.py`에 배선 테스트 추가**

`FakeNpmRegistryClient` 클래스 다음, `test_lifespan_wires_dependency_resolver_when_enabled`
근처에 추가:

```python
class FakeGithubReleaseClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.closed = False

    async def find_release_notes(self, owner_repo: str, name: str, version: str) -> object:
        raise AssertionError("should not be called in this test")

    async def aclose(self) -> None:
        self.closed = True


async def test_lifespan_wires_official_docs_workflow_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("OFFICIAL_DOCS_WORKFLOW_ENABLED", "true")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")

    fake_producer = FakeStartStop()
    fake_consumer = FakeConsumerSource()
    fake_comment_answer_consumer = FakeConsumerSource()
    fake_github_release_client = FakeGithubReleaseClient()
    monkeypatch.setattr("app.main.create_producer", lambda settings: fake_producer)
    monkeypatch.setattr("app.main.create_consumer", lambda settings: fake_consumer)
    monkeypatch.setattr(
        "app.main.create_comment_answer_consumer",
        lambda settings: fake_comment_answer_consumer,
    )
    monkeypatch.setattr(
        "app.context.github_release_client.GithubReleaseClient",
        lambda **kwargs: fake_github_release_client,
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

        assert captured_kwargs.get("official_docs_workflow") is not None
        assert fake_github_release_client.closed
    finally:
        get_settings.cache_clear()


async def test_lifespan_leaves_official_docs_workflow_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")
    # OFFICIAL_DOCS_WORKFLOW_ENABLED를 아예 설정하지 않는다 (기본 False 확인)

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

        assert captured_kwargs.get("official_docs_workflow") is None
    finally:
        get_settings.cache_clear()


async def test_lifespan_shares_npm_registry_client_across_both_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # dependency_check_enabled와 official_docs_workflow_enabled를 둘 다 켰을 때
    # NpmRegistryClient가 한 번만 생성되는지(패키지당 registry 호출 통합 계약)
    # 확인한다.
    get_settings.cache_clear()
    monkeypatch.setenv("KAFKA_CONSUMER_ENABLED", "true")
    monkeypatch.setenv("DEPENDENCY_CHECK_ENABLED", "true")
    monkeypatch.setenv("OFFICIAL_DOCS_WORKFLOW_ENABLED", "true")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_SECONDS", "0.05")

    fake_producer = FakeStartStop()
    fake_consumer = FakeConsumerSource()
    fake_comment_answer_consumer = FakeConsumerSource()
    fake_npm_registry_client = FakeNpmRegistryClient()
    fake_github_release_client = FakeGithubReleaseClient()
    construction_count = 0

    def _construct_npm_registry_client(*args: object, **kwargs: object) -> object:
        nonlocal construction_count
        construction_count += 1
        return fake_npm_registry_client

    monkeypatch.setattr("app.main.create_producer", lambda settings: fake_producer)
    monkeypatch.setattr("app.main.create_consumer", lambda settings: fake_consumer)
    monkeypatch.setattr(
        "app.main.create_comment_answer_consumer",
        lambda settings: fake_comment_answer_consumer,
    )
    monkeypatch.setattr(
        "app.context.npm_registry_client.NpmRegistryClient", _construct_npm_registry_client
    )
    monkeypatch.setattr(
        "app.context.github_release_client.GithubReleaseClient",
        lambda **kwargs: fake_github_release_client,
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

        assert construction_count == 1
        assert captured_kwargs.get("dependency_resolver") is not None
        assert captured_kwargs.get("official_docs_workflow") is not None
    finally:
        get_settings.cache_clear()
```

- [ ] **Step 8: 테스트 통과 확인**

Run: `uv run pytest tests/test_main.py -v`
Expected: PASS 전부 (기존 + 신규 3개)

- [ ] **Step 9: qdrant_client 회귀 방지 확인**

Run: `uv run python -c "import app.main; import sys; assert 'qdrant_client' not in sys.modules"`
Expected: 에러 없이 종료

- [ ] **Step 10: `.env.example`에 항목 추가**

`KAFKA_REVIEW_FEEDBACK_TOPIC=pr.comment.reflected` 다음 줄에 추가:

```
# 미설정이어도 동작하지만(미인증 60회/시간), 프로덕션에서는 설정 권장
OFFICIAL_DOCS_WORKFLOW_ENABLED=false
GITHUB_TOKEN=
```

- [ ] **Step 11: 전체 테스트 스위트 + 린트 + 타입체크**

Run: `uv run pytest && uv run ruff check . && uv run mypy .`
Expected: 전부 PASS/clean

- [ ] **Step 12: Commit**

```bash
git add app/review/pipeline.py app/core/config.py app/main.py .env.example tests/test_review_pipeline.py tests/test_main.py
git commit -m "feat :: official_docs_workflow_enabled 배선 - OfficialDocsWorkflow 파이프라인 연결"
```
