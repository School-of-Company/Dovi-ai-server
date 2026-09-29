import json
import re
from typing import NamedTuple

from pydantic import ValidationError

from app.review.schema import ReviewComment, ReviewModelOutput, VerificationResult

_FENCE = re.compile(r"```(?:json)?\s*(.+)\s*```", re.DOTALL)

# 부분 복구(#99)가 못 찾으면 대신 쓰는 기본 summary — 잘린 출력엔 summary가
# 끝까지 안 나온 경우가 드물게 있을 수 있다.
_DEFAULT_TRUNCATED_SUMMARY = "모델 출력이 중간에 잘려 summary를 복구하지 못했습니다."

_TRUNCATION_NOTICE = "\n\n※ 모델 출력이 중간에 잘려 일부 finding만 복구했습니다."


def _strip_fence(text: str) -> str:
    """markdown code fence(```json ... ```)가 있으면 안쪽 내용만 남긴다."""
    candidate = text.strip()
    match = _FENCE.search(candidate)
    if match:
        candidate = match.group(1).strip()
    return candidate


def _parse_fenced_json(text: str) -> object:
    """LLM 응답 문자열에서 JSON을 추출한다.

    markdown code fence(```json ... ```)로 감싼 경우 내부 JSON을 추출한다.

    Raises:
        ValueError: JSON 디코딩 실패
    """
    candidate = _strip_fence(text)

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


class PartialRecoveryResult(NamedTuple):
    output: ReviewModelOutput
    duplicates_removed: int


def parse_partial_review_output(text: str) -> PartialRecoveryResult | None:
    """잘린(finish_reason == "length") LLM 출력에서, 끝까지 완성된 finding만
    복구한다(이슈 #99) — 추가 LLM 호출 없이 부분 결과를 살리기 위한 것이다.

    전체 텍스트는 유효한 JSON이 아니므로 json.loads를 통째로 쓸 수 없다.
    "reviews" 배열 안을 문자열/이스케이프 상태를 추적하는 문자 단위 스캐너로
    훑어, 끝까지 닫힌 원소만 골라낸다 — finding의 message/evidence에 코드가
    들어가면 `{`, `}`, `"`가 문자열 안에 그대로 나올 수 있어 정규식으로는
    문자열 경계를 안전하게 구분할 수 없다.

    반복 루프로 잘린 경우 같은 finding이 여러 번 나올 수 있어
    (file_path, line, message) 기준으로 중복을 제거한다.

    복구된 finding이 하나도 없으면 None을 반환한다(호출자가 재시도로 넘어간다).
    """
    candidate = _strip_fence(text)

    array_start = _find_reviews_array_start(candidate)
    item_strings = (
        _extract_array_item_strings(candidate, array_start) if array_start is not None else []
    )

    reviews: list[ReviewComment] = []
    for raw_item in item_strings:
        try:
            obj = json.loads(raw_item)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        try:
            reviews.append(ReviewComment.model_validate(obj))
        except ValidationError:
            continue

    deduped, duplicates_removed = _dedupe_reviews(reviews)
    if not deduped:
        return None

    summary = _find_summary_value(candidate) or _DEFAULT_TRUNCATED_SUMMARY
    summary += _TRUNCATION_NOTICE

    return PartialRecoveryResult(
        output=ReviewModelOutput(summary=summary, reviews=deduped),
        duplicates_removed=duplicates_removed,
    )


def _dedupe_reviews(reviews: list[ReviewComment]) -> tuple[list[ReviewComment], int]:
    seen: set[tuple[str, int, str]] = set()
    deduped: list[ReviewComment] = []
    duplicates = 0
    for review in reviews:
        key = (review.file_path, review.line, review.message)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        deduped.append(review)
    return deduped, duplicates


def _find_reviews_array_start(text: str) -> int | None:
    """최상위 객체에서 "reviews" 키 다음, 그 값인 '[' 바로 뒤 인덱스를 찾는다.

    summary 값(자유 서술 문자열) 안에 우연히 "reviews"라는 글자가 나와도 안
    걸리도록, 문자열 상태를 추적하면서 최상위(depth==1) 객체의 키 위치에서만
    찾는다 — 문자열 내부(in_string=True)에서는 이 매칭 자체를 시도하지 않는다.
    """
    stack: list[str] = []
    in_string = False
    escape = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            if len(stack) == 1 and stack[-1] == "{" and text.startswith('"reviews"', i):
                j = i + len('"reviews"')
                while j < n and text[j].isspace():
                    j += 1
                if j < n and text[j] == ":":
                    j += 1
                    while j < n and text[j].isspace():
                        j += 1
                    if j < n and text[j] == "[":
                        return j + 1
            in_string = True
            i += 1
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
        i += 1
    return None


def _extract_array_item_strings(text: str, start: int) -> list[str]:
    """text[start:]에서 배열의 원소(reviews[i]) 중 끝까지 닫힌 것만 문자열로
    추출한다. 마지막에 잘려서 안 닫힌 원소는 버린다.

    depth 카운터는 `{`/`[`를 구분하지 않고 함께 센다 — 원소 하나의 시작(`{`)과
    끝(대응하는 `}`)만 알면 되고, 그 안의 `evidence: [...]` 같은 중첩 배열도
    같은 카운터로 균형이 맞으면 된다. 문자열 안의 괄호는 in_string 상태일 때
    건드리지 않으므로 속지 않는다.
    """
    items: list[str] = []
    i = start
    n = len(text)
    in_string = False
    escape = False
    depth = 0
    item_start: int | None = None
    while i < n:
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if depth == 0:
            if ch == "{":
                item_start = i
                depth = 1
            elif ch == "]":
                break
            i += 1
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0 and item_start is not None:
                items.append(text[item_start : i + 1])
                item_start = None
        i += 1
    return items


def _find_summary_value(text: str) -> str | None:
    """최상위 "summary" 문자열 값을 반환한다. 값이 끝까지 안 닫혀 있으면(잘림)
    None을 반환해 호출자가 기본 문구로 대체하게 한다.
    """
    stack: list[str] = []
    in_string = False
    escape = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            if len(stack) == 1 and stack[-1] == "{" and text.startswith('"summary"', i):
                j = i + len('"summary"')
                while j < n and text[j].isspace():
                    j += 1
                if j < n and text[j] == ":":
                    j += 1
                    while j < n and text[j].isspace():
                        j += 1
                    if j < n and text[j] == '"':
                        return _read_closed_string(text, j)
            in_string = True
            i += 1
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
        i += 1
    return None


def _read_closed_string(text: str, quote_index: int) -> str | None:
    """text[quote_index]가 여는 따옴표일 때, 그 문자열이 끝까지 닫혀 있으면
    이스케이프를 해제한 값을 반환하고, 안 닫혀 있으면(잘림) None을 반환한다.
    """
    i = quote_index + 1
    n = len(text)
    escape = False
    while i < n:
        ch = text[i]
        if escape:
            escape = False
        elif ch == "\\":
            escape = True
        elif ch == '"':
            raw = text[quote_index : i + 1]
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                return None
            return decoded if isinstance(decoded, str) else None
        i += 1
    return None
