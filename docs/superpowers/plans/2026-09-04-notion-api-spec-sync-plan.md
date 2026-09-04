# Notion API 명세 Sync (3단계) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** DOVI.md에 링크된 Notion API 명세 데이터베이스를, openapi.yaml/swagger.json이 레포에 없을 때만 fallback으로 동기화·검색해서 PR 리뷰 컨텍스트에 추가한다.

**Architecture:** PR 리뷰 시점에 DOVI.md에서 Notion 링크를 발견해 Redis에 기록(swagger가 없을 때만) → 별도 CLI(`scripts/sync_api_spec.py`)가 주기적으로 Redis의 알려진 링크들을 Notion REST API로 조회해 Qdrant에 임베딩 저장 → 리뷰 시점에 `ApiSpecRetriever`가 (swagger 없을 때만) 그 Qdrant collection을 검색해 프롬프트에 추가.

**Tech Stack:** httpx(Notion REST API 호출), 기존 CodeRankEmbedClient(임베딩 재사용), Qdrant(신규 collection), Redis(신규 링크 저장, 기존 dedup store와 같은 클라이언트 재사용).

---

## 0. 범위 확정 (Notion 조사로 밝혀진 배경)

`app/review/context.py`의 `_priority()`가 이미 `openapi.yaml`/`openapi.yml`/`swagger.json`(우선순위 1)과 `docs/**`(우선순위 3)를 `event.contextFiles`에서 처리하고 있고, github-app의 `pr-data-collector.service.ts`도 이미 이 파일들을 contextFiles 후보로 수집한다. 즉 노션 로드맵 7.3절의 "1차/2차"는 1단계 MVP 때 이미 끝나 있다. 이번 작업의 범위는 "3차: DOVI.md 링크 기반 Notion sync" 하나뿐이며, "4차: Controller/DTO 자동 추론"은 범위 밖이다.

**핵심 우선순위 규칙 (이중 안전장치):**
- Notion 링크는 **swagger/openapi가 `event.contextFiles`에 없을 때만** 저장한다 (저장 단계 게이트).
- `ApiSpecRetriever`도 **swagger/openapi가 이번 이벤트의 contextFiles에 없을 때만** 호출한다 (검색 단계 게이트).
- 두 게이트 모두 동일한 판정 함수(`context_files`에 priority 1 파일이 있는지)를 공유해 판단이 어긋나지 않게 한다.

**Notion 데이터베이스 스키마 (DOVI.md 링크 대상이 따라야 하는 최소 속성):**
```
Method    (select 또는 text: GET/POST/PUT/PATCH/DELETE)
Path      (text: /api/users/:id)
Summary   (text: 한 줄 설명)
Request Schema   (text, 선택)
Response Schema  (text, 선택)
Auth      (text, 선택: 필요한 인증 방식)
```
DOVI.md 예시(7.2절 기존 예시에 API Specification 섹션이 이미 있음):
```markdown
## API Specification
- Notion API Spec: https://www.notion.so/xxxxxxxx
```

---

## 1. File Structure

```
app/
  notion/
    client.py              # NotionClient — httpx 기반, query_database()만 제공
    schema.py               # ApiSpecEntry (파싱된 endpoint 1건)
  context/
    api_spec_link_store.py  # Redis 기반 (repository_id -> notion_database_url) 저장/조회
    api_spec_retriever.py   # ApiSpecRetriever — ProjectContextRetriever와 동형 구조
  rag/
    api_spec_vector_store.py  # ApiSpecVectorStore — QdrantVectorStore와 병렬 구조(payload 모양 다름)
  review/
    context.py               # has_openapi_spec() 헬퍼 추가 + Notion 링크 파싱/저장 훅 추가
    pipeline.py               # api_spec_retriever 배선 + 프롬프트 섹션 추가
scripts/
  sync_api_spec.py           # cron으로 도는 CLI
tests/
  test_notion_client.py
  test_api_spec_link_store.py
  test_api_spec_vector_store.py
  test_api_spec_retriever.py
  test_context.py            # has_openapi_spec, notion 링크 파싱 테스트 추가
  test_sync_api_spec.py
```

---

### Task 1: `has_openapi_spec()` 판정 헬퍼 + Notion 링크 파싱

**Files:**
- Modify: `app/review/context.py`
- Test: `tests/test_context.py`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
from app.review.context import extract_notion_api_spec_link, has_openapi_spec
from app.review.schema import ContextFile


def test_has_openapi_spec_true_for_openapi_yaml() -> None:
    files = [ContextFile(path="openapi.yaml", content="")]
    assert has_openapi_spec(files) is True


def test_has_openapi_spec_true_for_swagger_json() -> None:
    files = [ContextFile(path="swagger.json", content="")]
    assert has_openapi_spec(files) is True


def test_has_openapi_spec_false_when_absent() -> None:
    files = [ContextFile(path="README.md", content="")]
    assert has_openapi_spec(files) is False


