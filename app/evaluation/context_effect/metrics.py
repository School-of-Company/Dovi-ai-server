"""조건 A/B/C 실행 결과를 사람 라벨과 비교해 지표를 계산하는 순수 함수 모음.

부작용(파일 I/O, 네트워크)이 전혀 없다 — 입력은 전부 CaseRunResult/CaseLabels
값 자체다.
"""

from __future__ import annotations

import statistics
from typing import Literal

from app.evaluation.context_effect.schema import (
    CaseLabels,
    CaseRunResult,
    Condition,
    ConditionMetrics,
    ExpectedIssue,
)
from app.review.schema import ReviewComment

_CRITICAL_MAJOR_SEVERITIES = {"critical", "major"}


def _matching_expected(
    comment: ReviewComment, labels: CaseLabels, tolerance: int
) -> ExpectedIssue | None:
    for expected in labels.expected:
        if expected.file_path != comment.file_path:
            continue
        if expected.line_start - tolerance <= comment.line <= expected.line_end + tolerance:
            return expected
    return None


def classify_finding(
    comment: ReviewComment, labels: CaseLabels, *, tolerance: int = 3
) -> Literal["true_positive", "false_positive", "unjudged"]:
    """finding 하나를 사람 라벨에 비추어 분류한다.

    expected 매칭(파일 일치 + tolerance 내 라인)이 judgments보다 항상 먼저
    확인된다 — expected로 이미 매칭되는 finding은 사람이 judgments에 뭐라
    적었든(혹은 안 적었든) true_positive로 취급한다.
    """
    if _matching_expected(comment, labels, tolerance) is not None:
        return "true_positive"

    judgment = labels.judgments.get(f"{comment.file_path}:{comment.line}:{comment.title}")
    if judgment == "valid":
        return "true_positive"
    if judgment == "false_positive":
        return "false_positive"
    return "unjudged"


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    quantiles = statistics.quantiles(values, n=100, method="inclusive")
    index = min(max(int(pct) - 1, 0), len(quantiles) - 1)
    return quantiles[index]


def compute_metrics(
    results: list[CaseRunResult], labels_by_case: dict[str, CaseLabels]
) -> dict[Condition, ConditionMetrics]:
    """조건별로 그룹핑해 오탐률/재현율/근거 관련 지표를 계산한다.

    unjudged finding은 false_positive_rate 분자(FP)에도 포함한다(보수적으로
    카운트) — 다만 unjudged_count로 별도 노출해, 진짜 오탐과 "아직 라벨링
    안 됨"을 구분할 수 있게 한다.
    """
    by_condition: dict[Condition, list[CaseRunResult]] = {}
    for result in results:
        by_condition.setdefault(result.condition, []).append(result)

    metrics: dict[Condition, ConditionMetrics] = {}
    for condition, case_results in by_condition.items():
        case_count = len(case_results)
        failures = [r for r in case_results if r.failed is not None]
        failure_count = len(failures)
        failures_by_reason: dict[str, int] = {}
        for failure in failures:
            assert failure.failed is not None
            reason = failure.failed.reason
            failures_by_reason[reason] = failures_by_reason.get(reason, 0) + 1

        finding_count = 0
        true_positive_count = 0
        false_positive_count = 0
        unjudged_count = 0
        critical_major_fp_count = 0
        matched_expected_ids: set[tuple[str, str]] = set()

        seen_case_ids: set[str] = set()
        total_expected = 0

        evidence_case_count = 0
        evidence_prompt_hits = 0
        evidence_linked_findings = 0
        latencies: list[float] = []
        cache_hit_total = 0
        cache_total = 0

        for result in case_results:
            labels = labels_by_case.get(result.case_id)
            if labels is not None and result.case_id not in seen_case_ids:
                seen_case_ids.add(result.case_id)
                total_expected += len(labels.expected)

            if result.changed_packages:
                evidence_case_count += 1
                if result.evidence_in_prompt:
                    evidence_prompt_hits += 1

            if result.evidence_latency_ms is not None:
                latencies.append(result.evidence_latency_ms)
            cache_hit_total += result.cache_hits
            cache_total += result.cache_hits + result.cache_misses

            if result.completed is None or labels is None:
                continue

            for comment in result.completed.reviews:
                finding_count += 1
                classification = classify_finding(comment, labels)
                matched = _matching_expected(comment, labels, tolerance=3)
                if matched is not None:
                    matched_expected_ids.add((result.case_id, matched.id))

                if classification == "true_positive":
                    true_positive_count += 1
                elif classification == "false_positive":
                    false_positive_count += 1
                else:
                    unjudged_count += 1

                if (
                    classification != "true_positive"
                    and comment.severity in _CRITICAL_MAJOR_SEVERITIES
                ):
                    critical_major_fp_count += 1

                if result.changed_packages:
                    haystack = f"{comment.title} {comment.message} {' '.join(comment.evidence)}"
                    if any(pkg in haystack for pkg in result.changed_packages):
                        evidence_linked_findings += 1

        false_positive_rate = (
            (false_positive_count + unjudged_count) / finding_count
            if finding_count
            else None
        )
        recall = len(matched_expected_ids) / total_expected if total_expected else None

        if condition == "C":
            evidence_prompt_rate = (
                evidence_prompt_hits / evidence_case_count if evidence_case_count else None
            )
            evidence_linked_finding_rate = (
                evidence_linked_findings / finding_count if finding_count else None
            )
            evidence_latency_p50_ms = _percentile(latencies, 50)
            evidence_latency_p95_ms = _percentile(latencies, 95)
            cache_hit_rate = cache_hit_total / cache_total if cache_total else None
        else:
            evidence_prompt_rate = None
            evidence_linked_finding_rate = None
            evidence_latency_p50_ms = None
            evidence_latency_p95_ms = None
            cache_hit_rate = None

        metrics[condition] = ConditionMetrics(
            condition=condition,
            case_count=case_count,
            failure_count=failure_count,
            failures_by_reason=failures_by_reason,
            finding_count=finding_count,
            true_positive_count=true_positive_count,
            false_positive_count=false_positive_count,
            unjudged_count=unjudged_count,
            false_positive_rate=false_positive_rate,
            recall=recall,
            critical_major_false_positive_count=critical_major_fp_count,
            evidence_prompt_rate=evidence_prompt_rate,
            evidence_linked_finding_rate=evidence_linked_finding_rate,
            evidence_latency_p50_ms=evidence_latency_p50_ms,
            evidence_latency_p95_ms=evidence_latency_p95_ms,
            cache_hit_rate=cache_hit_rate,
        )

    return metrics


