from app.evaluation.context_effect.metrics import (
    classify_finding,
    compute_metrics,
    pending_judgments,
    render_markdown,
)
from app.evaluation.context_effect.schema import (
    CaseLabels,
    CaseRunResult,
    ExpectedIssue,
)
from app.review.schema import ReviewComment, ReviewCompletedEvent, ReviewFailedEvent


def _comment(
    *,
    file_path: str = "a.py",
    line: int = 10,
    title: str = "t",
    message: str = "m",
    severity: str = "major",
    evidence: list[str] | None = None,
) -> ReviewComment:
    return ReviewComment(
        severity=severity,  # type: ignore[arg-type]
        confidence=0.9,
        file_path=file_path,
        line=line,
        title=title,
        message=message,
        evidence=evidence if evidence is not None else ["e"],
    )


def _labels(
    *,
    case_id: str = "case1",
    category: str = "repo_specific",
    expected: list[ExpectedIssue] | None = None,
    judgments: dict[str, str] | None = None,
) -> CaseLabels:
    return CaseLabels(
        case_id=case_id,
        category=category,  # type: ignore[arg-type]
        expected=expected if expected is not None else [],
        judgments=judgments if judgments is not None else {},  # type: ignore[arg-type]
    )


def _expected(
    *, id: str = "e1", file_path: str = "a.py", line_start: int = 10, line_end: int = 10
) -> ExpectedIssue:
    return ExpectedIssue(
        id=id,
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        severity="major",
        description="d",
    )


class TestClassifyFinding:
    def test_matches_within_tolerance_boundary(self) -> None:
        labels = _labels(expected=[_expected(line_start=10, line_end=10)])
        comment = _comment(line=13)  # 10 + tolerance(3) == 13

        assert classify_finding(comment, labels) == "true_positive"

    def test_does_not_match_just_outside_tolerance(self) -> None:
        labels = _labels(expected=[_expected(line_start=10, line_end=10)])
        comment = _comment(line=14)  # 10 + 3 + 1

        assert classify_finding(comment, labels) == "unjudged"

    def test_matches_within_tolerance_lower_boundary(self) -> None:
        labels = _labels(expected=[_expected(line_start=10, line_end=10)])
        comment = _comment(line=7)  # 10 - tolerance(3) == 7

        assert classify_finding(comment, labels) == "true_positive"

    def test_does_not_match_just_outside_lower_tolerance(self) -> None:
        labels = _labels(expected=[_expected(line_start=10, line_end=10)])
        comment = _comment(line=6)  # 10 - 3 - 1

        assert classify_finding(comment, labels) == "unjudged"

    def test_expected_match_takes_priority_over_judgments(self) -> None:
        # judgments가 false_positive라고 적혀 있어도 expected 매칭이 우선한다.
        labels = _labels(
            expected=[_expected(line_start=10, line_end=10)],
            judgments={"a.py:10:t": "false_positive"},
        )
        comment = _comment(line=10, title="t")

        assert classify_finding(comment, labels) == "true_positive"

    def test_falls_back_to_judgment_valid(self) -> None:
        labels = _labels(judgments={"a.py:10:t": "valid"})
        comment = _comment(line=10, title="t")

        assert classify_finding(comment, labels) == "true_positive"

    def test_falls_back_to_judgment_false_positive(self) -> None:
        labels = _labels(judgments={"a.py:10:t": "false_positive"})
        comment = _comment(line=10, title="t")

        assert classify_finding(comment, labels) == "false_positive"

    def test_unjudged_when_no_expected_and_no_judgment(self) -> None:
        labels = _labels()
        comment = _comment(line=10, title="t")

        assert classify_finding(comment, labels) == "unjudged"

    def test_different_file_path_does_not_match_expected(self) -> None:
        labels = _labels(expected=[_expected(file_path="b.py", line_start=10, line_end=10)])
        comment = _comment(file_path="a.py", line=10)

        assert classify_finding(comment, labels) == "unjudged"


