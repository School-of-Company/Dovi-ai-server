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