def test_extract_notion_api_spec_link_from_dovi_md() -> None:
    dovi = ContextFile(
        path="DOVI.md",
        content="## API Specification\n- Notion API Spec: https://www.notion.so/abcdef1234567890abcdef1234567890\n",
    )
    assert (
        extract_notion_api_spec_link([dovi])
        == "https://www.notion.so/abcdef1234567890abcdef1234567890"
    )


def test_extract_notion_api_spec_link_returns_none_without_dovi_md() -> None:
    assert extract_notion_api_spec_link([ContextFile(path="README.md", content="x")]) is None


def test_extract_notion_api_spec_link_returns_none_without_link_line() -> None:
    dovi = ContextFile(path="DOVI.md", content="## API Specification\n(아직 없음)\n")
    assert extract_notion_api_spec_link([dovi]) is None
```

- [ ] **Step 2: 테스트 실행해 실패 확인**

Run: `uv run pytest tests/test_context.py -k "openapi_spec or notion_api_spec_link" -v`
Expected: FAIL with `ImportError: cannot import name 'has_openapi_spec'`

- [ ] **Step 3: 구현**

`app/review/context.py`에 추가:

```python
import re

_OPENAPI_FILENAMES = {"openapi.yaml", "openapi.yml", "swagger.json"}
_NOTION_LINK_PATTERN = re.compile(
    r"Notion API Spec:\s*(https://(?:www\.)?notion\.so/\S+)", re.IGNORECASE
)


def has_openapi_spec(context_files: list[ContextFile]) -> bool:
    """swagger/openapi가 이미 있으면 Notion API 명세 fallback을 쓰지 않는다."""
    return any(
        PurePosixPath(f.path.lower()).name in _OPENAPI_FILENAMES for f in context_files
    )


def extract_notion_api_spec_link(context_files: list[ContextFile]) -> str | None:
    """DOVI.md의 '## API Specification' 섹션에서 Notion 링크를 찾는다.

    swagger가 있는지 여부는 호출자(review pipeline)가 has_openapi_spec()로 먼저
    판단해서, 이 함수는 링크 파싱 자체에만 집중한다 (단일 책임).
    """
    dovi = next(
        (f for f in context_files if PurePosixPath(f.path.lower()).name == "dovi.md"),
        None,
    )
    if dovi is None:
        return None
    match = _NOTION_LINK_PATTERN.search(dovi.content)
    return match.group(1) if match else None
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_context.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/review/context.py tests/test_context.py
git commit -m "feat :: DOVI.md Notion API 명세 링크 파싱 및 swagger 존재 판정 추가"
```

---

### Task 2: Notion 링크 저장소 (Redis)

**Files:**
- Create: `app/context/api_spec_link_store.py`
- Test: `tests/test_api_spec_link_store.py`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
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
```

- [ ] **Step 2: 테스트 실행해 실패 확인**

Run: `uv run pytest tests/test_api_spec_link_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.context'`

- [ ] **Step 3: 구현**

```python
# app/context/api_spec_link_store.py
from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol


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

    def __init__(self, redis: RedisLike, *, key_prefix: str = "ai-review:notion-api-spec-link:") -> None:
        self._redis = redis
        self._key_prefix = key_prefix

    def _key(self, repository_id: int) -> str:
        return f"{self._key_prefix}{repository_id}"

    async def save(self, *, repository_id: int, notion_database_url: str) -> None:
        await self._redis.set(self._key(repository_id), notion_database_url)

    async def get(self, *, repository_id: int) -> str | None:
        value = await self._redis.get(self._key(repository_id))
        return value if isinstance(value, str) else None

    async def list_all(self) -> list[tuple[int, str]]:
        keys = await self._redis.keys(f"{self._key_prefix}*")
        result: list[tuple[int, str]] = []
        for key in keys:  # type: ignore[union-attr]
            key_str = key if isinstance(key, str) else key.decode()
            repository_id = int(key_str.removeprefix(self._key_prefix))
            url = await self.get(repository_id=repository_id)
            if url is not None:
                result.append((repository_id, url))
        return result
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_api_spec_link_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/context/api_spec_link_store.py tests/test_api_spec_link_store.py
git commit -m "feat :: 레포별 Notion API 명세 링크 Redis 저장소 추가"
```

---

### Task 3: 리뷰 파이프라인에서 링크 발견/저장 훅 연결

**Files:**
- Modify: `app/review/pipeline.py`
- Test: `tests/test_review_pipeline.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`ReviewPipeline`에 `notion_link_store: NotionLinkStore | None = None`을 옵션으로 추가하고, `run()` 안에서 swagger가 없고 Notion 링크가 있으면 저장하도록 한다.

```python
class FakeNotionLinkStore:
    def __init__(self) -> None:
        self.saved: list[tuple[int, str]] = []

    async def save(self, *, repository_id: int, notion_database_url: str) -> None:
        self.saved.append((repository_id, notion_database_url))

    async def get(self, *, repository_id: int) -> str | None:
        return None

    async def list_all(self) -> list[tuple[int, str]]:
        return []


