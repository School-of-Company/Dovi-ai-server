from collections.abc import Callable

import httpx

from app.context.npm_registry_client import DeprecationLookupResult, NpmRegistryClient


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> NpmRegistryClient:
    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(
        transport=transport, base_url="https://registry.npmjs.org"
    )
    return NpmRegistryClient(client=async_client)


async def test_returns_deprecation_message_when_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/axios/1.20.0"
        return httpx.Response(200, json={"deprecated": "use fetch instead"})

    client = _client(handler)
    result = await client.check_deprecation("axios", "1.20.0")
    assert result == DeprecationLookupResult(ok=True, message="use fetch instead")


async def test_returns_none_when_not_deprecated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "axios", "version": "1.20.0"})

    client = _client(handler)
    result = await client.check_deprecation("axios", "1.20.0")
    assert result == DeprecationLookupResult(ok=True, message=None)


async def test_encodes_scoped_package_name_correctly() -> None:
    captured_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # Use raw_path which preserves encoding, decode to string
        captured_paths.append(request.url.raw_path.decode())
        return httpx.Response(200, json={})

    client = _client(handler)
    await client.check_deprecation("@tanstack/query-core", "5.102.8")

    # @는 그대로, /만 %2F로 인코딩되어야 한다 (safe="" 였다면 %40tanstack...이 되어
    # registry가 항상 404를 반환했을 것 — 실제로 겪은 버그).
    assert captured_paths == ["/@tanstack%2Fquery-core/5.102.8"]


async def test_returns_not_ok_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = _client(handler)
    result = await client.check_deprecation("nonexistent-package", "1.0.0")
    assert result == DeprecationLookupResult(ok=False, message=None)


async def test_returns_not_ok_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    client = _client(handler)
    result = await client.check_deprecation("axios", "1.20.0")
    assert result == DeprecationLookupResult(ok=False, message=None)


async def test_returns_not_ok_on_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    result = await client.check_deprecation("axios", "1.20.0")
    assert result == DeprecationLookupResult(ok=False, message=None)


async def test_returns_not_ok_on_malformed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"not valid json", headers={"content-type": "application/json"}
        )

    client = _client(handler)
    result = await client.check_deprecation("axios", "1.20.0")
    assert result == DeprecationLookupResult(ok=False, message=None)


async def test_returns_ok_when_response_body_is_not_a_dict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["unexpected", "list", "body"])

    client = _client(handler)
    result = await client.check_deprecation("axios", "1.20.0")
    assert result == DeprecationLookupResult(ok=True, message=None)


async def test_extracts_github_repo_from_git_plus_https_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "repository": {"type": "git", "url": "git+https://github.com/axios/axios.git"}
            },
        )

    client = _client(handler)
    result = await client.check_deprecation("axios", "1.20.0")
    assert result.github_repo == "axios/axios"


async def test_extracts_github_repo_from_git_protocol_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"repository": {"url": "git://github.com/axios/axios.git"}})

    client = _client(handler)
    result = await client.check_deprecation("axios", "1.20.0")
    assert result.github_repo == "axios/axios"


async def test_extracts_github_repo_from_plain_https_url_string() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"repository": "https://github.com/axios/axios"})

    client = _client(handler)
    result = await client.check_deprecation("axios", "1.20.0")
    assert result.github_repo == "axios/axios"


async def test_github_repo_is_none_when_repository_field_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "axios"})

    client = _client(handler)
    result = await client.check_deprecation("axios", "1.20.0")
    assert result.github_repo is None


async def test_github_repo_is_none_when_repository_is_not_github() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"repository": {"url": "https://gitlab.com/foo/bar.git"}})

    client = _client(handler)
    result = await client.check_deprecation("axios", "1.20.0")
    assert result.github_repo is None
