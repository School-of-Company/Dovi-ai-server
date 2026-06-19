import pytest
from pydantic import ValidationError

from app.llm.output_parser import parse_review_output

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
        '"suggestedFix": "```python\\nprint(\'hi\')\\n```"}]}'
    )
    text = f"```json\n{nested_json}\n```"
    result = parse_review_output(text)
    assert result.reviews[0].suggested_fix == "```python\nprint('hi')\n```"


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
