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
    # 헤딩 라인은 [^\n]*로 한 줄만 매칭한다 — re.DOTALL 하에서 `.*$\n`을 쓰면
    # `.`이 개행까지 흡수해 greedy 백트래킹이 문서 끝에서부터 첫 `$\n` 경계를
    # 찾아버려(즉 헤딩 바로 다음이 아니라 문서 맨 뒤 근처) 섹션 본문이 통째로
    # 사라지는 문제가 있다.
    return re.compile(
        rf"^##\s+\[?{escaped}\]?[^\n]*\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL
    )


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
