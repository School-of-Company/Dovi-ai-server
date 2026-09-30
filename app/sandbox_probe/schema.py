from typing import Literal

from app.review.schema import CamelModel

ProbeStatus = Literal["passed", "found_issue", "inconclusive"]
ProbeName = Literal["init_order", "lifecycle", "build"]

MAX_EVIDENCE_BYTES = 8 * 1024
MAX_FINDING_EVIDENCE_BYTES = 4 * 1024
MAX_FINDINGS = 10
MAX_EVENT_BYTES = 512 * 1024

_TRUNCATION_MARKER = "...(앞부분 생략)\n"


class SandboxProbeRequestedEvent(CamelModel):
    review_job_id: str
    repository_id: int
    installation_id: int
    repo_full_name: str
    pr_number: int
    head_sha: str
    base_sha: str


class Finding(CamelModel):
    probe: ProbeName
    title: str
    message: str
    file_path: str | None = None
    line: int | None = None
    evidence: str


class SandboxProbeCompletedEvent(CamelModel):
    review_job_id: str
    repository_id: int
    pr_number: int
    head_sha: str
    status: ProbeStatus
    evidence: str = ""
    findings: list[Finding] = []


def keep_tail(text: str, max_bytes: int) -> str:
    # 빌드 에러는 보통 출력 끝에 나오므로 초과 시 앞이 아니라 뒤를 남긴다.
    raw = text.encode()
    if len(raw) <= max_bytes:
        return text
    budget = max_bytes - len(_TRUNCATION_MARKER.encode())
    tail = raw[-budget:].decode(errors="ignore")
    return _TRUNCATION_MARKER + tail


def _serialized_size(event: SandboxProbeCompletedEvent) -> int:
    return len(event.model_dump_json(by_alias=True).encode())


def cap_completed_event(event: SandboxProbeCompletedEvent) -> SandboxProbeCompletedEvent:
    findings = [
        f.model_copy(update={"evidence": keep_tail(f.evidence, MAX_FINDING_EVIDENCE_BYTES)})
        for f in event.findings[:MAX_FINDINGS]
    ]
    capped = event.model_copy(
        update={
            "evidence": keep_tail(event.evidence, MAX_EVIDENCE_BYTES),
            "findings": findings,
        }
    )
    while _serialized_size(capped) > MAX_EVENT_BYTES and capped.findings:
        capped = capped.model_copy(update={"findings": capped.findings[:-1]})
    return capped
