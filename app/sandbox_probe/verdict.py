from dataclasses import dataclass
from typing import Literal

from app.sandbox_probe.schema import Finding, ProbeName, ProbeStatus

OutcomeStatus = Literal["passed", "found_issue", "skip", "inconclusive"]


@dataclass(frozen=True)
class ProbeOutcome:
    probe: ProbeName
    status: OutcomeStatus
    findings: tuple[Finding, ...] = ()
    evidence: str = ""


@dataclass(frozen=True)
class StageResult:
    ok: bool
    evidence: str = ""


@dataclass(frozen=True)
class AggregateResult:
    status: ProbeStatus
    evidence: str
    findings: tuple[Finding, ...]


def aggregate(
    install: StageResult,
    build: StageResult | None,
    init_order: ProbeOutcome | None,
    lifecycle: ProbeOutcome | None,
) -> AggregateResult:
    if not install.ok:
        return AggregateResult("inconclusive", install.evidence, ())

    if build is None or not build.ok:
        evidence = build.evidence if build is not None else ""
        finding = Finding(
            probe="build",
            title="빌드 실패",
            message="PR head 커밋을 빌드하지 못했습니다.",
            evidence=evidence,
        )
        return AggregateResult("found_issue", evidence, (finding,))

    outcomes = [o for o in (init_order, lifecycle) if o is not None]
    findings = tuple(f for o in outcomes for f in o.findings)
    evidence = "\n\n".join(f"[{o.probe}] {o.evidence}" for o in outcomes if o.evidence)

    statuses = {o.status for o in outcomes}
    if "found_issue" in statuses:
        return AggregateResult("found_issue", evidence, findings)
    # 환경 기인 실패일 수 있는 기동 불가는 통과로 단정하지 않는다(오탐·오통과 방지).
    if "inconclusive" in statuses:
        return AggregateResult("inconclusive", evidence, findings)
    if "passed" in statuses:
        return AggregateResult("passed", evidence, findings)
    return AggregateResult("inconclusive", evidence, findings)
