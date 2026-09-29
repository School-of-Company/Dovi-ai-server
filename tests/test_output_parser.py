import pytest
from pydantic import ValidationError

from app.llm.output_parser import (
    parse_partial_review_output,
    parse_review_output,
    parse_verification_result,
)

_VALID = '{"summary": "ok", "reviews": []}'


def test_parses_plain_json() -> None:
    result = parse_review_output(_VALID)
    assert result.summary == "ok"
    assert result.reviews == []


def test_parses_json_in_code_fence() -> None:
    text = f"여기 결과입니다:\n```json\n{_VALID}\n```\n참고하세요."
    result = parse_review_output(text)
    assert result.summary == "ok"


def test_parses_fence_without_lang() -> None:
    text = f"```\n{_VALID}\n```"
    result = parse_review_output(text)
    assert result.summary == "ok"


def test_parses_json_with_nested_code_fence() -> None:
    nested_json = (
        '{"summary": "ok", "reviews": [{"severity": "major", "confidence": 0.9, '
        '"filePath": "a.py", "line": 1, "title": "t", "message": "m", '
        '"evidence": ["e"], '
        '"suggestedFix": "```python\\nprint(\'hi\')\\n```"}]}'
    )
    text = f"```json\n{nested_json}\n```"
    result = parse_review_output(text)
    assert result.reviews[0].suggested_fix == "```python\nprint('hi')\n```"


def test_empty_evidence_raises_validation_error() -> None:
    text = (
        '{"summary": "s", "reviews": [{"severity": "major", "confidence": 0.9, '
        '"filePath": "a.py", "line": 1, "title": "t", "message": "m", '
        '"evidence": []}]}'
    )
    with pytest.raises(ValidationError):
        parse_review_output(text)


def test_parses_review_with_comment() -> None:
    text = (
        '{"summary": "s", "reviews": [{"severity": "major", "confidence": 0.9, '
        '"filePath": "a.py", "line": 3, "title": "t", "message": "m", '
        '"evidence": ["e"]}]}'
    )
    result = parse_review_output(text)
    assert len(result.reviews) == 1
    assert result.reviews[0].file_path == "a.py"


def test_invalid_json_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_review_output("not json at all")


def test_schema_violation_raises_validation_error() -> None:
    # confidence > 1.0 → 스키마 검증 실패
    text = (
        '{"summary": "s", "reviews": [{"severity": "major", "confidence": 2.0, '
        '"filePath": "a.py", "line": 1, "title": "t", "message": "m"}]}'
    )
    with pytest.raises(ValidationError):
        parse_review_output(text)


def test_parses_verification_result() -> None:
    text = '{"verdicts": [{"index": 0, "confirmed": true, "reason": "실제 버그"}]}'
    result = parse_verification_result(text)
    assert len(result.verdicts) == 1
    assert result.verdicts[0].index == 0
    assert result.verdicts[0].confirmed is True


def test_parses_verification_result_in_code_fence() -> None:
    text = '```json\n{"verdicts": []}\n```'
    result = parse_verification_result(text)
    assert result.verdicts == []


def test_verification_result_invalid_json_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_verification_result("not json at all")


# --- parse_partial_review_output (이슈 #99, 출력 잘림 부분 복구) ---


def test_parse_partial_review_output_recovers_completed_findings() -> None:
    # finding 3개 중 2개까지만 완성되고 3번째는 message 값 중간에서 잘렸다.
    text = (
        '{"summary": "부분 결과입니다.", "reviews": ['
        '{"severity": "major", "confidence": 0.9, "filePath": "a.py", "line": 3, '
        '"title": "t1", "message": "m1", "evidence": ["e1"]}, '
        '{"severity": "critical", "confidence": 0.95, "filePath": "b.py", "line": 10, '
        '"title": "t2", "message": "m2", "evidence": ["e2"]}, '
        '{"severity": "minor", "confidence": 0.6, "filePath": "c.py", "line": 5, '
        '"title": "t3", "message": "잘'
    )

    result = parse_partial_review_output(text)

    assert result is not None
    assert [r.file_path for r in result.output.reviews] == ["a.py", "b.py"]
    assert result.duplicates_removed == 0
    assert result.output.summary.startswith("부분 결과입니다.")
    assert "잘려" in result.output.summary


