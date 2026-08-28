import httpx

from app.llm.output_parser import parse_review_output
from app.review.schema import ReviewModelOutput

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

    async def generate(
        self, messages: list[ChatMessage], *, max_tokens: int = 1500
    ) -> ReviewModelOutput:
        payload = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "review_output", "schema": self._schema},
            },
        }

        try:
            response = await self._client.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise TimeoutError("LLM request timed out") from exc

        response.raise_for_status()
        data = response.json()

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"unexpected LLM response shape: {exc}") from exc

        if not isinstance(content, str):
            raise ValueError(f"LLM response content is not a string: {content!r}")

        return parse_review_output(content)

    async def aclose(self) -> None:
        await self._client.aclose()
