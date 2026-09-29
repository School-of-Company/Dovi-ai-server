"""조건 A/B/C 실행 시 관측용 계측(prompt 노출 여부, 지연, 캐시 히트)을 위한
얇은 래퍼들. 부작용은 감싼 대상(inner)에 위임할 뿐, 여기 자체는 파이프라인
동작을 바꾸지 않는다.
"""

from __future__ import annotations

import time
from pathlib import PurePosixPath

from app.context.npm_lockfile_diff import extract_dependency_changes
from app.context.official_docs_workflow import ReleaseNotesCache
from app.context.release_notes_cache import CachedReleaseNotes
from app.llm.client import ChatMessage
from app.review.pipeline import OfficialDocsContextBuilder, ReviewLLM
from app.review.schema import (
    ChangedFile,
    ReviewModelOutput,
    ReviewRequestedEvent,
    VerificationResult,
)

_LOCKFILE_NAME = "package-lock.json"

# app/context/official_docs_workflow.py의 _HEADER(private)와 동기화가 필요하다:
# 실제 값은 "\n\n#### 의존성 버전 변경 근거 (공식 릴리즈 노트)" — 여기서는 그
# 부분 문자열만 복제해 "근거가 실제로 프롬프트에 들어갔는가"를 판정한다.
_EVIDENCE_HEADER_MARKER = "의존성 버전 변경 근거"


def strip_to_diff_only(event: ReviewRequestedEvent) -> ReviewRequestedEvent:
    """조건 A(순수 diff)용으로 context_files와 changed_files의 전체 파일 내용을
    제거한 새 이벤트를 만든다.

    원본 event와 그 changed_files 리스트/객체는 절대 변형하지 않는다 — 항상
    새 리스트/새 이벤트를 만들어 반환한다. pr_title/pr_body는 그대로 남긴다
    (계획서에서 diff + PR 제목/본문까지는 조건 A에 포함하기로 확정됨).
    """
    stripped_files = [
        changed_file.model_copy(update={"content": None, "previous_content": None})
        for changed_file in event.changed_files
    ]
    return event.model_copy(
        update={"context_files": [], "changed_files": stripped_files}
    )


class InMemoryRedis:
    """RedisLike 프로토콜(문자열 get/set)을 흉내내는 dict 기반 fake.

    평가 하네스는 영속 Redis 없이도 조건 C의 릴리즈 노트 캐시를 실행 1회
    범위 안에서(같은 실행 안의 여러 케이스가 같은 패키지를 공유할 때) 동작하게
    하려고 이 fake를 쓴다.
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, name: str) -> str | None:
        return self._store.get(name)

    async def set(self, name: str, value: str, ex: int | None = None) -> None:
        self._store[name] = value


class CountingReleaseNotesCache:
    """ReleaseNotesCache를 감싸 hit/miss 횟수를 센다."""

    def __init__(self, inner: ReleaseNotesCache) -> None:
        self._inner = inner
        self.hits = 0
        self.misses = 0

    async def get(self, owner_repo: str, version: str) -> CachedReleaseNotes | None:
        result = await self._inner.get(owner_repo, version)
        if result is not None:
            self.hits += 1
        else:
            self.misses += 1
        return result

    async def set(self, owner_repo: str, version: str, notes: str | None) -> None:
        await self._inner.set(owner_repo, version, notes)


class TimedOfficialDocsWorkflow:
    """OfficialDocsContextBuilder를 감싸 build_evidence() 호출 지연을 잰다.

    last_latency_ms는 가장 최근 호출 결과로 매번 덮어쓴다 — run()당 최대 1회만
    build_evidence()를 호출하는 파이프라인 동작과 맞물려, 케이스 하나당 지연
    1개만 기록하면 충분하다.
    """

    def __init__(self, inner: OfficialDocsContextBuilder) -> None:
        self._inner = inner
        self.last_latency_ms: float | None = None

    async def build_evidence(self, changed_files: list[ChangedFile]) -> str:
        start = time.perf_counter()
        result = await self._inner.build_evidence(changed_files)
        self.last_latency_ms = (time.perf_counter() - start) * 1000
        return result


class PromptCapturingLLM:
    """ReviewLLM을 감싸 generate()에 실제로 전달된 messages에 릴리즈 노트 근거
    헤더가 포함됐는지 관측한다. 그 외에는 전부 inner에 그대로 위임한다.
    """

    def __init__(self, inner: ReviewLLM) -> None:
        self._inner = inner
        self.evidence_seen = False

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1500,
        max_reviews: int | None = None,
    ) -> ReviewModelOutput:
        if any(_EVIDENCE_HEADER_MARKER in message.get("content", "") for message in messages):
            self.evidence_seen = True
        return await self._inner.generate(
            messages, max_tokens=max_tokens, max_reviews=max_reviews
        )

    async def verify_findings(
        self, messages: list[ChatMessage], *, max_tokens: int = 800
    ) -> VerificationResult:
        return await self._inner.verify_findings(messages, max_tokens=max_tokens)

    async def count_tokens(self, text: str) -> int:
        return await self._inner.count_tokens(text)

    async def get_context_window(self) -> int | None:
        return await self._inner.get_context_window()


def changed_npm_packages(event: ReviewRequestedEvent) -> list[str]:
    """event.changed_files 중 package-lock.json patch에서 변경된 패키지명을
    등장 순서를 유지한 채 중복 제거해 반환한다.
    """
    seen: set[str] = set()
    names: list[str] = []
    for changed_file in event.changed_files:
        if PurePosixPath(changed_file.file_path).name != _LOCKFILE_NAME:
            continue
        for change in extract_dependency_changes(changed_file.patch):
            if change.name in seen:
                continue
            seen.add(change.name)
            names.append(change.name)
    return names