class TestComputeMetrics:
    def test_empty_results_returns_empty_metrics(self) -> None:
        assert compute_metrics([], {}) == {}

    def test_unjudged_counts_toward_false_positive_rate_but_tracked_separately(self) -> None:
        labels = _labels(case_id="c1")
        completed = ReviewCompletedEvent(
            review_job_id="j",
            repository_id=1,
            pr_number=1,
            head_sha="h",
            summary="s",
            reviews=[_comment(line=10, title="t")],
            model_version="m",
            prompt_version="p1",
        )
        result = CaseRunResult(case_id="c1", condition="A", completed=completed)

        metrics = compute_metrics([result], {"c1": labels})

        m = metrics["A"]
        assert m.finding_count == 1
        assert m.unjudged_count == 1
        assert m.false_positive_count == 0
        assert m.false_positive_rate == 1.0

    def test_recall_and_true_positive_computed_from_expected_match(self) -> None:
        labels = _labels(
            case_id="c1",
            expected=[
                _expected(id="e1", line_start=10, line_end=10),
                _expected(id="e2", line_start=50, line_end=50),
            ],
        )
        completed = ReviewCompletedEvent(
            review_job_id="j",
            repository_id=1,
            pr_number=1,
            head_sha="h",
            summary="s",
            reviews=[_comment(line=10, title="t")],
            model_version="m",
            prompt_version="p1",
        )
        result = CaseRunResult(case_id="c1", condition="B", completed=completed)

        metrics = compute_metrics([result], {"c1": labels})

        m = metrics["B"]
        assert m.true_positive_count == 1
        assert m.recall == 0.5  # e1만 매칭, e2는 못 잡음 -> 1/2

    def test_critical_major_false_positive_count(self) -> None:
        labels = _labels(case_id="c1")
        completed = ReviewCompletedEvent(
            review_job_id="j",
            repository_id=1,
            pr_number=1,
            head_sha="h",
            summary="s",
            reviews=[
                _comment(line=10, title="t1", severity="critical"),
                _comment(line=20, title="t2", severity="minor"),
            ],
            model_version="m",
            prompt_version="p1",
        )
        result = CaseRunResult(case_id="c1", condition="A", completed=completed)

        metrics = compute_metrics([result], {"c1": labels})

        m = metrics["A"]
        assert m.critical_major_false_positive_count == 1

    def test_failed_cases_are_aggregated_by_reason(self) -> None:
        failed1 = CaseRunResult(
            case_id="c1",
            condition="A",
            failed=ReviewFailedEvent(review_job_id="j1", head_sha="h", reason="timeout"),
        )
        failed2 = CaseRunResult(
            case_id="c2",
            condition="A",
            failed=ReviewFailedEvent(review_job_id="j2", head_sha="h", reason="timeout"),
        )
        failed3 = CaseRunResult(
            case_id="c3",
            condition="A",
            failed=ReviewFailedEvent(review_job_id="j3", head_sha="h", reason="parse_error"),
        )

        metrics = compute_metrics([failed1, failed2, failed3], {})

        m = metrics["A"]
        assert m.case_count == 3
        assert m.failure_count == 3
        assert m.failures_by_reason == {"timeout": 2, "parse_error": 1}
        assert m.finding_count == 0
        assert m.false_positive_rate is None
        assert m.recall is None

    def test_evidence_metrics_only_populated_for_condition_c(self) -> None:
        labels = _labels(case_id="c1")
        completed = ReviewCompletedEvent(
            review_job_id="j",
            repository_id=1,
            pr_number=1,
            head_sha="h",
            summary="s",
            reviews=[],
            model_version="m",
            prompt_version="p1",
        )
        result_a = CaseRunResult(
            case_id="c1",
            condition="A",
            completed=completed,
            evidence_in_prompt=True,
            changed_packages=["axios"],
        )
        result_c = CaseRunResult(
            case_id="c1",
            condition="C",
            completed=completed,
            evidence_in_prompt=True,
            evidence_latency_ms=100.0,
            cache_hits=1,
            cache_misses=1,
            changed_packages=["axios"],
        )

        metrics = compute_metrics([result_a, result_c], {"c1": labels})

        assert metrics["A"].evidence_prompt_rate is None
        assert metrics["A"].cache_hit_rate is None
        assert metrics["C"].evidence_prompt_rate == 1.0
        assert metrics["C"].cache_hit_rate == 0.5
        assert metrics["C"].evidence_latency_p50_ms == 100.0
        assert metrics["C"].evidence_latency_p95_ms == 100.0

    def test_evidence_linked_finding_rate(self) -> None:
        labels = _labels(case_id="c1")
        completed = ReviewCompletedEvent(
            review_job_id="j",
            repository_id=1,
            pr_number=1,
            head_sha="h",
            summary="s",
            reviews=[
                _comment(line=1, title="axios 관련 이슈", message="axios 업그레이드 문제"),
                _comment(line=2, title="다른 이슈", message="관련 없음"),
            ],
            model_version="m",
            prompt_version="p1",
        )
        result = CaseRunResult(
            case_id="c1", condition="C", completed=completed, changed_packages=["axios"]
        )

        metrics = compute_metrics([result], {"c1": labels})

        assert metrics["C"].evidence_linked_finding_rate == 0.5

    def test_evidence_prompt_rate_denominator_excludes_cases_without_changed_packages(
        self,
    ) -> None:
        labels = _labels(case_id="c1")
        completed = ReviewCompletedEvent(
            review_job_id="j",
            repository_id=1,
            pr_number=1,
            head_sha="h",
            summary="s",
            reviews=[],
            model_version="m",
            prompt_version="p1",
        )
        result = CaseRunResult(
            case_id="c1", condition="C", completed=completed, changed_packages=[]
        )

        metrics = compute_metrics([result], {"c1": labels})

        assert metrics["C"].evidence_prompt_rate is None