async def test_run_saves_notion_link_when_no_swagger_present() -> None:
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=[]))
    link_store = FakeNotionLinkStore()
    event = _event()
    event.context_files = [
        ContextFile(
            path="DOVI.md",
            content="## API Specification\n- Notion API Spec: https://notion.so/abc\n",
        )
    ]
    pipeline = ReviewPipeline(
        fake, model_version="v", prompt_version="v1", notion_link_store=link_store
    )

    await pipeline.run(event)

    assert link_store.saved == [(42, "https://notion.so/abc")]


async def test_run_does_not_save_notion_link_when_swagger_present() -> None:
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=[]))
    link_store = FakeNotionLinkStore()
    event = _event()
    event.context_files = [
        ContextFile(path="openapi.yaml", content="..."),
        ContextFile(
            path="DOVI.md",
            content="## API Specification\n- Notion API Spec: https://notion.so/abc\n",
        ),
    ]
    pipeline = ReviewPipeline(
        fake, model_version="v", prompt_version="v1", notion_link_store=link_store
    )

    await pipeline.run(event)

    assert link_store.saved == []
```

(`_event()` 헬퍼와 `ReviewRequestedEvent`에 `context_files: list[ContextFile] = []` 필드가 이미 있는지 확인 — 없다면 이 Task에서 스키마에 추가한다. `ReviewCompletedEvent`/`_build_messages`는 이미 `contextFiles` 개념을 알고 있을 가능성이 높으므로, 이번 태스크 착수 전에 `app/review/schema.py`의 `ReviewRequestedEvent`를 다시 읽어 실제 필드명을 확인한다.)

- [ ] **Step 2: 테스트 실행해 실패 확인**

Run: `uv run pytest tests/test_review_pipeline.py -k notion_link -v`
Expected: FAIL (`notion_link_store` 인자 없음)

- [ ] **Step 3: 구현**

`app/review/pipeline.py`의 `ReviewPipeline.__init__`에 파라미터 추가:

```python
from app.context.api_spec_link_store import NotionLinkStore
from app.review.context import extract_notion_api_spec_link, has_openapi_spec

class ReviewPipeline:
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
    ) -> None:
        ...
        self._notion_link_store = notion_link_store
```

`run()` 초입, `targets = analyze(event)` 다음 줄쯤에 추가:

```python
        await self._maybe_save_notion_link(event)
```

새 메서드:

```python
    async def _maybe_save_notion_link(self, event: ReviewRequestedEvent) -> None:
        if self._notion_link_store is None:
            return
        if has_openapi_spec(event.context_files):
            return
        link = extract_notion_api_spec_link(event.context_files)
        if link is None:
            return
        try:
            await self._notion_link_store.save(
                repository_id=event.repository_id, notion_database_url=link
            )
        except Exception:
            logger.warning("failed to save notion api spec link", exc_info=True)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_review_pipeline.py -v`
Expected: PASS, 기존 테스트도 회귀 없이 통과

- [ ] **Step 5: Commit**

```bash
git add app/review/pipeline.py tests/test_review_pipeline.py
git commit -m "feat :: 리뷰 파이프라인에서 Notion API 명세 링크 자동 발견/저장"
```

---

### Task 4: Notion REST API 클라이언트

**Files:**
- Create: `app/notion/client.py`
- Create: `app/notion/schema.py`
- Test: `tests/test_notion_client.py`

- [ ] **Step 1: 실패하는 테스트 작성**

httpx의 `MockTransport`로 실제 네트워크 없이 검증한다.

```python
import httpx
import pytest

from app.notion.client import NotionClient
from app.notion.schema import ApiSpecEntry


def _transport(response_json: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_json)

    return httpx.MockTransport(handler)


async def test_query_database_parses_rows_into_entries() -> None:
    response_json = {
        "results": [
            {
                "properties": {
                    "Method": {"select": {"name": "GET"}},
                    "Path": {"rich_text": [{"plain_text": "/api/users/:id"}]},
                    "Summary": {"rich_text": [{"plain_text": "유저 단건 조회"}]},
                    "Request Schema": {"rich_text": []},
                    "Response Schema": {"rich_text": [{"plain_text": "{id, name}"}]},
                    "Auth": {"rich_text": [{"plain_text": "Bearer"}]},
                }
            }
        ],
        "has_more": False,
        "next_cursor": None,
    }
    client = NotionClient(
        token="fake-token", http_client=httpx.AsyncClient(transport=_transport(response_json))
    )

    entries = await client.query_database("db-id")

    assert entries == [
        ApiSpecEntry(
            method="GET",
            path="/api/users/:id",
            summary="유저 단건 조회",
            request_schema="",
            response_schema="{id, name}",
            auth="Bearer",
        )
    ]


