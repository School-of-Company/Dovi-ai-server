from app.review.schema import ReviewComment

_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "suggestion": 3}


def filter_reviews(
    reviews: list[ReviewComment],
    *,
    min_confidence: float = 0.5,
    max_comments: int = 8,
    max_per_file: int = 3,
    require_evidence: bool = True,
) -> list[ReviewComment]:
    kept = [
        r
        for r in reviews
        if r.confidence >= min_confidence and (not require_evidence or r.evidence)
    ]

    # 정렬을 먼저 해서, 같은 위치 중복 중 가장 심각도·신뢰도 높은 리뷰가 남도록 한다
    sorted_kept = sorted(
        kept, key=lambda r: (_SEVERITY_ORDER[r.severity], -r.confidence)
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
