import json

from app.sandbox_probe.schema import (
    MAX_EVENT_BYTES,
    MAX_EVIDENCE_BYTES,
    MAX_FINDING_EVIDENCE_BYTES,
    MAX_FINDINGS,
    Finding,
    SandboxProbeCompletedEvent,
    SandboxProbeRequestedEvent,
    cap_completed_event,
    keep_tail,
)


def _finding(evidence: str = "e", **overrides: object) -> Finding:
    data: dict[str, object] = {
        "probe": "init_order",
        "title": "t",
        "message": "m",
        "evidence": evidence,
    }
    data.update(overrides)
    return Finding.model_validate(data)


def _completed(**overrides: object) -> SandboxProbeCompletedEvent:
    data: dict[str, object] = {
        "review_job_id": "1:2:abc",
        "repository_id": 1,
        "pr_number": 2,
        "head_sha": "abc",
        "status": "found_issue",
    }
    data.update(overrides)
    return SandboxProbeCompletedEvent.model_validate(data)


def test_requested_event_parses_camel_case_payload() -> None:
    raw = json.dumps(
        {
            "reviewJobId": "1:2:abc",
            "repositoryId": 1,
            "installationId": 99,
            "repoFullName": "org/repo",
            "prNumber": 2,
            "headSha": "abc",
            "baseSha": "def",
        }
    )

    event = SandboxProbeRequestedEvent.model_validate_json(raw)

    assert event.installation_id == 99
    assert event.repo_full_name == "org/repo"


def test_completed_event_serializes_camel_case_and_round_trips() -> None:
    event = _completed(findings=[_finding(file_path="src/a.ts", line=None)])

    dumped = json.loads(event.model_dump_json(by_alias=True))

    assert dumped["reviewJobId"] == "1:2:abc"
    assert dumped["findings"][0]["filePath"] == "src/a.ts"
    assert dumped["findings"][0]["line"] is None
    assert SandboxProbeCompletedEvent.model_validate(dumped) == event


def test_keep_tail_leaves_short_text_untouched() -> None:
    assert keep_tail("short", 100) == "short"


def test_keep_tail_preserves_the_end_within_byte_limit() -> None:
    text = "A" * 500 + "ROOT CAUSE"

    result = keep_tail(text, 100)

    assert result.endswith("ROOT CAUSE")
    assert len(result.encode()) <= 100


def test_keep_tail_never_splits_multibyte_characters() -> None:
    result = keep_tail("가" * 200, 100)

    assert len(result.encode()) <= 100
    assert result.endswith("가")


def test_cap_truncates_event_and_finding_evidence() -> None:
    event = _completed(
        evidence="x" * (MAX_EVIDENCE_BYTES * 2),
        findings=[_finding(evidence="y" * (MAX_FINDING_EVIDENCE_BYTES * 2))],
    )

    capped = cap_completed_event(event)

    assert len(capped.evidence.encode()) <= MAX_EVIDENCE_BYTES
    assert len(capped.findings[0].evidence.encode()) <= MAX_FINDING_EVIDENCE_BYTES


def test_cap_limits_number_of_findings() -> None:
    event = _completed(findings=[_finding() for _ in range(MAX_FINDINGS + 5)])

    assert len(cap_completed_event(event).findings) == MAX_FINDINGS


def test_cap_keeps_events_within_limits_unchanged() -> None:
    event = _completed(evidence="ok", findings=[_finding()])

    assert cap_completed_event(event) == event


def test_cap_drops_findings_when_event_still_exceeds_size_limit() -> None:
    huge_message = "m" * (MAX_EVENT_BYTES // 2)
    event = _completed(
        findings=[_finding(message=huge_message), _finding(message=huge_message)]
    )

    capped = cap_completed_event(event)

    assert len(capped.model_dump_json(by_alias=True).encode()) <= MAX_EVENT_BYTES
    assert len(capped.findings) == 1
