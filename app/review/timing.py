from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewTimingRecord:
    """리뷰 한 건의 단계별 소요 시간(ms). 캐시·추론 설정 같은 성능 작업의 근거로 쓴다(이슈 #134)."""

    review_job_id: str
    total_ms: int
    prep_ms: int
    generate_ms: int
    summary_ms: int
    verify_ms: int
    batches: int
    targets: int
    prompt_chars: int


def to_ms(seconds: float) -> int:
    return round(seconds * 1000)
