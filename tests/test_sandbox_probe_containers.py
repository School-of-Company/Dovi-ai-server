import asyncio
import json
import threading
import urllib.request
from collections.abc import Iterator

import pytest

from app.sandbox_probe.containers import mock_server
from app.sandbox_probe.containers.egress_proxy import handle_client


@pytest.fixture
def mock_url() -> Iterator[str]:
    mock_server.RECEIVED.clear()
    server = mock_server.create_server(0, host="127.0.0.1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _request(url: str, method: str = "GET", data: bytes | None = None) -> bytes:
    request = urllib.request.Request(url, data=data, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        body: bytes = response.read()
    return body


def test_mock_server_records_any_call_and_exposes_them(mock_url: str) -> None:
    _request(f"{mock_url}/webhook?wait=true", "POST", b'{"content":"started"}')
    _request(f"{mock_url}/other", "PUT", b"x")

    received = json.loads(_request(f"{mock_url}/_received"))

    assert [(r["method"], r["path"]) for r in received] == [
        ("POST", "/webhook?wait=true"),
        ("PUT", "/other"),
    ]
    assert received[0]["body"] == '{"content":"started"}'


def test_mock_server_does_not_record_the_inspection_call_itself(mock_url: str) -> None:
    _request(f"{mock_url}/_received")

    assert json.loads(_request(f"{mock_url}/_received")) == []


async def _proxy_response(request: bytes, allowed: set[str]) -> bytes:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_client(reader, writer, allowed)

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(1024), 5)
        writer.close()
    return response


async def test_egress_proxy_rejects_hosts_outside_the_allowlist() -> None:
    response = await _proxy_response(
        b"CONNECT evil.example.com:443 HTTP/1.1\r\nHost: evil.example.com:443\r\n\r\n",
        {"registry.npmjs.org"},
    )

    assert response.startswith(b"HTTP/1.1 403")


async def test_egress_proxy_rejects_allowed_host_on_other_ports() -> None:
    response = await _proxy_response(
        b"CONNECT registry.npmjs.org:22 HTTP/1.1\r\n\r\n", {"registry.npmjs.org"}
    )

    assert response.startswith(b"HTTP/1.1 403")


async def test_egress_proxy_rejects_plain_http_requests() -> None:
    response = await _proxy_response(
        b"GET http://registry.npmjs.org/ HTTP/1.1\r\n\r\n", {"registry.npmjs.org"}
    )

    assert response.startswith(b"HTTP/1.1 405")
