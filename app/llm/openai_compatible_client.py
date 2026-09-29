import copy
import logging

import httpx
from langfuse import get_client, observe

from app.llm.errors import LLMOutputTruncatedError
from app.llm.output_parser import parse_review_output, parse_verification_result
from app.review.schema import ReviewModelOutput, VerificationResult

# 잘림 재시도(이슈 #99)에서 response_format 스키마에 걸어두는 message 길이
# 상한. "2문장 이내"를 문자 수로 대략 환산한 값 — 문법 차원에서 강제되면
# 출력 자체가 짧아져 잘림 재발을 줄이지만, 서버가 이 제약을 실제로 지원하는지
# 확인되지 않았으므로 파이프라인은 이 값과 무관하게 개수를 별도로도 확인한다.
_TRUNCATION_RETRY_MAX_MESSAGE_CHARS = 300

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
        # /tokenize, /props는 llama.cpp의 네이티브 라우트라 /v1 프리픽스 밖에
        # 있다 — base_url("http://host:port/v1")에서 /v1을 뗀 루트로 따로
        # 호출해야 해서 base_url 자체를 보관해둔다(기존엔 httpx client 생성에만
        # 쓰고 버리고 있었음).
        self._base_url = base_url
        self._client = client or httpx.AsyncClient(
            base_url=base_url, timeout=timeout_seconds
        )
        self._schema = ReviewModelOutput.model_json_schema(by_alias=True)
        self._verification_schema = VerificationResult.model_json_schema(by_alias=True)

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1500,
        max_reviews: int | None = None,
    ) -> ReviewModelOutput:
        schema = (
            self._schema
            if max_reviews is None
            else self._truncation_retry_schema(max_reviews)
        )
        content, finish_reason = await self._complete(
            messages,
            max_tokens=max_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "review_output", "schema": schema},
            },
        )
        try:
            return parse_review_output(content)
        except ValueError:
            # finish_reason == "length"면 형식 오류가 아니라 출력이 잘린
            # 것이다 — 호출자(ReviewPipeline)가 잘린 원문에서 부분 복구를
            # 시도할 수 있도록 원문을 실어 별도 예외로 던진다(이슈 #99).
            if finish_reason == "length":
                raise LLMOutputTruncatedError(
                    "LLM output truncated at max_tokens", raw_content=content
                ) from None
            raise

    async def generate_text(
        self, messages: list[ChatMessage], *, max_tokens: int = 500
    ) -> str:
        content, _finish_reason = await self._complete(messages, max_tokens=max_tokens)
        return content

    async def verify_findings(
        self, messages: list[ChatMessage], *, max_tokens: int = 800
    ) -> VerificationResult:
        content, _finish_reason = await self._complete(
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

    def _truncation_retry_schema(self, max_reviews: int) -> dict[str, object]:
        """잘림 재시도 전용 스키마 사본 — reviews 개수와 message 길이에 문법
        차원의 상한을 건다(이슈 #99). 원본 self._schema는 다른 호출에서 계속
        재사용되므로 깊은 복사본만 수정한다.
        """
        schema = copy.deepcopy(self._schema)
        schema["properties"]["reviews"]["maxItems"] = max_reviews
        schema["$defs"]["ReviewComment"]["properties"]["message"]["maxLength"] = (
            _TRUNCATION_RETRY_MAX_MESSAGE_CHARS
        )
        return schema

    async def count_tokens(self, text: str) -> int:
        """llama.cpp의 /tokenize로 실제 토큰 수를 센다. 실패하면 예외를 그대로
        던진다 — 폴백은 호출자(ReviewPipeline) 책임이다."""
        root = self._base_url.removesuffix("/v1")
        response = await self._client.post(f"{root}/tokenize", json={"content": text})
        response.raise_for_status()
        data = response.json()
        tokens = data["tokens"]
        if not isinstance(tokens, list):
            raise ValueError(f"unexpected /tokenize response shape: {data!r}")
        return len(tokens)

    async def get_context_window(self) -> int | None:
        """llama.cpp의 /props에서 요청 하나가 실제로 쓸 수 있는 n_ctx를 읽는다.

        버전에 따라 위치가 달라, 요청당 값(default_generation_settings.n_ctx)을
        먼저 보고 없으면 최상위 n_ctx를 본다 — 최상위 값은 슬롯 여러 개의 합산치일
        수 있어 요청 하나가 실제로 쓸 수 있는 양이 아닐 수 있다. 파싱까지 전부
        같은 try 안에서 처리해, 예상과 다른 응답 구조에도 예외 없이 None을
        반환한다는 계약을 지킨다.
        """
        root = self._base_url.removesuffix("/v1")
        try:
            response = await self._client.get(f"{root}/props")
            response.raise_for_status()
            data = response.json()
            gen_settings = data.get("default_generation_settings")
            n_ctx = (
                gen_settings.get("n_ctx") if isinstance(gen_settings, dict) else None
            ) or data.get("n_ctx")
            return int(n_ctx) if isinstance(n_ctx, int) else None
        except Exception:
            return None

    @observe(as_type="generation", name="llm-complete")
    async def _complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int,
        response_format: dict[str, object] | None = None,
    ) -> tuple[str, str | None]:
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

        # finish_reason == "length"는 출력이 max_tokens에 걸려 잘렸다는 뜻이라,
        # 이후 parse_error가 나면 "모델이 형식을 못 지켰다"가 아니라 "출력이
        # 잘렸다"는 걸 구분하는 근거가 된다 — generate()가 이 값을 보고
        # LLMOutputTruncatedError로 바꿔 던진다(이슈 #99). choices[0]은 위에서
        # 이미 성공적으로 접근했으므로(content 추출) 여기서 다시 존재를 확인할
        # 필요는 없다.
        finish_reason = data["choices"][0].get("finish_reason")
        if finish_reason == "length":
            logger.warning("LLM output truncated by max_tokens model=%s", self._model)

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        usage_details: dict[str, int] = {}
        if isinstance(usage.get("prompt_tokens"), int):
            usage_details["input"] = usage["prompt_tokens"]
        if isinstance(usage.get("completion_tokens"), int):
            usage_details["output"] = usage["completion_tokens"]
        get_client().update_current_generation(
            output=content,
            usage_details=usage_details,
            metadata={"finish_reason": finish_reason},
        )

        return content, finish_reason

    async def aclose(self) -> None:
        await self._client.aclose()
