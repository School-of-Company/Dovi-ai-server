from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.evaluation.models import Base, ReviewLineCheckRow
from app.evaluation.repository import SqlAlchemyEvaluationRepository
from app.review.diff_lines import LineCheckRecord


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


def _record(
    review_job_id: str = "123:45:abcabc", *, annotated: bool = False, ok: int = 3
) -> LineCheckRecord:
    return LineCheckRecord(
        review_job_id=review_job_id,
        annotated=annotated,
        llm={"ok": ok, "line_not_in_diff": 2, "file_not_in_diff": 1},
        final={"ok": 1, "line_not_in_diff": 0, "file_not_in_diff": 0},
    )


async def test_save_line_check_persists_counts_without_a_review_job_row(
    repo_and_sessions: tuple[SqlAlchemyEvaluationRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repo, session_factory = repo_and_sessions

    await repo.save_line_check(_record(annotated=True))

    async with session_factory() as session:
        row = await session.get(ReviewLineCheckRow, "123:45:abcabc")
    assert row is not None
    assert row.annotated is True
    assert (row.llm_ok, row.llm_line_not_in_diff, row.llm_file_not_in_diff) == (3, 2, 1)
    assert (row.final_ok, row.final_line_not_in_diff, row.final_file_not_in_diff) == (1, 0, 0)
    assert row.created_at is not None


async def test_save_line_check_overwrites_the_same_review_job(
    repo_and_sessions: tuple[SqlAlchemyEvaluationRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repo, session_factory = repo_and_sessions

    await repo.save_line_check(_record(ok=3))
    await repo.save_line_check(_record(ok=9, annotated=True))

    async with session_factory() as session:
        rows = (await session.scalars(select(ReviewLineCheckRow))).all()
    assert len(rows) == 1
    assert rows[0].llm_ok == 9
    assert rows[0].annotated is True


async def test_line_checks_for_different_jobs_are_kept_separately(
    repo_and_sessions: tuple[SqlAlchemyEvaluationRepository, async_sessionmaker[AsyncSession]],
) -> None:
    repo, session_factory = repo_and_sessions

    await repo.save_line_check(_record("1:1:a", annotated=False))
    await repo.save_line_check(_record("2:2:b", annotated=True))

    async with session_factory() as session:
        rows = (await session.scalars(select(ReviewLineCheckRow))).all()
    assert {(r.review_job_id, r.annotated) for r in rows} == {("1:1:a", False), ("2:2:b", True)}
