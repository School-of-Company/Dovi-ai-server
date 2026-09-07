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
            message = await self._registry_client.get_deprecation_message(name, version)
        except Exception:
            logger.warning(
                "npm registry lookup failed name=%s version=%s", name, version, exc_info=True
            )
            return None

        try:
            await self._cache.set(
                name, version, CachedResult(deprecated=message is not None, message=message)
            )
        except Exception:
            logger.warning(
                "npm deprecation cache write failed name=%s version=%s",
                name,
                version,
                exc_info=True,
            )

        return message
