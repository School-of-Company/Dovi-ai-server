from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_REGISTRY_BASE_URL = "https://registry.npmjs.org"
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


class NpmRegistryClient:
    """npm registry에서 특정 (패키지명, 버전)의 deprecated 여부를 조회한다.

    best-effort: 네트워크 실패/타임아웃/404 등 어떤 이유로든 조회에 실패하면
    예외를 던지지 않고 `DeprecationLookupResult(ok=False, message=None)`을
    반환한다 — 실패와 "확인 결과 deprecated 아님"을 호출자가 구분할 수 있도록
    분리된 결과 타입을 쓴다(실패를 성공으로 오인해 캐시에 남기지 않기 위함).
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

    async def check_deprecation(self, name: str, version: str) -> DeprecationLookupResult:
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
            return DeprecationLookupResult(ok=False, message=None)

        if response.status_code != 200:
            return DeprecationLookupResult(ok=False, message=None)

        try:
            data = response.json()
        except ValueError:
            return DeprecationLookupResult(ok=False, message=None)

        deprecated = data.get("deprecated") if isinstance(data, dict) else None
        message = deprecated if isinstance(deprecated, str) else None
        github_repo = (
            _extract_github_repo(data.get("repository")) if isinstance(data, dict) else None
        )
        return DeprecationLookupResult(ok=True, message=message, github_repo=github_repo)

    async def aclose(self) -> None:
        await self._client.aclose()
