from app.sandbox_probe.schema import Finding
from app.sandbox_probe.verdict import ProbeOutcome, StageResult, aggregate

_OK = StageResult(ok=True)


def _finding(probe: str = "init_order") -> Finding:
    return Finding.model_validate(
        {"probe": probe, "title": "t", "message": "m", "evidence": "e"}
    )


def _passed(probe: str = "init_order", evidence: str = "") -> ProbeOutcome:
    return ProbeOutcome(probe, "passed", evidence=evidence)  # type: ignore[arg-type]


def _issue(probe: str = "init_order") -> ProbeOutcome:
    return ProbeOutcome(
        probe,  # type: ignore[arg-type]
        "found_issue",
        findings=(_finding(probe),),
        evidence="Cannot access 'FormEntity' before initialization",
    )


def test_install_failure_is_inconclusive_and_skips_everything_else() -> None:
    result = aggregate(
        StageResult(ok=False, evidence="ERR_PNPM_LOCKFILE"), None, None, None
    )

    assert result.status == "inconclusive"
    assert result.evidence == "ERR_PNPM_LOCKFILE"
    assert result.findings == ()


def test_build_failure_is_reported_as_found_issue_and_ignores_probes() -> None:
    result = aggregate(
        _OK, StageResult(ok=False, evidence="TS2322 error"), _issue(), _passed("lifecycle")
    )

    assert result.status == "found_issue"
    assert [f.probe for f in result.findings] == ["build"]
    assert result.findings[0].evidence == "TS2322 error"


def test_all_probes_passing_is_passed() -> None:
    result = aggregate(_OK, _OK, _passed(), _passed("lifecycle"))

    assert result.status == "passed"
    assert result.findings == ()


def test_any_found_issue_makes_the_job_found_issue_and_keeps_other_evidence() -> None:
    result = aggregate(
        _OK, _OK, _issue(), _passed("lifecycle", evidence="notified on shutdown")
    )

    assert result.status == "found_issue"
    assert len(result.findings) == 1
    assert "Cannot access" in result.evidence
    assert "notified on shutdown" in result.evidence


def test_found_issue_wins_over_inconclusive() -> None:
    result = aggregate(
        _OK, _OK, _issue(), ProbeOutcome("lifecycle", "inconclusive")
    )

    assert result.status == "found_issue"


def test_inconclusive_probe_prevents_a_passed_verdict() -> None:
    result = aggregate(
        _OK, _OK, _passed(), ProbeOutcome("lifecycle", "inconclusive")
    )

    assert result.status == "inconclusive"


def test_skipped_lifecycle_probe_does_not_block_passed() -> None:
    result = aggregate(_OK, _OK, _passed(), ProbeOutcome("lifecycle", "skip"))

    assert result.status == "passed"


def test_all_probes_skipped_is_inconclusive_because_nothing_was_verified() -> None:
    result = aggregate(
        _OK, _OK, ProbeOutcome("init_order", "skip"), ProbeOutcome("lifecycle", "skip")
    )

    assert result.status == "inconclusive"


def test_missing_probe_outcomes_are_ignored() -> None:
    result = aggregate(_OK, _OK, _passed(), None)

    assert result.status == "passed"