def test_parse_partial_review_output_ignores_braces_and_quotes_inside_message() -> None:
    # message 안에 코드가 들어가면 `{`, `}`, 이스케이프된 `"`가 문자열 그대로
    # 나온다 — 정규식이었으면 여기서 원소 경계를 잘못 잡았을 것이다.
    text = (
        '{"summary": "ok", "reviews": ['
        '{"severity": "major", "confidence": 0.9, "filePath": "a.py", "line": 1, '
        '"title": "t", "message": "if (x == \\"{}\\") { return; }", '
        '"evidence": ["e"]}, '
        '{"severity": "major", "confidence": 0.8, "filePath": "b.py", "line": 2, '
        '"title": "t2", "message": "잘'
    )

    result = parse_partial_review_output(text)

    assert result is not None
    assert len(result.output.reviews) == 1
    assert result.output.reviews[0].message == 'if (x == "{}") { return; }'


def test_parse_partial_review_output_dedupes_repeated_findings() -> None:
    # 반복 루프로 잘린 출력은 같은 finding이 여러 번 나올 수 있다.
    finding = (
        '{"severity": "critical", "confidence": 0.9, "filePath": "a.py", "line": 3, '
        '"title": "t", "message": "same message", "evidence": ["e"]}'
    )
    text = f'{{"summary": "loop", "reviews": [{finding}, {finding}, {finding}]}}'

    result = parse_partial_review_output(text)

    assert result is not None
    assert len(result.output.reviews) == 1
    assert result.duplicates_removed == 2


def test_parse_partial_review_output_drops_schema_invalid_items() -> None:
    # 닫혀 있어도(완성돼 보여도) 스키마 제약(confidence <= 1.0)을 어기면 버린다.
    text = (
        '{"summary": "ok", "reviews": ['
        '{"severity": "major", "confidence": 2.0, "filePath": "a.py", "line": 1, '
        '"title": "t", "message": "m", "evidence": ["e"]}, '
        '{"severity": "major", "confidence": 0.9, "filePath": "b.py", "line": 2, '
        '"title": "t2", "message": "m2", "evidence": ["e2"]}'
        "]}"
    )

    result = parse_partial_review_output(text)

    assert result is not None
    assert len(result.output.reviews) == 1
    assert result.output.reviews[0].file_path == "b.py"


def test_parse_partial_review_output_defaults_summary_when_value_not_closed() -> None:
    # summary 문자열 값 자체가 끝까지 안 닫혀 있으면 기본 문구로 대체한다.
    text = (
        '{"reviews": ['
        '{"severity": "major", "confidence": 0.9, "filePath": "a.py", "line": 1, '
        '"title": "t", "message": "m", "evidence": ["e"]}'
        '], "summary": "이 문장은 끝까지 안 닫힘'
    )

    result = parse_partial_review_output(text)

    assert result is not None
    assert result.output.summary.startswith(
        "모델 출력이 중간에 잘려 summary를 복구하지 못했습니다."
    )


def test_parse_partial_review_output_returns_none_when_nothing_recoverable() -> None:
    text = (
        '{"summary": "ok", "reviews": [{"severity": "major", "confidence": 0.9, '
        '"filePath": "a.py"'
    )

    assert parse_partial_review_output(text) is None


def test_parse_partial_review_output_handles_well_formed_json_too() -> None:
    # 정상(안 잘린) 입력도 망가뜨리지 않아야 한다.
    text = (
        '{"summary": "정상 종료", "reviews": ['
        '{"severity": "major", "confidence": 0.9, "filePath": "a.py", "line": 1, '
        '"title": "t", "message": "m", "evidence": ["e"]}'
        "]}"
    )

    result = parse_partial_review_output(text)

    assert result is not None
    assert len(result.output.reviews) == 1
    assert result.output.summary.startswith("정상 종료")
    assert result.duplicates_removed == 0
