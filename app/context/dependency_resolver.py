from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import Protocol

from app.context.gradle_dependency_diff import (
    extract_dependency_changes as extract_gradle_dependency_changes,
)
from app.context.maven_central_client import RelocationLookupResult
from app.context.npm_deprecation_cache import CachedResult
from app.context.npm_lockfile_diff import DependencyChange, extract_dependency_changes
from app.context.npm_registry_client import DeprecationLookupResult
from app.review.schema import ChangedFile, ReviewComment

logger = logging.getLogger(__name__)

_NPM_LOCKFILE_NAME = "package-lock.json"
_GRADLE_BUILD_FILE_NAMES = {"build.gradle", "build.gradle.kts"}

# 한 파일당 확인할 distinct (name, version) 쌍의 상한. PR 리뷰는 Kafka consumer가
# 순차 처리하므로, 큰 npm update lockfile 하나가 수백 개의 registry 호출을
# 유발하면 큐 전체가 오래 막힐 수 있다 — 이를 방지하기 위한 상한이다.
_MAX_DEPENDENCY_CHECKS_PER_FILE = 50

# npm registry의 `deprecated` 필드는 패키지 게시자가 자유롭게 쓰는 텍스트라
# Dovi의 PR 코멘트에 그대로 노출하면 안 된다(줄바꿈/마크다운 주입, 과도한 길이).
_MAX_MESSAGE_LENGTH = 200


def _sanitize_registry_message(message: str) -> str:
    collapsed = " ".join(message.split())  # 모든 공백/줄바꿈을 단일 스페이스로 뭉갠다
    collapsed = collapsed.replace("`", "")
    if len(collapsed) > _MAX_MESSAGE_LENGTH:
        collapsed = collapsed[:_MAX_MESSAGE_LENGTH].rstrip() + "..."
    return collapsed


class RegistryClient(Protocol):
    async def check_deprecation(self, name: str, version: str) -> DeprecationLookupResult: ...


class DeprecationCache(Protocol):
    async def get(self, name: str, version: str) -> CachedResult | None: ...
    async def set(self, name: str, version: str, result: CachedResult) -> None: ...


class RelocationClient(Protocol):
    async def check_relocation(self, name: str, version: str) -> RelocationLookupResult: ...