async def test_query_database_returns_empty_list_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = NotionClient(
        token="fake-token", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    assert await client.query_database("missing-db") == []
```

- [ ] **Step 2: 테스트 실행해 실패 확인**

Run: `uv run pytest tests/test_notion_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.notion'`

- [ ] **Step 3: 구현**

```python
# app/notion/schema.py
from pydantic import BaseModel


class ApiSpecEntry(BaseModel):
    method: str
    path: str
    summary: str
    request_schema: str = ""
    response_schema: str = ""
    auth: str = ""

    def to_text(self) -> str:
        lines = [f"{self.method} {self.path}", self.summary]
        if self.request_schema:
            lines.append(f"Request: {self.request_schema}")
        if self.response_schema:
            lines.append(f"Response: {self.response_schema}")
        if self.auth:
            lines.append(f"Auth: {self.auth}")
        return "\n".join(lines)
```

```python
# app/notion/client.py
from __future__ import annotations

import logging

import httpx

from app.notion.schema import ApiSpecEntry

logger = logging.getLogger(__name__)

_NOTION_API_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


def _rich_text(prop: dict) -> str:
    parts = prop.get("rich_text", [])
    return "".join(p.get("plain_text", "") for p in parts)


def _select(prop: dict) -> str:
    select = prop.get("select")
    return select.get("name", "") if select else ""


def _parse_row(properties: dict) -> ApiSpecEntry:
    return ApiSpecEntry(
        method=_select(properties.get("Method", {})),
        path=_rich_text(properties.get("Path", {})),
        summary=_rich_text(properties.get("Summary", {})),
        request_schema=_rich_text(properties.get("Request Schema", {})),
        response_schema=_rich_text(properties.get("Response Schema", {})),
        auth=_rich_text(properties.get("Auth", {})),
    )


class NotionClient:
    """Notion 데이터베이스를 read-only로 조회하는 얇은 REST 클라이언트.

    sync_api_spec.py(오프라인 배치)에서만 쓰인다 — PR 리뷰 요청 처리 경로에서는
    절대 호출하지 않는다 (7.3절: PR 리뷰 시점에 Notion을 직접 조회하지 않는다).
    """

    def __init__(self, token: str, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._token = token
        self._http_client = http_client or httpx.AsyncClient(
            base_url=_NOTION_API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": _NOTION_VERSION,
            },
            timeout=30.0,
        )

    async def query_database(self, database_id: str) -> list[ApiSpecEntry]:
        entries: list[ApiSpecEntry] = []
        cursor: str | None = None
        try:
            while True:
                payload = {"start_cursor": cursor} if cursor else {}
                response = await self._http_client.post(
                    f"/databases/{database_id}/query", json=payload
                )
                response.raise_for_status()
                data = response.json()
                for row in data.get("results", []):
                    entries.append(_parse_row(row.get("properties", {})))
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
        except httpx.HTTPError:
            logger.warning("notion database query failed database_id=%s", database_id, exc_info=True)
            return []
        return entries
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_notion_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/notion/ tests/test_notion_client.py
git commit -m "feat :: Notion 데이터베이스 read-only 조회 클라이언트 추가"
```

---

### Task 5: API 명세 전용 Qdrant 벡터 스토어

**Files:**
- Create: `app/rag/api_spec_vector_store.py`
- Test: `tests/test_api_spec_vector_store.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_vector_store.py`의 구조를 그대로 참고해서 작성한다 (repository_id 스코핑, delete_by_repository 포함).

```python
from qdrant_client import QdrantClient

from app.notion.schema import ApiSpecEntry
from app.rag.api_spec_vector_store import ApiSpecSearchResult, ApiSpecVectorStore


def _store(dimension: int = 3) -> ApiSpecVectorStore:
    client = QdrantClient(location=":memory:")
    return ApiSpecVectorStore(client, "api_spec", vector_size=dimension)


def _entry(path: str = "/api/users") -> ApiSpecEntry:
    return ApiSpecEntry(method="GET", path=path, summary="유저 조회")


def test_upsert_and_search_roundtrip() -> None:
    store = _store()
    store.ensure_collection()

    store.upsert_entries(1, [_entry()], [[1.0, 0.0, 0.0]])
    results = store.search(1, [1.0, 0.0, 0.0], limit=5)

    assert len(results) == 1
    assert results[0].path == "/api/users"
    assert results[0].method == "GET"


def test_search_does_not_leak_across_repositories() -> None:
    store = _store()
    store.ensure_collection()
    store.upsert_entries(1, [_entry()], [[1.0, 0.0, 0.0]])
    store.upsert_entries(2, [_entry()], [[1.0, 0.0, 0.0]])

    assert len(store.search(1, [1.0, 0.0, 0.0], limit=10)) == 1
    assert len(store.search(2, [1.0, 0.0, 0.0], limit=10)) == 1


def test_delete_by_repository_removes_only_that_repository() -> None:
    store = _store()
    store.ensure_collection()
    store.upsert_entries(1, [_entry()], [[1.0, 0.0, 0.0]])
    store.upsert_entries(2, [_entry()], [[1.0, 0.0, 0.0]])

    store.delete_by_repository(1)

    assert store.search(1, [1.0, 0.0, 0.0], limit=10) == []
    assert len(store.search(2, [1.0, 0.0, 0.0], limit=10)) == 1
```

- [ ] **Step 2: 테스트 실행해 실패 확인**

Run: `uv run pytest tests/test_api_spec_vector_store.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 구현**

`app/rag/vector_store.py`의 `_point_id`/`ensure_collection`/payload-index 패턴을 그대로 재사용하되, 엔티티를 코드 chunk가 아니라 `ApiSpecEntry`로 바꾼다. point id는 `uuid5(repository_id + method + path)`로 결정론적으로 만든다 (재sync 시 같은 endpoint면 덮어쓴다).

```python
# app/rag/api_spec_vector_store.py
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client.http.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from app.notion.schema import ApiSpecEntry

logger = logging.getLogger(__name__)

_POINT_ID_NAMESPACE = uuid.UUID("9f1c1a3e-2b7d-4e6a-8c3f-1a2b3c4d5e6f")


def _point_id(repository_id: int, entry: ApiSpecEntry) -> str:
    key = f"{repository_id}:{entry.method}:{entry.path}"
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, key))


