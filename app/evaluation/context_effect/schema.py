from typing import Literal

from app.review.schema import CamelModel, ReviewCompletedEvent, ReviewFailedEvent, Severity

Condition = Literal["A", "B", "C"]


class ExpectedIssue(CamelModel):
    id: str
    file_path: str
    line_start: int
    line_end: int
    severity: Severity
    description: str


class CaseLabels(CamelModel):
    """사람이 라벨링한 평가 케이스 하나(=하나의 PR)에 대한 기대값/판정.

    judgments의 키는 f"{file_path}:{line}:{title}" 형식이며,
    app.review.result_filter.filter_reviews()의 dedup 키인
    (file_path, line, title)과 그대로 대응한다 — ReviewCompletedEvent.reviews에
    최종적으로 남은(이미 dedup된) finding만 사람이 판정하면 된다.
    """

    case_id: str
    category: Literal["dependency", "repo_specific"]
    expected: list[ExpectedIssue] = []
    judgments: dict[str, Literal["valid", "false_positive"]] = {}


class CaseRunResult(CamelModel):
    """케이스 하나를 조건 하나로 돌린 결과.

    completed/failed는 정확히 하나만 채워진다 — ReviewCompletedEvent |
    ReviewFailedEvent 유니온을 그대로 두면 pydantic이 두 모델을 판별하기
    어려우므로, 두 개의 optional 필드로 분리해 직렬화 문제를 피한다.
    """

    case_id: str
    condition: Condition
    completed: ReviewCompletedEvent | None = None
    failed: ReviewFailedEvent | None = None
    evidence_in_prompt: bool = False
    evidence_latency_ms: float | None = None
    cache_hits: int = 0
    cache_misses: int = 0
    changed_packages: list[str] = []


class ConditionMetrics(CamelModel):
    condition: Condition
    case_count: int
    failure_count: int
    failures_by_reason: dict[str, int] = {}
    finding_count: int
    true_positive_count: int
    false_positive_count: int
    unjudged_count: int
    false_positive_rate: float | None
    recall: float | None
    critical_major_false_positive_count: int
    evidence_prompt_rate: float | None = None
    evidence_linked_finding_rate: float | None = None
    evidence_latency_p50_ms: float | None = None
    evidence_latency_p95_ms: float | None = None
    cache_hit_rate: float | None = None
