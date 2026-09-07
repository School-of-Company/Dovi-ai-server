from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.evaluation.models import Base, ReviewFeedbackRow, ReviewJobRow, ReviewRecordRow
from app.evaluation.repository import SqlAlchemyEvaluationRepository
from app.evaluation.schema import ReviewFeedbackEvent
from app.review.schema import ReviewComment, ReviewCompletedEvent, ReviewFailedEvent


@pytest.fixture
async def repo_and_sessions() -> AsyncGenerator[
    tuple[SqlAlchemyEvaluationRepository, async_sessionmaker[AsyncSession]]
]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    # SQLite는 기본적으로 FK 제약을 강제하지 않는다 — Postgres와 동일하게
    # FK 위반이 실제로 에러를 내는지 검증하려면 명시적으로 켜야 한다.
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_connection: object, connection_record: object) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield SqlAlchemyEvaluationRepository(session_factory), session_factory
    await engine.dispose()


def _completed_event() -> ReviewCompletedEvent:
    return ReviewCompletedEvent(
        review_job_id="123:45:abcabc",
        repository_id=123,
        pr_number=45,
        head_sha="abcabc",
        summary="ok",
        reviews=[
            ReviewComment(
                severity="minor",
                confidence=1.0,
                file_path="a.py",
                line=1,
                title="t",
                message="m",
                evidence=["e"],
            )
        ],
        model_version="qwen2.5-coder-32b",
        prompt_version="v1",
    )


async def test_save_completed_persists_job_and_record(
    repo_and_sessions: tuple[SqlAlchemyEvaluationRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repo, session_factory = repo_and_sessions

    await repo.save_completed(_completed_event())

    async with session_factory() as session:
        job = await session.get(ReviewJobRow, "123:45:abcabc")
        record = await session.get(ReviewRecordRow, "123:45:abcabc")
    assert job is not None
    assert job.status == "completed"
    assert job.repository_id == 123
    assert record is not None
    assert record.summary == "ok"
    assert record.model_version == "qwen2.5-coder-32b"
    assert len(record.reviews) == 1
    assert record.reviews[0]["severity"] == "minor"


async def test_save_completed_is_idempotent(
    repo_and_sessions: tuple[SqlAlchemyEvaluationRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repo, session_factory = repo_and_sessions
    event_ = _completed_event()

    await repo.save_completed(event_)
    await repo.save_completed(event_)

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ReviewJobRow).where(
                        ReviewJobRow.review_job_id == "123:45:abcabc"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


async def test_save_failed_parses_review_job_id(
    repo_and_sessions: tuple[SqlAlchemyEvaluationRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repo, session_factory = repo_and_sessions
    failed = ReviewFailedEvent(review_job_id="123:45:abcabc", head_sha="abcabc", reason="timeout")

    await repo.save_failed(failed)

    async with session_factory() as session:
        job = await session.get(ReviewJobRow, "123:45:abcabc")
    assert job is not None
    assert job.status == "failed"
    assert job.fail_reason == "timeout"
    assert job.repository_id == 123
    assert job.pr_number == 45


async def test_upsert_feedback_creates_then_updates(
    repo_and_sessions: tuple[SqlAlchemyEvaluationRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repo, session_factory = repo_and_sessions
    await repo.save_completed(_completed_event())

    await repo.upsert_feedback(
        ReviewFeedbackEvent(
            review_job_id="123:45:abcabc", finding_index=0, reflected=True, reason="applied"
        )
    )
    await repo.upsert_feedback(
        ReviewFeedbackEvent(
            review_job_id="123:45:abcabc", finding_index=0, reflected=False, reason="reverted"
        )
    )

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ReviewFeedbackRow).where(
                        ReviewFeedbackRow.review_job_id == "123:45:abcabc"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].reflected is False
    assert rows[0].reason == "reverted"


async def test_upsert_feedback_unknown_review_job_id_does_not_raise(
    repo_and_sessions: tuple[SqlAlchemyEvaluationRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repo, session_factory = repo_and_sessions

    await repo.upsert_feedback(
        ReviewFeedbackEvent(
            review_job_id="does-not-exist", finding_index=0, reflected=True, reason=None
        )
    )

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ReviewFeedbackRow).where(
                        ReviewFeedbackRow.review_job_id == "does-not-exist"
                    )
                )
            )
            .scalars()
            .all()
        )
    # 예외를 던지지 않을 뿐 아니라, FK 위반으로 실제로 아무 행도 남지 않아야 한다
    # (트랜잭션이 롤백됐는지 확인 — 예외 미발생만으로는 부분 커밋 여부를 못 잡는다)
    assert rows == []
