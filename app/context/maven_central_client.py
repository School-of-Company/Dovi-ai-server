from __future__ import annotations

import logging
from dataclasses import dataclass
from xml.etree import ElementTree

import httpx

logger = logging.getLogger(__name__)

_REPO_BASE_URL = "https://repo1.maven.org/maven2"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == name:
            return child.text
    return None


def _find_relocation(root: ElementTree.Element) -> ElementTree.Element | None:
    for dist_mgmt in root:
        if _local_name(dist_mgmt.tag) != "distributionManagement":
            continue
        for child in dist_mgmt:
            if _local_name(child.tag) == "relocation":
                return child
    return None


@dataclass
class RelocationLookupResult:
    """POM relocation 조회의 성공/실패와 결과를 분리해서 표현한다 (npm
    DeprecationLookupResult와 동일한 이유 — 조회 실패를 "relocation 없음"으로
    잘못 캐싱하지 않기 위함).

    `ok=False`는 네트워크/타임아웃/404/malformed XML 등 조회 자체가 실패했다는
    뜻이고, `ok=True`인데 `relocated_to=None`은 조회는 성공했지만 relocation이
    없다는 뜻이다.
    """

    ok: bool
    relocated_to: str | None  # "newGroupId:newArtifactId" 형태


class MavenCentralClient:
    """Maven Central에서 (groupId:artifactId, version)의 POM을 가져와
    <distributionManagement><relocation> 여부를 확인한다.

    Maven Central에는 npm registry의 `deprecated` 필드 같은 명시적 플래그가
    없다 — relocation은 아티팩트 좌표가 실제로 이전됐을 때만 POM에 기록되는
    표준 마커라 이 신호로 대체한다. npm deprecated보다 훨씬 드물게 걸린다.

    best-effort: 네트워크 실패/타임아웃/404/malformed XML 등 어떤 이유로든
    조회에 실패하면 예외를 던지지 않고 `RelocationLookupResult(ok=False, ...)`을
    반환한다.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=_REPO_BASE_URL, timeout=timeout_seconds
        )

    async def check_relocation(self, name: str, version: str) -> RelocationLookupResult:
        if ":" not in name:
            return RelocationLookupResult(ok=False, relocated_to=None)
        group_id, artifact_id = name.split(":", 1)
        group_path = group_id.replace(".", "/")
        path = f"/{group_path}/{artifact_id}/{version}/{artifact_id}-{version}.pom"
        try:
            response = await self._client.get(path)
        except httpx.HTTPError:
            logger.warning(
                "maven central lookup failed name=%s version=%s", name, version, exc_info=True
            )
            return RelocationLookupResult(ok=False, relocated_to=None)

        if response.status_code != 200:
            return RelocationLookupResult(ok=False, relocated_to=None)

        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError:
            return RelocationLookupResult(ok=False, relocated_to=None)

        relocation = _find_relocation(root)
        if relocation is None:
            return RelocationLookupResult(ok=True, relocated_to=None)

        new_group = _child_text(relocation, "groupId") or group_id
        new_artifact = _child_text(relocation, "artifactId") or artifact_id
        return RelocationLookupResult(ok=True, relocated_to=f"{new_group}:{new_artifact}")

    async def aclose(self) -> None:
        await self._client.aclose()
