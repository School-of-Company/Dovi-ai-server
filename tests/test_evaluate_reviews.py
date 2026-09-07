from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.evaluation.models import Base, ReviewFeedbackRow, ReviewJobRow, ReviewRecordRow
from scripts.evaluate_reviews import build_report


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as s:
        yield s
    await engine.dispose()


async def _seed(
    session: AsyncSession,
    *,
    job_id: str,
    severity: str,
    reflected: bool,
    model_version: str = "m1",
    prompt_version: str = "p1",
    finding_index: int = 0,
) -> None:
    now = datetime.now(UTC)
    session.add(
        ReviewJobRow(
            review_job_id=job_id,
            repository_id=1,
            pr_number=1,
            head_sha="sha",
            status="completed",
            fail_reason=None,
            created_at=now,
        )
    )
    session.add(
        ReviewRecordRow(
            review_job_id=job_id,
            reviews=[{"severity": severity}],
            summary="s",
            model_version=model_version,
            prompt_version=prompt_version,
        )
    )
    session.add(
        ReviewFeedbackRow(
            review_job_id=job_id,
            finding_index=finding_index,
            reflected=reflected,
            reason=None,
            updated_at=now,
        )
    )
    await session.commit()


async def _seed_record_without_feedback(
    session: AsyncSession, *, job_id: str, finding_count: int
) -> None:
    """피드백이 아직 안 붙은 리뷰 레코드 — INNER JOIN에는 안 잡히지만
    total_finding_count(평가 커버리지의 분모)에는 잡혀야 한다."""
    session.add(
        ReviewJobRow(
            review_job_id=job_id,
            repository_id=1,
            pr_number=1,
            head_sha="sha",
            status="completed",
            fail_reason=None,
            created_at=datetime.now(UTC),
        )
    )
    session.add(
        ReviewRecordRow(
            review_job_id=job_id,
            reviews=[{"severity": "minor"} for _ in range(finding_count)],
            summary="s",
            model_version="m1",
            prompt_version="p1",
        )
    )
    await session.commit()


async def test_build_report_computes_acceptance_rates(session: AsyncSession) -> None:
    await _seed(session, job_id="1", severity="critical", reflected=True)
    await _seed(session, job_id="2", severity="critical", reflected=False)
    await _seed(session, job_id="3", severity="minor", reflected=True)

    report = await build_report(session)

    assert report["total_feedback_count"] == 3
    assert report["total_finding_count"] == 3
    assert report["evaluated_ratio"] == pytest.approx(1.0)
    assert report["overall_acceptance_rate"] == pytest.approx(2 / 3)
    by_severity = report["by_severity"]
    assert isinstance(by_severity, dict)
    assert by_severity["critical"] == pytest.approx(0.5)
    assert by_severity["minor"] == pytest.approx(1.0)
    by_model_prompt_version = report["by_model_prompt_version"]
    assert isinstance(by_model_prompt_version, dict)
    assert by_model_prompt_version["m1::p1"] == pytest.approx(2 / 3)


async def test_build_report_empty_db_returns_none_rate(session: AsyncSession) -> None:
    report = await build_report(session)

    assert report["total_feedback_count"] == 0
    assert report["total_finding_count"] == 0
    assert report["evaluated_ratio"] is None
    assert report["overall_acceptance_rate"] is None
    assert report["by_severity"] == {}
    assert report["by_model_prompt_version"] == {}


async def test_build_report_skips_out_of_range_finding_index(session: AsyncSession) -> None:
    await _seed(session, job_id="1", severity="critical", reflected=True, finding_index=1)

    report = await build_report(session)

    assert report["total_feedback_count"] == 0
    assert report["overall_acceptance_rate"] is None
    assert report["by_severity"] == {}
    assert report["by_model_prompt_version"] == {}


async def test_build_report_skips_negative_finding_index(session: AsyncSession) -> None:
    # 음수 인덱스는 파이썬 리스트에서 뒤에서부터 조용히 읽히므로, 잘못된 finding과
    # 피드백이 짝지어지지 않도록 범위 밖으로 취급해 스킵해야 한다.
    await _seed(session, job_id="1", severity="critical", reflected=True, finding_index=-1)

    report = await build_report(session)

    assert report["total_feedback_count"] == 0
    assert report["by_severity"] == {}


async def test_build_report_counts_findings_without_feedback_in_coverage(
    session: AsyncSession,
) -> None:
    # 피드백이 붙은 finding 1개 + 피드백이 전혀 없는 finding 3개
    await _seed(session, job_id="1", severity="critical", reflected=True)
    await _seed_record_without_feedback(session, job_id="2", finding_count=3)

    report = await build_report(session)

    assert report["total_feedback_count"] == 1
    assert report["total_finding_count"] == 4
    assert report["evaluated_ratio"] == pytest.approx(0.25)
    # 반영률 자체는 평가된 finding만으로 계산된다(커버리지와 분리된 지표).
    assert report["overall_acceptance_rate"] == pytest.approx(1.0)
