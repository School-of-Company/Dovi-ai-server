"""여러 스크립트에서 재사용하는 고정 응답 FakeLLM (네트워크 불필요)."""

from app.llm.client import ChatMessage
from app.review.schema import ReviewComment, ReviewModelOutput, ReviewVerdict, VerificationResult


class FakeLLM:
    async def generate(
        self, messages: list[ChatMessage], *, max_tokens: int = 1500
    ) -> ReviewModelOutput:
        return ReviewModelOutput(
            summary="[FAKE] 잔액 검증 완화 및 과인출 가능성이 있습니다.",
            reviews=[
                ReviewComment(
                    severity="major",
                    confidence=0.9,
                    file_path="app/service/payment.py",
                    line=12,
                    title="[FAKE] 잔액 부족 체크 누락",
                    message="잔액 차감 전 충분한 잔액이 있는지 확인하지 않습니다.",
                    evidence=["balance -= amount"],
                )
            ],
        )

    async def verify_findings(
        self, messages: list[ChatMessage], *, max_tokens: int = 800
    ) -> VerificationResult:
        # 로컬 테스트 스크립트용 fake라 항상 confirmed 처리한다.
        return VerificationResult(
            verdicts=[ReviewVerdict(index=0, confirmed=True, reason="[FAKE] ok")]
        )