@dataclass
class ApiSpecSearchResult:
    method: str
    path: str
    summary: str
    request_schema: str
    response_schema: str
    auth: str
    score: float


class ApiSpecVectorStore:
    def __init__(self, client: QdrantClient, collection_name: str, vector_size: int) -> None:
        self._client = client
        self._collection_name = collection_name
        self._vector_size = vector_size

    def ensure_collection(self) -> None:
        if self._client.collection_exists(self._collection_name):
            return
        self._client.create_collection(
            collection_name=self._collection_name,
            vectors_config=VectorParams(size=self._vector_size, distance=Distance.COSINE),
        )
        self._client.create_payload_index(
            collection_name=self._collection_name,
            field_name="repositoryId",
            field_schema=PayloadSchemaType.INTEGER,
        )

    def upsert_entries(
        self, repository_id: int, entries: list[ApiSpecEntry], vectors: list[list[float]]
    ) -> None:
        if len(entries) != len(vectors):
            raise ValueError("entries와 vectors의 개수가 일치해야 한다")
        if not entries:
            return
        points = [
            PointStruct(
                id=_point_id(repository_id, entry),
                vector=vector,
                payload={
                    "repositoryId": repository_id,
                    "method": entry.method,
                    "path": entry.path,
                    "summary": entry.summary,
                    "requestSchema": entry.request_schema,
                    "responseSchema": entry.response_schema,
                    "auth": entry.auth,
                },
            )
            for entry, vector in zip(entries, vectors)
        ]
        self._client.upsert(collection_name=self._collection_name, points=points)

    def delete_by_repository(self, repository_id: int) -> None:
        self._client.delete(
            collection_name=self._collection_name,
            points_selector=Filter(
                must=[FieldCondition(key="repositoryId", match=MatchValue(value=repository_id))]
            ),
        )

    def search(
        self, repository_id: int, query_vector: list[float], *, limit: int = 5
    ) -> list[ApiSpecSearchResult]:
        response = self._client.query_points(
            collection_name=self._collection_name,
            query=query_vector,
            limit=limit,
            query_filter=Filter(
                must=[FieldCondition(key="repositoryId", match=MatchValue(value=repository_id))]
            ),
        )
        results = []
        for point in response.points:
            payload = point.payload
            if payload is None:
                continue
            results.append(
                ApiSpecSearchResult(
                    method=payload["method"],
                    path=payload["path"],
                    summary=payload["summary"],
                    request_schema=payload["requestSchema"],
                    response_schema=payload["responseSchema"],
                    auth=payload["auth"],
                    score=point.score,
                )
            )
        return results
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_api_spec_vector_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/rag/api_spec_vector_store.py tests/test_api_spec_vector_store.py
git commit -m "feat :: API 명세 전용 Qdrant 벡터 스토어 추가"
```

---

### Task 6: `sync_api_spec.py` CLI

**Files:**
- Create: `scripts/sync_api_spec.py`
- Test: `tests/test_sync_api_spec.py`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
from qdrant_client import QdrantClient

from app.notion.schema import ApiSpecEntry
from app.rag.api_spec_vector_store import ApiSpecVectorStore
from scripts.sync_api_spec import sync_all


class FakeEmbedder:
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t)), 0.0, 0.0] for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return [float(len(text)), 0.0, 0.0]

    @property
    def dimension(self) -> int:
        return 3


class FakeNotionClient:
    def __init__(self, entries_by_db: dict[str, list[ApiSpecEntry]]) -> None:
        self._entries_by_db = entries_by_db

    async def query_database(self, database_id: str) -> list[ApiSpecEntry]:
        return self._entries_by_db.get(database_id, [])


class FakeLinkStore:
    def __init__(self, links: list[tuple[int, str]]) -> None:
        self._links = links

    async def list_all(self) -> list[tuple[int, str]]:
        return self._links


async def test_sync_all_upserts_entries_for_every_known_repository() -> None:
    entries = [ApiSpecEntry(method="GET", path="/x", summary="s")]
    notion = FakeNotionClient({"db-1": entries})
    link_store = FakeLinkStore([(42, "db-1")])
    vector_store = ApiSpecVectorStore(QdrantClient(location=":memory:"), "api_spec", vector_size=3)

    total = await sync_all(
        link_store=link_store, notion_client=notion, embedder=FakeEmbedder(), vector_store=vector_store
    )

    assert total == 1
    assert len(vector_store.search(42, [1.0, 0.0, 0.0], limit=10)) == 1


async def test_sync_all_skips_repository_with_no_entries() -> None:
    notion = FakeNotionClient({})
    link_store = FakeLinkStore([(42, "empty-db")])
    vector_store = ApiSpecVectorStore(QdrantClient(location=":memory:"), "api_spec", vector_size=3)

    total = await sync_all(
        link_store=link_store, notion_client=notion, embedder=FakeEmbedder(), vector_store=vector_store
    )

    assert total == 0
```

