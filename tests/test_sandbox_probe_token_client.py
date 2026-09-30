import httpx
import pytest

from app.sandbox_probe.token_client import GithubAppTokenClient, TokenFetchError


def _client(handler: httpx.MockTransport) -> GithubAppTokenClient:
    return GithubAppTokenClient(
        "http://github-app.internal/",
        "s3cret",
        client=httpx.AsyncClient(transport=handler),
    )


async def test_fetch_token_posts_scope_and_shared_secret() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["secret"] = request.headers["X-Dovi-Internal-Secret"]
        seen["body"] = request.content
        return httpx.Response(200, json={"token": "ghs_abc", "expiresAt": "2026-01-01T00:00:00Z"})

    token = await _client(httpx.MockTransport(handler)).fetch_token(99, 42)

    assert token == "ghs_abc"
    assert seen["url"] == "http://github-app.internal/internal/sandbox-probe/token"
    assert seen["secret"] == "s3cret"
    assert seen["body"] == b'{"installationId":99,"repositoryId":42}'


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"message": "nope ghs_leak"}),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, json={"token": ""}),
        httpx.Response(200, text="not json"),
    ],
)
async def test_fetch_token_raises_without_leaking_response_body(
    response: httpx.Response,
) -> None:
    client = _client(httpx.MockTransport(lambda request: response))

    with pytest.raises(TokenFetchError) as info:
        await client.fetch_token(1, 2)

    assert "ghs_leak" not in str(info.value)


async def test_fetch_token_wraps_network_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(TokenFetchError):
        await _client(httpx.MockTransport(handler)).fetch_token(1, 2)
