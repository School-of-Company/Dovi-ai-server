from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.evaluation.models import Base, ReviewTimingRow
from app.evaluation.repository import SqlAlchemyEvaluationRepository
from app.review.pipeline import ReviewPipeline
from app.review.schema import (
    ChangedFile,
    ReviewCompletedEvent,
    ReviewModelOutput,
    ReviewRequestedEvent,
    make_review_job_id,
)
from app.review.timing import ReviewTimingRecord
from tests.test_review_pipeline import FakeLLM


def _event() -> ReviewRequestedEvent:
    return ReviewRequestedEvent(
        review_job_id=make_review_job_id(42, 7, "abc123"),
        repository_id=42,
        pr_number=7,
        head_sha="abc123",
        base_sha="def456",
        changed_files=[ChangedFile(file_path="a.py", status="modified", patch="@@ -1 +1 @@\n+x")],
    )


async def test_pipeline_reports_stage_timings_to_the_sink_and_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    records: list[ReviewTimingRecord] = []

    async def sink(record: ReviewTimingRecord) -> None:
        records.append(record)

    pipeline = ReviewPipeline(
        FakeLLM(ReviewModelOutput(summary="ok", reviews=[])),
        model_version="v",
        prompt_version="v1",
        timing_sink=sink,
    )

    with caplog.at_level("INFO"):
        result = await pipeline.run(_event())

    assert isinstance(result, ReviewCompletedEvent)
    assert len(records) == 1
    record = records[0]
    assert record.review_job_id == "42:7:abc123"
    assert (record.batches, record.targets) == (1, 1)
    assert record.prompt_chars > 0
    assert record.total_ms >= record.prep_ms + record.generate_ms
    assert "review timing reviewJobId=42:7:abc123" in caplog.text


async def test_failing_timing_sink_does_not_break_the_review() -> None:
    async def sink(record: ReviewTimingRecord) -> None:
        raise RuntimeError("db down")

    pipeline = ReviewPipeline(
        FakeLLM(ReviewModelOutput(summary="ok", reviews=[])),
        model_version="v",
        prompt_version="v1",
        timing_sink=sink,
    )

    assert isinstance(await pipeline.run(_event()), ReviewCompletedEvent)


@pytest.fixture
async def repo_and_sessions() -> AsyncGenerator[
    tuple[SqlAlchemyEvaluationRepository, async_sessionmaker[AsyncSession]]
]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield SqlAlchemyEvaluationRepository(session_factory), session_factory
    await engine.dispose()


def _record(total_ms: int) -> ReviewTimingRecord:
    return ReviewTimingRecord(
        review_job_id="42:7:abc123",
        total_ms=total_ms,
        prep_ms=10,
        generate_ms=200,
        summary_ms=30,
        verify_ms=40,
        batches=2,
        targets=3,
        prompt_chars=5000,
    )


async def test_save_review_timing_persists_and_overwrites(
    repo_and_sessions: tuple[SqlAlchemyEvaluationRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repo, session_factory = repo_and_sessions

    await repo.save_review_timing(_record(300))
    await repo.save_review_timing(_record(999))

    async with session_factory() as session:
        row = await session.get(ReviewTimingRow, "42:7:abc123")
    assert row is not None
    assert row.total_ms == 999
    assert (row.prep_ms, row.generate_ms, row.summary_ms, row.verify_ms) == (10, 200, 30, 40)
    assert (row.batches, row.targets, row.prompt_chars) == (2, 3, 5000)
    assert row.created_at is not None