- [ ] **Step 2: 테스트 실행해 실패 확인**

Run: `uv run pytest tests/test_sync_api_spec.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 구현**

```python
"""DOVI.md에서 발견된 Notion API 명세 DB를 주기적으로 Qdrant에 동기화하는 CLI.

review pipeline이 Redis(RedisNotionLinkStore)에 기록해둔 (repository_id, notion
database url) 목록을 읽어 Notion을 조회하고, 결과를 임베딩해 Qdrant에 저장한다.
PR 리뷰 요청 처리 경로에서는 이 스크립트를 호출하지 않는다 — 호스트 cron으로
주기 실행한다 (예: 매일 새벽 1회).

사용법:
    uv run python scripts/sync_api_spec.py
"""

import asyncio
import logging

from app.context.api_spec_link_store import NotionLinkStore, RedisNotionLinkStore
from app.core.config import get_settings
from app.notion.client import NotionClient
from app.rag.api_spec_vector_store import ApiSpecVectorStore
from app.rag.embeddings import CodeRankEmbedClient, Embedder
from app.review.dedup import RedisLike, create_redis_client  # 기존 redis client factory 재사용

logger = logging.getLogger(__name__)


async def sync_all(
    *,
    link_store: NotionLinkStore,
    notion_client: NotionClient,
    embedder: Embedder,
    vector_store: ApiSpecVectorStore,
) -> int:
    vector_store.ensure_collection()
    total = 0
    for repository_id, database_url in await link_store.list_all():
        database_id = database_url.rstrip("/").split("/")[-1]
        entries = await notion_client.query_database(database_id)
        vector_store.delete_by_repository(repository_id)
        if not entries:
            logger.info("no api spec entries repository_id=%s", repository_id)
            continue
        vectors = embedder.embed_documents([entry.to_text() for entry in entries])
        vector_store.upsert_entries(repository_id, entries, vectors)
        total += len(entries)
        logger.info("synced repository_id=%s entries=%d", repository_id, len(entries))
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()

    from qdrant_client import QdrantClient

    redis = create_redis_client(settings)
    link_store = RedisNotionLinkStore(redis)
    notion_client = NotionClient(token=settings.notion_api_token)
    embedder = CodeRankEmbedClient(settings.embedding_model)
    qdrant_client = QdrantClient(url=settings.qdrant_url)
    vector_store = ApiSpecVectorStore(
        qdrant_client, settings.api_spec_collection_name, vector_size=embedder.dimension
    )

    total = asyncio.run(
        sync_all(
            link_store=link_store,
            notion_client=notion_client,
            embedder=embedder,
            vector_store=vector_store,
        )
    )
    print(f"동기화 완료: {total}개 endpoint")


if __name__ == "__main__":
    main()
```

(`app/review/dedup.py`에 `create_redis_client`가 이미 있는지, 이름이 정확히 이건지 Task 착수 전에 확인 — 없으면 `app/kafka/client.py` 등 실제 factory 위치를 찾아 import 경로를 맞춘다.)

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_sync_api_spec.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/sync_api_spec.py tests/test_sync_api_spec.py
git commit -m "feat :: Notion API 명세 동기화 CLI 추가"
```

---

### Task 7: `ApiSpecRetriever` + 파이프라인 검색 연동

**Files:**
- Create: `app/context/api_spec_retriever.py`
- Modify: `app/review/pipeline.py`
- Test: `tests/test_api_spec_retriever.py`, `tests/test_review_pipeline.py`

- [ ] **Step 1: 실패하는 테스트 작성 (retriever 단위 테스트)**

`tests/test_retriever.py`의 `FakeEmbedder`/`SpyVectorStore` 패턴을 그대로 참고.

```python
from app.context.api_spec_retriever import ApiSpecRetriever
from app.rag.api_spec_vector_store import ApiSpecSearchResult


class FakeEmbedder:
    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


class SpyStore:
    def __init__(self, results: list[ApiSpecSearchResult]) -> None:
        self._results = results
        self.received_repository_id: int | None = None

    def search(self, repository_id: int, query_vector: list[float], *, limit: int = 5):
        self.received_repository_id = repository_id
        return self._results


def _result(path: str = "/api/x") -> ApiSpecSearchResult:
    return ApiSpecSearchResult(
        method="GET", path=path, summary="s", request_schema="", response_schema="", auth="", score=0.9
    )


def test_retrieve_returns_matching_entries() -> None:
    store = SpyStore([_result()])
    retriever = ApiSpecRetriever(FakeEmbedder(), store)  # type: ignore[arg-type]

    results = retriever.retrieve("query", 42)

    assert store.received_repository_id == 42
    assert len(results) == 1


def test_retrieve_swallows_errors_and_returns_empty() -> None:
    class BoomStore:
        def search(self, *args: object, **kwargs: object) -> list[object]:
            raise RuntimeError("qdrant down")

    retriever = ApiSpecRetriever(FakeEmbedder(), BoomStore())  # type: ignore[arg-type]

    assert retriever.retrieve("query", 42) == []
```

