from typing import Protocol

from app.review.schema import ReviewModelOutput

ChatMessage = dict[str, str]


class LLMClient(Protocol):
    async def generate(
        self, messages: list[ChatMessage], *, max_tokens: int = 1500
    ) -> ReviewModelOutput:
        """메시지 목록을 기반으로 코드 리뷰 결과를 생성한다.

        구현체는 라이브러리 전용 예외(예: httpx.TimeoutException)를 아래 표준
        예외로 변환해 던져, 파이프라인의 실패 분류 규약을 유지해야 한다.

        Raises:
            TimeoutError: LLM API 호출 타임아웃
            ValueError: 응답 파싱 또는 검증 실패 (pydantic ValidationError 포함)
        """
        ...
