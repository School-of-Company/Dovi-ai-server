import logging

import httpx
from langfuse import get_client, observe

from app.llm.output_parser import parse_review_output, parse_verification_result
from app.review.schema import ReviewModelOutput, VerificationResult

logger = logging.getLogger(__name__)

ChatMessage = dict[str, str]


class OpenAICompatibleLLMClient:
    """llama.cpp/vLLM/SGLang 등 OpenAI-compatible /v1/chat/completions 엔드포인트 구현체.

    런타임이 바뀌어도(LLM_BASE_URL/LLM_MODEL 교체) pipeline 코드는 그대로 유지된다.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._client = client or httpx.AsyncClient(
            base_url=base_url, timeout=timeout_seconds
        )
        self._schema = ReviewModelOutput.model_json_schema(by_alias=True)
        self._verification_schema = VerificationResult.model_json_schema(by_alias=True)

    async def generate(
        self, messages: list[ChatMessage], *, max_tokens: int = 1500
    ) -> ReviewModelOutput:
        content = await self._complete(
            messages,
            max_tokens=max_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "review_output", "schema": self._schema},
            },
        )
        return parse_review_output(content)

    async def generate_text(
        self, messages: list[ChatMessage], *, max_tokens: int = 500
    ) -> str:
        return await self._complete(messages, max_tokens=max_tokens)

    async def verify_findings(
        self, messages: list[ChatMessage], *, max_tokens: int = 800
    ) -> VerificationResult:
        content = await self._complete(
            messages,
            max_tokens=max_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "verification_result",
                    "schema": self._verification_schema,
                },
            },
        )
        return parse_verification_result(content)

    @observe(as_type="generation", name="llm-complete")
    async def _complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int,
        response_format: dict[str, object] | None = None,
    ) -> str:
        # generate()/generate_text()/verify_findings() 전부 이 헬퍼 하나를 거치므로,
        # 계측 지점을 여기 한 곳에만 두면 셋 다 자동으로 트레이싱된다. Langfuse가
        # 설정 안 돼 있으면(LANGFUSE_ENABLED=false) get_client()는 그냥 no-op이라
        # 아래 update_current_generation 호출도 안전하게 아무 일도 안 한다.
        get_client().update_current_generation(model=self._model, input=messages)

        payload: dict[str, object] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        try:
            response = await self._client.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            logger.warning("LLM request timed out model=%s", self._model)
            raise TimeoutError("LLM request timed out") from exc

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            logger.exception(
                "LLM server returned error status=%s model=%s",
                response.status_code,
                self._model,
            )
            raise

        data = response.json()

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            logger.exception("unexpected LLM response shape model=%s", self._model)
            raise ValueError(f"unexpected LLM response shape: {exc}") from exc

        if not isinstance(content, str):
            raise ValueError(f"LLM response content is not a string: {content!r}")

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        usage_details: dict[str, int] = {}
        if isinstance(usage.get("prompt_tokens"), int):
            usage_details["input"] = usage["prompt_tokens"]
        if isinstance(usage.get("completion_tokens"), int):
            usage_details["output"] = usage["completion_tokens"]
        get_client().update_current_generation(output=content, usage_details=usage_details)

        return content

    async def aclose(self) -> None:
        await self._client.aclose()
