import json
import re

from app.review.schema import ReviewModelOutput, VerificationResult

_FENCE = re.compile(r"```(?:json)?\s*(.+)\s*```", re.DOTALL)


def _parse_fenced_json(text: str) -> object:
    """LLM 응답 문자열에서 JSON을 추출한다.

    markdown code fence(```json ... ```)로 감싼 경우 내부 JSON을 추출한다.

    Raises:
        ValueError: JSON 디코딩 실패
    """
    candidate = text.strip()
    match = _FENCE.search(candidate)
    if match:
        candidate = match.group(1).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in LLM output: {exc}") from exc


def parse_review_output(text: str) -> ReviewModelOutput:
    """LLM 응답 문자열을 ReviewModelOutput으로 파싱한다.

    Raises:
        ValueError: JSON 디코딩 실패
        pydantic.ValidationError: 스키마 검증 실패
    """
    return ReviewModelOutput.model_validate(_parse_fenced_json(text))


def parse_verification_result(text: str) -> VerificationResult:
    """LLM 응답 문자열을 VerificationResult로 파싱한다.

    Raises:
        ValueError: JSON 디코딩 실패
        pydantic.ValidationError: 스키마 검증 실패
    """
    return VerificationResult.model_validate(_parse_fenced_json(text))
