from __future__ import annotations

import logging
from typing import Any

import httpx

from app.notion.schema import ApiSpecEntry

logger = logging.getLogger(__name__)

_NOTION_API_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


def _rich_text(prop: dict[str, Any]) -> str:
    parts = prop.get("rich_text", [])
    return "".join(p.get("plain_text", "") for p in parts)


def _select(prop: dict[str, Any]) -> str:
    select = prop.get("select")
    return select.get("name", "") if select else ""


def _parse_row(properties: dict[str, Any]) -> ApiSpecEntry:
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
            logger.warning(
                "notion database query failed database_id=%s", database_id, exc_info=True
            )
            return []
        return entries
