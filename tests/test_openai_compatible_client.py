import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import ValidationError

from app.llm.openai_compatible_client import OpenAICompatibleLLMClient
from app.review.schema import ReviewModelOutput

_VALID_CONTENT = json.dumps({"summary": "ok", "reviews": []})


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> OpenAICompatibleLLMClient:
    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(base_url="http://localhost:8001/v1", transport=transport)
    return OpenAICompatibleLLMClient(
        base_url="http://localhost:8001/v1", model="test-model", client=async_client
    )


def _openai_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"completion_tokens": 10},
        },
    )


async def test_generate_returns_parsed_output() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _openai_response(_VALID_CONTENT)

    client = _client(handler)
    result = await client.generate([{"role": "user", "content": "hi"}])

    assert isinstance(result, ReviewModelOutput)
    assert result.summary == "ok"


async def test_generate_sends_json_schema_response_format() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _openai_response(_VALID_CONTENT)

    client = _client(handler)
    await client.generate([{"role": "user", "content": "hi"}], max_tokens=500)

    body = captured["body"]
    assert body["model"] == "test-model"
    assert body["max_tokens"] == 500
    assert body["response_format"]["type"] == "json_schema"
    schema = body["response_format"]["json_schema"]["schema"]
    assert "reviews" in schema["properties"]


async def test_generate_timeout_raises_builtin_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(handler)

    with pytest.raises(TimeoutError):
        await client.generate([{"role": "user", "content": "hi"}])


async def test_generate_http_error_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    client = _client(handler)

    with pytest.raises(httpx.HTTPStatusError):
        await client.generate([{"role": "user", "content": "hi"}])


async def test_generate_malformed_response_raises_value_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    client = _client(handler)

    with pytest.raises(ValueError):
        await client.generate([{"role": "user", "content": "hi"}])


async def test_generate_invalid_json_content_raises_value_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _openai_response("not json")

    client = _client(handler)

    with pytest.raises(ValueError):
        await client.generate([{"role": "user", "content": "hi"}])


async def test_generate_schema_violation_raises_validation_error() -> None:
    bad_content = json.dumps(
        {
            "summary": "s",
            "reviews": [
                {
                    "severity": "major",
                    "confidence": 2.0,
                    "filePath": "a.py",
                    "line": 1,
                    "title": "t",
                    "message": "m",
                }
            ],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _openai_response(bad_content)

    client = _client(handler)

    with pytest.raises(ValidationError):
        await client.generate([{"role": "user", "content": "hi"}])