def pending_judgments(
    results: list[CaseRunResult], labels_by_case: dict[str, CaseLabels]
) -> list[dict[str, object]]:
    """classify_finding()이 "unjudged"로 분류한 finding들을 사람이 라벨링할 수
    있는 형태로 뽑아낸다.

    각 항목은 사람이 확인 후 CaseLabels.judgments에
    f"{file_path}:{line}:{title}": "valid" 또는 "false_positive"로 채워 넣을
    수 있도록 case_id/condition/file_path/line/title/message를 그대로 담은
    라벨링 템플릿이다.
    """
    pending: list[dict[str, object]] = []
    for result in results:
        if result.completed is None:
            continue
        labels = labels_by_case.get(result.case_id)
        if labels is None:
            continue
        for comment in result.completed.reviews:
            if classify_finding(comment, labels) != "unjudged":
                continue
            pending.append(
                {
                    "caseId": result.case_id,
                    "condition": result.condition,
                    "filePath": comment.file_path,
                    "line": comment.line,
                    "title": comment.title,
                    "message": comment.message,
                }
            )
    return pending


def _format_rate(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "-"


def _format_ms(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "-"


def render_markdown(metrics: dict[Condition, ConditionMetrics]) -> str:
    """A/B/C를 행으로, 지표들을 열로 하는 마크다운 표를 만든다. None은 "-"로 표시한다."""
    headers = [
        "condition",
        "case_count",
        "failure_count",
        "failures_by_reason",
        "finding_count",
        "true_positive_count",
        "false_positive_count",
        "unjudged_count",
        "false_positive_rate",
        "recall",
        "critical_major_fp_count",
        "evidence_prompt_rate",
        "evidence_linked_finding_rate",
        "evidence_latency_p50_ms",
        "evidence_latency_p95_ms",
        "cache_hit_rate",
    ]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for condition in ("A", "B", "C"):
        m = metrics.get(condition)
        if m is None:
            continue
        failures_text = (
            ", ".join(f"{reason}:{count}" for reason, count in sorted(m.failures_by_reason.items()))
            or "-"
        )
        row = [
            m.condition,
            str(m.case_count),
            str(m.failure_count),
            failures_text,
            str(m.finding_count),
            str(m.true_positive_count),
            str(m.false_positive_count),
            str(m.unjudged_count),
            _format_rate(m.false_positive_rate),
            _format_rate(m.recall),
            str(m.critical_major_false_positive_count),
            _format_rate(m.evidence_prompt_rate),
            _format_rate(m.evidence_linked_finding_rate),
            _format_ms(m.evidence_latency_p50_ms),
            _format_ms(m.evidence_latency_p95_ms),
            _format_rate(m.cache_hit_rate),
        ]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)
