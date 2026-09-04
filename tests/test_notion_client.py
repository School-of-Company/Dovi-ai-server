from typing import Any

import httpx

from app.notion.client import NotionClient
from app.notion.schema import ApiSpecEntry


def _transport(response_json: dict[str, Any]) -> httpx.MockTransport:
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
        token="fake-token",
        http_client=httpx.AsyncClient(
            base_url="https://api.notion.com/v1", transport=_transport(response_json)
        ),
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
        token="fake-token",
        http_client=httpx.AsyncClient(
            base_url="https://api.notion.com/v1", transport=httpx.MockTransport(handler)
        ),
    )

    assert await client.query_database("missing-db") == []
