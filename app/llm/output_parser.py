import json
import re

from app.review.schema import ReviewModelOutput

_FENCE = re.compile(r"```(?:json)?\s*(.+)\s*```", re.DOTALL)


def parse_review_output(text: str) -> ReviewModelOutput:
    """LLM 응답 문자열을 ReviewModelOutput으로 파싱한다.

    markdown code fence(```json ... ```)로 감싼 경우 내부 JSON을 추출한다.

    Raises:
        ValueError: JSON 디코딩 실패
        pydantic.ValidationError: 스키마 검증 실패
    """
    candidate = text.strip()
    match = _FENCE.search(candidate)
    if match:
        candidate = match.group(1).strip()

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in LLM output: {exc}") from exc

    return ReviewModelOutput.model_validate(data)