- [ ] **Step 2: 테스트 실행해 실패 확인**

Run: `uv run pytest tests/test_api_spec_retriever.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 구현 — retriever**

```python
# app/context/api_spec_retriever.py
from __future__ import annotations

import logging

from app.rag.api_spec_vector_store import ApiSpecSearchResult, ApiSpecVectorStore
from app.rag.embeddings import Embedder

logger = logging.getLogger(__name__)


class ApiSpecRetriever:
    """Notion에서 동기화된 API 명세를 검색한다.

    ProjectContextRetriever와 동일한 best-effort 원칙: 실패하면 예외를 삼키고
    빈 결과로 fallback한다 (API 명세 검색 실패가 리뷰 자체를 막으면 안 된다).
    """

    def __init__(
        self,
        embedder: Embedder,
        vector_store: ApiSpecVectorStore,
        *,
        limit: int = 3,
        min_score: float = 0.5,
    ) -> None:
        self._embedder = embedder
        self._vector_store = vector_store
        self._limit = limit
        self._min_score = min_score

    def retrieve(self, query_text: str, repository_id: int) -> list[ApiSpecSearchResult]:
        if not query_text.strip():
            return []
        try:
            query_vector = self._embedder.embed_query(query_text)
            results = self._vector_store.search(repository_id, query_vector, limit=self._limit)
        except Exception:
            logger.warning("api spec retrieval failed, continuing without it", exc_info=True)
            return []
        return [r for r in results if r.score >= self._min_score][: self._limit]
```

- [ ] **Step 4: retriever 테스트 통과 확인**

Run: `uv run pytest tests/test_api_spec_retriever.py -v`
Expected: PASS

- [ ] **Step 5: 파이프라인 연동 테스트 작성**

```python
class FakeApiSpecRetriever:
    def __init__(self, results: list[ApiSpecSearchResult]) -> None:
        self.results = results
        self.received: tuple[str, int] | None = None

    def retrieve(self, query_text: str, repository_id: int) -> list[ApiSpecSearchResult]:
        self.received = (query_text, repository_id)
        return self.results


async def test_run_includes_api_spec_when_no_swagger_present() -> None:
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=[]))
    api_spec_retriever = FakeApiSpecRetriever(
        [ApiSpecSearchResult(method="GET", path="/api/x", summary="s", request_schema="", response_schema="", auth="", score=0.9)]
    )
    event = _event()  # context_files에 openapi/swagger 없음

    pipeline = ReviewPipeline(
        fake, model_version="v", prompt_version="v1", api_spec_retriever=api_spec_retriever
    )
    await pipeline.run(event)

    user_message = fake.received[1]["content"]
    assert "관련 API 명세" in user_message
    assert "GET /api/x" in user_message


async def test_run_skips_api_spec_when_swagger_present() -> None:
    fake = FakeLLM(output=ReviewModelOutput(summary="ok", reviews=[]))
    api_spec_retriever = FakeApiSpecRetriever([_result()])
    event = _event()
    event.context_files = [ContextFile(path="openapi.yaml", content="...")]

    pipeline = ReviewPipeline(
        fake, model_version="v", prompt_version="v1", api_spec_retriever=api_spec_retriever
    )
    await pipeline.run(event)

    assert api_spec_retriever.received is None
    user_message = fake.received[1]["content"]
    assert "관련 API 명세" not in user_message
```

- [ ] **Step 6: 파이프라인 구현**

`ReviewPipeline.__init__`에 `api_spec_retriever: ApiSpecRetriever | None = None` 추가. `run()`의 실제 현재 코드(`app/review/pipeline.py:141-142`)는:

```python
        related_context = await self._retrieve_related_context(event.repository_id, targets)
        messages = self._build_messages(event, targets, related_context)
```

이걸 이렇게 바꾼다:

```python
        related_context = await self._retrieve_related_context(event.repository_id, targets)
        api_spec_context = await self._retrieve_api_spec_context(event, targets)
        messages = self._build_messages(event, targets, related_context, api_spec_context)
```

새 메서드 (diff 전체 hunk를 쿼리로 쓴다 — target별이 아니라 PR 전체 단위 검색이라 `_retrieve_related_context`처럼 target마다 반복하지 않는다):

```python
    async def _retrieve_api_spec_context(
        self, event: ReviewRequestedEvent, targets: list[ReviewTarget]
    ) -> str:
        if self._api_spec_retriever is None:
            return ""
        if has_openapi_spec(event.context_files):
            return ""
        query = "\n".join(hunk for t in targets for hunk in t.hunks)
        loop = asyncio.get_running_loop()
        call = functools.partial(self._api_spec_retriever.retrieve, query, event.repository_id)
        try:
            results = await loop.run_in_executor(None, call)
        except Exception:
            logger.warning("api spec context retrieval failed", exc_info=True)
            return ""
        if not results:
            return ""
        entries = "\n\n".join(f"{r.method} {r.path}\n{r.summary}" for r in results)
        return f"\n\n#### 관련 API 명세\n{entries}"
