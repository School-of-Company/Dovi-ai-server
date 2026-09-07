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
