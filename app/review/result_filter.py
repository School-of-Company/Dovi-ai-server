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
    """inline에 안 다는 minor/suggestion 리뷰를 summary에 붙일 제목 목록으로 추린다."""
    return [
        r.title
        for r in reviews
        if r.severity not in _INLINE_SEVERITIES and r.confidence >= min_confidence
    ]