```

`_build_messages`(`app/review/pipeline.py:330-343`)는 target별이 아니라 PR 전체에 한 번 붙는 섹션이므로 `_render_target`은 건드리지 않고, `_build_messages`에만 매개변수를 추가한다:

```python
    def _build_messages(
        self,
        event: ReviewRequestedEvent,
        targets: list[ReviewTarget],
        related_context: dict[str, list[ChunkSearchResult]],
        api_spec_context: str = "",
    ) -> list[ChatMessage]:
        diff = "\n\n".join(
            self._render_target(t, related_context.get(t.file_path, [])) for t in targets
        )
        context = build_context(event.context_files)
        user = f"## Project Context\n{context}\n\n## Changes\n{diff}" if context else diff
        user += api_spec_context
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
```

- [ ] **Step 7: 전체 테스트 통과 확인**

Run: `uv run pytest -q`
Expected: 전부 PASS

- [ ] **Step 8: Commit**

```bash
git add app/context/api_spec_retriever.py app/review/pipeline.py tests/test_api_spec_retriever.py tests/test_review_pipeline.py
git commit -m "feat :: swagger 없을 때 Notion 기반 API 명세 검색 결과를 리뷰 컨텍스트에 연결"
```

---

### Task 8: Settings + main.py 배선 + .env.example

**Files:**
- Modify: `app/core/config.py`, `app/main.py`, `.env.example`

- [ ] **Step 1: Settings 추가**

`app/core/config.py`:
```python
    notion_sync_enabled: bool = False
    notion_api_token: str = ""
    api_spec_collection_name: str = "dovi_api_spec_chunks"
```

- [ ] **Step 2: `.env.example` 추가**

```
NOTION_SYNC_ENABLED=false
NOTION_API_TOKEN=
API_SPEC_COLLECTION_NAME=dovi_api_spec_chunks
```

- [ ] **Step 3: `app/main.py` 배선**

`if settings.rag_enabled:` 블록 안(RAG 인프라가 이미 있어야 API 명세도 의미 있으므로 같은 조건 재사용)에서, `notion_sync_enabled`가 true일 때만 `ApiSpecRetriever` 구성:

```python
        api_spec_retriever = None
        if settings.notion_sync_enabled:
            from app.context.api_spec_retriever import ApiSpecRetriever
            from app.rag.api_spec_vector_store import ApiSpecVectorStore

            api_spec_vector_store = ApiSpecVectorStore(
                qdrant_client, settings.api_spec_collection_name, vector_size=embedder.dimension
            )
            api_spec_retriever = ApiSpecRetriever(embedder, api_spec_vector_store)

        notion_link_store = None
        if settings.notion_sync_enabled:
            from app.context.api_spec_link_store import RedisNotionLinkStore

            notion_link_store = RedisNotionLinkStore(redis_client)
```

`ReviewPipeline(...)` 생성 호출에 `api_spec_retriever=api_spec_retriever, notion_link_store=notion_link_store` 추가.

> `redis_client`가 이 시점(RAG 배선 블록)에 이미 생성돼 있는지 확인 — 현재 `main.py`에서 `redis_client`는 더 아래(`create_redis_client(settings)`)에서 만들어진다. 순서를 조정하거나, `notion_link_store` 배선을 redis_client 생성 이후로 옮긴다.

- [ ] **Step 4: 검증**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy .`
Expected: 전부 통과. `uv run python -c "import app.main; import sys; print('qdrant_client' in sys.modules, 'httpx' in sys.modules)"` — `httpx`는 이미 다른 곳(LLM client)에서도 쓰이므로 `True`가 나와도 문제 없음. 중요한 건 `qdrant_client`가 여전히 `False`인 것 (기존 인시던트 재발 방지 확인).

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py app/main.py .env.example
git commit -m "feat :: Notion API 명세 sync 기능 플래그 및 배선 추가"
```

---

## Self-Review 체크리스트

- [x] `ReviewRequestedEvent.context_files: list[ContextFile] = []` — 필드명 확인 완료 (`app/review/schema.py:35`), 이 문서 전체에서 정확히 일치시킴
- [x] `_build_messages`/`_render_target` 실제 시그니처 확인 완료 (`app/review/pipeline.py:330-357`) — Task 7을 실제 코드에 맞춰 갱신함
- [x] `create_redis_client`는 `app/review/dedup.py:89`에 있음 — Task 6/8의 import 경로 확정
- [ ] Notion integration token 발급 및 팀 관리자가 실제 API 명세 데이터베이스를 그 integration에 공유하는 절차는 이 스펙 범위 밖(운영 setup) — README나 별도 운영 문서에 안내만 남긴다
