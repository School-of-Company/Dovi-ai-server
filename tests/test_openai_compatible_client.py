import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.llm.openai_compatible_client import OpenAICompatibleLLMClient
from app.review.schema import ReviewModelOutput, VerificationResult

_VALID_CONTENT = json.dumps({"summary": "ok", "reviews": []})
_VALID_VERIFICATION_CONTENT = json.dumps(
    {"verdicts": [{"index": 0, "confirmed": True, "reason": "ok"}]}
)


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
    captured: dict[str, Any] = {}

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


async def test_generate_non_dict_body_raises_value_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "dict"])

    client = _client(handler)

    with pytest.raises(ValueError):
        await client.generate([{"role": "user", "content": "hi"}])


async def test_generate_null_content_raises_value_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": None}}],
                "usage": {"completion_tokens": 0},
            },
        )

    client = _client(handler)

    with pytest.raises(ValueError):
        await client.generate([{"role": "user", "content": "hi"}])


async def test_generate_invalid_json_content_raises_value_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _openai_response("not json")

    client = _client(handler)

    with pytest.raises(ValueError):
        await client.generate([{"role": "user", "content": "hi"}])


async def test_generate_text_returns_plain_string() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _openai_response("이건 자유 텍스트 답변입니다.")

    client = _client(handler)
    result = await client.generate_text([{"role": "user", "content": "hi"}])

    assert result == "이건 자유 텍스트 답변입니다."


async def test_generate_text_does_not_set_response_format() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _openai_response("답변")

    client = _client(handler)
    await client.generate_text([{"role": "user", "content": "hi"}], max_tokens=300)

    body = captured["body"]
    assert body["max_tokens"] == 300
    assert "response_format" not in body


async def test_generate_text_timeout_raises_builtin_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(handler)

    with pytest.raises(TimeoutError):
        await client.generate_text([{"role": "user", "content": "hi"}])


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


async def test_verify_findings_returns_parsed_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _openai_response(_VALID_VERIFICATION_CONTENT)

    client = _client(handler)
    result = await client.verify_findings([{"role": "user", "content": "hi"}])

    assert isinstance(result, VerificationResult)
    assert result.verdicts[0].confirmed is True


async def test_verify_findings_sends_its_own_json_schema() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _openai_response(_VALID_VERIFICATION_CONTENT)

    client = _client(handler)
    await client.verify_findings([{"role": "user", "content": "hi"}], max_tokens=200)

    body = captured["body"]
    assert body["max_tokens"] == 200
    assert body["response_format"]["json_schema"]["name"] == "verification_result"
    schema = body["response_format"]["json_schema"]["schema"]
    assert "verdicts" in schema["properties"]


async def test_verify_findings_timeout_raises_builtin_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(handler)

    with pytest.raises(TimeoutError):
        await client.verify_findings([{"role": "user", "content": "hi"}])


class FakeLangfuseClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def update_current_generation(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


async def test_records_langfuse_generation_with_model_input_output_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_langfuse = FakeLangfuseClient()
    monkeypatch.setattr(
        "app.llm.openai_compatible_client.get_client", lambda: fake_langfuse
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _openai_response(_VALID_CONTENT)

    client = _client(handler)
    messages = [{"role": "user", "content": "hi"}]
    await client.generate(messages)

    # 첫 호출은 model/input, 두 번째 호출은 output/usage — _complete() 안에서
    # 요청 전/후로 두 번 update한다.
    assert len(fake_langfuse.calls) == 2
    assert fake_langfuse.calls[0] == {"model": "test-model", "input": messages}
    assert fake_langfuse.calls[1] == {
        "output": _VALID_CONTENT,
        "usage_details": {"output": 10},
        "metadata": {"finish_reason": None},
    }


async def test_generate_records_finish_reason_length_in_langfuse_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_langfuse = FakeLangfuseClient()
    monkeypatch.setattr(
        "app.llm.openai_compatible_client.get_client", lambda: fake_langfuse
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": _VALID_CONTENT}, "finish_reason": "length"}
                ],
                "usage": {"completion_tokens": 10},
            },
        )

    client = _client(handler)
    await client.generate([{"role": "user", "content": "hi"}])

    assert fake_langfuse.calls[1]["metadata"] == {"finish_reason": "length"}


async def test_count_tokens_calls_tokenize_on_v1_stripped_root() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"tokens": [1, 2, 3, 4, 5]})

    client = _client(handler)
    result = await client.count_tokens("hello world")

    assert captured["url"] == "http://localhost:8001/tokenize"
    assert captured["body"] == {"content": "hello world"}
    assert result == 5


async def test_count_tokens_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _client(handler)

    with pytest.raises(httpx.HTTPStatusError):
        await client.count_tokens("hello")


async def test_count_tokens_raises_on_malformed_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    client = _client(handler)

    with pytest.raises((KeyError, ValueError)):
        await client.count_tokens("hello")


async def test_get_context_window_prefers_per_request_n_ctx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://localhost:8001/props"
        return httpx.Response(
            200,
            json={"n_ctx": 32768, "default_generation_settings": {"n_ctx": 8192}},
        )

    client = _client(handler)
    result = await client.get_context_window()

    # 요청당 실제 usable 값(default_generation_settings.n_ctx)을 최상위 값보다
    # 우선한다 — 최상위 값은 슬롯 여러 개의 합산치일 수 있다.
    assert result == 8192


async def test_get_context_window_falls_back_to_top_level_n_ctx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"n_ctx": 8192})

    client = _client(handler)
    result = await client.get_context_window()

    assert result == 8192


async def test_get_context_window_returns_none_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _client(handler)

    assert await client.get_context_window() is None


async def test_get_context_window_returns_none_when_default_generation_settings_is_null() -> (
    None
):
    # 일부 llama.cpp 버전은 default_generation_settings가 null일 수 있다 —
    # 파싱이 try 밖에서 AttributeError를 던지면 안 된다("실패하면 None" 계약).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"default_generation_settings": None})

    client = _client(handler)

    assert await client.get_context_window() is None