class DependencyResolver:
    """package-lock.json / build.gradle(.kts) 변경분에서 deprecated 또는
    relocation된 의존성을 찾아 ReviewComment로 만든다. LLM을 거치지 않는
    결정론적 판단이다 — registry/POM이 직접 알려주는 사실이라 재해석의
    여지가 없다.

    best-effort: 개별 패키지 조회가 실패해도 나머지는 계속 진행하고, 전체가
    실패해도 예외를 던지지 않고 빈 리스트를 반환한다(리뷰 자체를 막지 않는다).

    maven_client/maven_cache를 생략하면 Gradle build 파일은 그냥 무시된다
    (npm 전용으로 계속 동작).
    """

    def __init__(
        self,
        registry_client: RegistryClient,
        cache: DeprecationCache,
        *,
        maven_client: RelocationClient | None = None,
        maven_cache: DeprecationCache | None = None,
    ) -> None:
        self._registry_client = registry_client
        self._cache = cache
        self._maven_client = maven_client
        self._maven_cache = maven_cache

    async def find_deprecated_dependencies(
        self, changed_files: list[ChangedFile]
    ) -> list[ReviewComment]:
        findings: list[ReviewComment] = []
        for file in changed_files:
            file_name = PurePosixPath(file.file_path).name
            if file_name == _NPM_LOCKFILE_NAME:
                findings.extend(await self._check_npm_lockfile(file))
            elif file_name in _GRADLE_BUILD_FILE_NAMES and self._maven_client is not None:
                findings.extend(await self._check_gradle_build_file(file))
        return findings

    def _dedupe_and_cap(
        self, changes: list[DependencyChange], file_path: str
    ) -> list[DependencyChange]:
        # 같은 (name, version) 쌍이 한 patch 안에 여러 번 나올 수 있다(hoisted
        # top-level 복사본 + nested transitive 복사본이 같은 버전으로 동시에 bump되는
        # 경우 등) — 중복 조회/중복 summary bullet을 막기 위해 처음 등장한 것만 남긴다.
        deduped: list[DependencyChange] = []
        seen: set[tuple[str, str]] = set()
        for change in changes:
            key = (change.name, change.version)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(change)

        if len(deduped) > _MAX_DEPENDENCY_CHECKS_PER_FILE:
            logger.warning(
                "dependency check capped at %d for %s, skipping %d remaining",
                _MAX_DEPENDENCY_CHECKS_PER_FILE,
                file_path,
                len(deduped) - _MAX_DEPENDENCY_CHECKS_PER_FILE,
            )
            deduped = deduped[:_MAX_DEPENDENCY_CHECKS_PER_FILE]
        return deduped

    async def _check_npm_lockfile(self, file: ChangedFile) -> list[ReviewComment]:
        try:
            changes = extract_dependency_changes(file.patch)
        except Exception:
            logger.warning("failed to parse lockfile patch path=%s", file.file_path, exc_info=True)
            return []

        deduped = self._dedupe_and_cap(changes, file.file_path)

        findings: list[ReviewComment] = []
        for change in deduped:
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
                    message=f"npm registry: '{_sanitize_registry_message(message)}'",
                    evidence=[change.evidence_line],
                    suggested_fix=None,
                )
            )
        return findings

    async def _check_gradle_build_file(self, file: ChangedFile) -> list[ReviewComment]:
        try:
            changes = extract_gradle_dependency_changes(file.patch)
        except Exception:
            logger.warning(
                "failed to parse gradle build file patch path=%s", file.file_path, exc_info=True
            )
            return []

        deduped = self._dedupe_and_cap(changes, file.file_path)

        findings: list[ReviewComment] = []
        for change in deduped:
            relocated_to = await self._get_relocation_message(change.name, change.version)
            if relocated_to is None:
                continue
            findings.append(
                ReviewComment(
                    severity="minor",
                    confidence=1.0,
                    file_path=file.file_path,
                    line=change.new_file_line,
                    title=f"Maven 아티팩트 이전(relocation)됨: {change.name}:{change.version}",
                    message=(
                        f"Maven Central: '{change.name}'가 "
                        f"'{_sanitize_registry_message(relocated_to)}'로 이전됨"
                    ),
                    evidence=[change.evidence_line],
                    suggested_fix=f"의존성 좌표를 '{relocated_to}'로 갱신하는 것을 검토하세요.",
                )
            )
        return findings

    async def _get_relocation_message(self, name: str, version: str) -> str | None:
        assert self._maven_client is not None
        assert self._maven_cache is not None

        try:
            cached = await self._maven_cache.get(name, version)
        except Exception:
            logger.warning(
                "maven relocation cache read failed name=%s version=%s",
                name,
                version,
                exc_info=True,
            )
            cached = None

        if cached is not None:
            return cached.message if cached.deprecated else None

        try:
            result = await self._maven_client.check_relocation(name, version)
        except Exception:
            logger.warning(
                "maven central lookup failed name=%s version=%s", name, version, exc_info=True
            )
            return None

        if not result.ok:
            return None

        try:
            await self._maven_cache.set(
                name,
                version,
                CachedResult(
                    deprecated=result.relocated_to is not None, message=result.relocated_to
                ),
            )
        except Exception:
            logger.warning(
                "maven relocation cache write failed name=%s version=%s",
                name,
                version,
                exc_info=True,
            )

        return result.relocated_to

    async def _get_deprecation_message(self, name: str, version: str) -> str | None:
        try:
            cached = await self._cache.get(name, version)
        except Exception:
            logger.warning(
                "npm deprecation cache read failed name=%s version=%s",
                name,
                version,
                exc_info=True,
            )
            cached = None

        if cached is not None:
            return cached.message if cached.deprecated else None

        try:
            result = await self._registry_client.check_deprecation(name, version)
        except Exception:
            logger.warning(
                "npm registry lookup failed name=%s version=%s", name, version, exc_info=True
            )
            return None

        if not result.ok:
            # 일시적 조회 실패 — 캐시에 남기지 않는다(다음 PR에서 다시 시도하도록).
            return None

        try:
            await self._cache.set(
                name,
                version,
                CachedResult(deprecated=result.message is not None, message=result.message),
            )
        except Exception:
            logger.warning(
                "npm deprecation cache write failed name=%s version=%s",
                name,
                version,
                exc_info=True,
            )

        return result.message
