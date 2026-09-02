from app.review.schema import ReviewComment

_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "suggestion": 3}
_INLINE_SEVERITIES = {"critical", "major"}


def filter_reviews(
    reviews: list[ReviewComment],
    *,
    min_confidence: float = 0.5,
    max_comments: int = 8,
    max_per_file: int = 3,
    require_evidence: bool = True,
) -> list[ReviewComment]:
    """inline PR comment로 달 리뷰만 추린다.

    minor/suggestion은 여기 포함하지 않는다 — inline에 달면 노이즈가 커서,
    summarize_minor()로 요약 텍스트에만 남긴다.
    """
    kept = [
        r
        for r in reviews
        if r.severity in _INLINE_SEVERITIES
        and r.confidence >= min_confidence
        and (not require_evidence or any(e.strip() for e in r.evidence))
    ]

    # 정렬을 먼저 해서, 같은 위치 중복 중 가장 심각도·신뢰도 높은 리뷰가 남도록 한다
    sorted_kept = sorted(
        kept, key=lambda r: (_SEVERITY_ORDER.get(r.severity, 99), -r.confidence)
    )

    seen: set[tuple[str, int, str]] = set()
    deduped: list[ReviewComment] = []
    for r in sorted_kept:
        key = (r.file_path, r.line, r.title)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    per_file: dict[str, int] = {}
    result: list[ReviewComment] = []
    for r in deduped:
        if len(result) >= max_comments:
            break
        if per_file.get(r.file_path, 0) >= max_per_file:
            continue
        per_file[r.file_path] = per_file.get(r.file_path, 0) + 1
        result.append(r)

    return result


def summarize_minor(
    reviews: list[ReviewComment], *, min_confidence: float = 0.5
) -> list[str]:
    """inline에 안 다는 minor/suggestion 리뷰를 summary에 붙일 한 줄 설명 목록으로 추린다.

    title만 남기면 모델이 title을 "코드 중복"처럼 짧게 쓸 때 무슨 문제인지 전혀
    알 수 없는 라벨만 남는다. message(1-3문장 설명)를 함께 붙여서, title이
    부실해도 최소한의 근거가 요약에 남게 한다. message가 비어 있는 항목은
    (evidence가 빈 finding을 버리는 것과 동일하게) 노이즈이므로 제외한다.
    """
    return [
        f"{r.title}: {r.message}"
        for r in reviews
        if r.severity not in _INLINE_SEVERITIES
        and r.confidence >= min_confidence
        and r.message.strip()
    ]