class TestPendingJudgments:
    def test_only_lists_unjudged_findings(self) -> None:
        labels = _labels(
            case_id="c1",
            expected=[_expected(line_start=10, line_end=10)],
            judgments={"a.py:20:known-fp": "false_positive"},
        )
        completed = ReviewCompletedEvent(
            review_job_id="j",
            repository_id=1,
            pr_number=1,
            head_sha="h",
            summary="s",
            reviews=[
                _comment(line=10, title="matches-expected"),
                _comment(line=20, title="known-fp"),
                _comment(line=30, title="needs-judgment"),
            ],
            model_version="m",
            prompt_version="p1",
        )
        result = CaseRunResult(case_id="c1", condition="A", completed=completed)

        pending = pending_judgments([result], {"c1": labels})

        assert len(pending) == 1
        assert pending[0]["title"] == "needs-judgment"
        assert pending[0]["caseId"] == "c1"
        assert pending[0]["condition"] == "A"

    def test_failed_case_produces_no_pending_judgments(self) -> None:
        result = CaseRunResult(
            case_id="c1",
            condition="A",
            failed=ReviewFailedEvent(review_job_id="j", head_sha="h", reason="timeout"),
        )

        assert pending_judgments([result], {}) == []


class TestRenderMarkdown:
    def test_renders_rows_in_a_b_c_order_and_none_as_dash(self) -> None:
        labels = _labels(case_id="c1")
        completed = ReviewCompletedEvent(
            review_job_id="j",
            repository_id=1,
            pr_number=1,
            head_sha="h",
            summary="s",
            reviews=[],
            model_version="m",
            prompt_version="p1",
        )
        results = [
            CaseRunResult(case_id="c1", condition="C", completed=completed),
            CaseRunResult(case_id="c1", condition="A", completed=completed),
        ]
        metrics = compute_metrics(results, {"c1": labels})

        markdown = render_markdown(metrics)

        lines = markdown.splitlines()
        # 헤더 다음 첫 데이터 행이 A, 그다음이 C여야 한다.
        assert lines[2].startswith("| A |")
        assert lines[3].startswith("| C |")
        assert "-" in markdown  # false_positive_rate 등 분모 0인 필드가 "-"로 표기됨
