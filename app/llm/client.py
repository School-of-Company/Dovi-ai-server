from typing import Protocol

from app.review.schema import ReviewModelOutput

ChatMessage = dict[str, str]


class LLMClient(Protocol):
    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1500,
        max_reviews: int | None = None,
    ) -> ReviewModelOutput:
        """메시지 목록을 기반으로 코드 리뷰 결과를 생성한다.

        구현체는 라이브러리 전용 예외(예: httpx.TimeoutException)를 아래 표준
        예외로 변환해 던져, 파이프라인의 실패 분류 규약을 유지해야 한다.

        max_reviews를 주면 응답 스키마에 reviews 개수·message 길이 상한을
        걸어 짧은 재시도를 유도한다(이슈 #99) — 서버가 문법 차원에서 이를
        강제하는지는 보장되지 않으므로, 호출자는 결과 개수를 스스로도
        확인해야 한다.

        Raises:
            TimeoutError: LLM API 호출 타임아웃
            ValueError: 응답 파싱 또는 검증 실패 (pydantic ValidationError 포함).
                출력이 max_tokens에 걸려 잘린 경우엔 그 서브클래스인
                app.llm.errors.LLMOutputTruncatedError를 던진다.
        """
        ...

    async def count_tokens(self, text: str) -> int:
        """text의 실제 토큰 수를 센다 (이슈 #98 — 문자 수 기반 예산으로는 한글/
        코드 혼합 텍스트에서 컨텍스트 초과를 보장 못 함).

        실패하면 예외를 그대로 던진다 — 폴백(보수적 추정)은 호출자(파이프라인)의
        책임이다.
        """
        ...

    async def get_context_window(self) -> int | None:
        """서버가 실제로 쓸 수 있는 컨텍스트 크기(usable n_ctx)를 반환한다.

        설정값(`llm_max_context`)이 서버 실행 옵션과 어긋날 수 있어(예: 병렬
        슬롯 수에 따라 요청당 usable 컨텍스트가 설정값보다 작을 수 있음), 서버가
        직접 보고하는 값을 우선한다. 조회할 수 없으면 None을 반환한다(예외를
        던지지 않는다 — 호출자는 항상 설정값으로 폴백할 수 있어야 한다).
        """
        ...
