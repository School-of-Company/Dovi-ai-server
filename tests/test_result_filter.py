from app.review.result_filter import filter_reviews, summarize_minor
from app.review.schema import ReviewComment, Severity


def _comment(
    *,
    severity: Severity = "major",
    confidence: float = 0.9,
    file_path: str = "a.py",
    line: int = 1,
    title: str = "t",
    message: str = "m",
    evidence: list[str] | None = None,
) -> ReviewComment:
    return ReviewComment(
        severity=severity,
        confidence=confidence,
        file_path=file_path,
        line=line,
        title=title,
        message=message,
        evidence=evidence if evidence is not None else ["e"],
    )


def test_empty() -> None:
    assert filter_reviews([]) == []


def test_drops_low_confidence() -> None:
    reviews = [_comment(confidence=0.3), _comment(confidence=0.8, line=2)]
    result = filter_reviews(reviews, min_confidence=0.5)
    assert len(result) == 1
    assert result[0].line == 2


def test_drops_empty_evidence() -> None:
    # 스키마가 evidence min_length=1을 강제하므로, 빈 evidence는 정상 구성이 불가능하다.
    # model_construct로 검증을 우회해 필터 자체의 방어 로직(defense in depth)을 확인한다.
    empty_evidence = ReviewComment.model_construct(
        severity="major",
        confidence=0.9,
        file_path="a.py",
        line=1,
        title="t",
        message="m",
        evidence=[],
    )
    reviews = [empty_evidence, _comment(line=2, evidence=["x"])]
    result = filter_reviews(reviews, require_evidence=True)
    assert [r.line for r in result] == [2]


def test_drops_blank_evidence() -> None:
    reviews = [_comment(evidence=["", "  "]), _comment(line=2, evidence=["real"])]
    result = filter_reviews(reviews, require_evidence=True)
    assert [r.line for r in result] == [2]


def test_dedupes_same_location() -> None:
    reviews = [_comment(), _comment()]
    assert len(filter_reviews(reviews)) == 1


def test_dedupes_same_location_keeps_highest_severity() -> None:
    reviews = [
        _comment(severity="minor", confidence=0.6),
        _comment(severity="critical", confidence=0.9),
    ]
    result = filter_reviews(reviews)
    assert len(result) == 1
    assert result[0].severity == "critical"


def test_sorts_by_severity_then_confidence() -> None:
    reviews = [
        _comment(severity="critical", line=2, title="b"),
        _comment(severity="major", line=3, title="c"),
    ]
    result = filter_reviews(reviews)
    assert [r.severity for r in result] == ["critical", "major"]


def test_drops_minor_and_suggestion_from_inline() -> None:
    reviews = [
        _comment(severity="minor", line=1, title="a"),
        _comment(severity="suggestion", line=2, title="b"),
        _comment(severity="major", line=3, title="c"),
    ]
    result = filter_reviews(reviews)
    assert [r.severity for r in result] == ["major"]


def test_summarize_minor_collects_non_inline_titles() -> None:
    reviews = [
        _comment(severity="critical", line=1, title="a"),
        _comment(severity="minor", line=2, title="b"),
        _comment(severity="suggestion", line=3, title="c"),
    ]
    assert summarize_minor(reviews) == ["b", "c"]


def test_summarize_minor_drops_low_confidence() -> None:
    reviews = [_comment(severity="minor", line=1, title="a", confidence=0.2)]
    assert summarize_minor(reviews, min_confidence=0.5) == []


def test_max_comments_limit() -> None:
    reviews = [_comment(file_path=f"f{i}.py", line=i + 1, title=str(i)) for i in range(12)]
    result = filter_reviews(reviews, max_comments=8)
    assert len(result) == 8


def test_max_per_file_limit() -> None:
    reviews = [_comment(line=i + 1, title=str(i)) for i in range(5)]
    result = filter_reviews(reviews, max_per_file=3)
    assert len(result) == 3
    assert all(r.file_path == "a.py" for r in result)
