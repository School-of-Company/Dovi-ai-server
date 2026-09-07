import logging
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.evaluation.models import ReviewFeedbackRow, ReviewJobRow, ReviewRecordRow
from app.evaluation.schema import ReviewFeedbackEvent
from app.review.schema import ReviewCompletedEvent, ReviewFailedEvent

logger = logging.getLogger(__name__)


class EvaluationRepository(Protocol):
    async def save_completed(self, event: ReviewCompletedEvent) -> None: ...

    async def save_failed(self, event: ReviewFailedEvent) -> None: ...

    async def upsert_feedback(self, feedback: ReviewFeedbackEvent) -> None: ...


def _parse_review_job_id(review_job_id: str) -> tuple[int, int, str]:
    """reviewJobId는 "repositoryId:prNumber:headSha" 형태다
    (app/review/schema.py의 make_review_job_id 참고). ReviewFailedEvent에는
    repository_id/pr_number 필드가 없어 이 문자열에서 역으로 파싱해야 한다."""
    repository_id_str, pr_number_str, head_sha = review_job_id.split(":", 2)
    return int(repository_id_str), int(pr_number_str), head_sha


class SqlAlchemyEvaluationRepository:
    """review_jobs/review_records/review_feedback을 관리한다.

    모든 쓰기는 get-then-write 방식이다(DB 네이티브 ON CONFLICT 대신) —
    Postgres/SQLite 양쪽에서 동일하게 동작해야 하고, 이 서비스는 같은
    reviewJobId가 동시에 두 번 쓰이는 경쟁 상황이 없다(Kafka consumer가
    reviewJobId당 dedup으로 직렬 처리).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save_completed(self, event: ReviewCompletedEvent) -> None:
        async with self._session_factory() as session, session.begin():
            job = await session.get(ReviewJobRow, event.review_job_id)
            if job is None:
                session.add(
                    ReviewJobRow(
                        review_job_id=event.review_job_id,
                        repository_id=event.repository_id,
                        pr_number=event.pr_number,
                        head_sha=event.head_sha,
                        status="completed",
                        fail_reason=None,
                        created_at=datetime.now(UTC),
                    )
                )
            else:
                job.status = "completed"
                job.fail_reason = None

            reviews_payload = [review.model_dump(by_alias=True) for review in event.reviews]
            record = await session.get(ReviewRecordRow, event.review_job_id)
            if record is None:
                session.add(
                    ReviewRecordRow(
                        review_job_id=event.review_job_id,
                        reviews=reviews_payload,
                        summary=event.summary,
                        model_version=event.model_version,
                        prompt_version=event.prompt_version,
                    )
                )
            else:
                record.reviews = reviews_payload
                record.summary = event.summary
                record.model_version = event.model_version
                record.prompt_version = event.prompt_version

    async def save_failed(self, event: ReviewFailedEvent) -> None:
        repository_id, pr_number, head_sha = _parse_review_job_id(event.review_job_id)
        async with self._session_factory() as session, session.begin():
            job = await session.get(ReviewJobRow, event.review_job_id)
            if job is None:
                session.add(
                    ReviewJobRow(
                        review_job_id=event.review_job_id,
                        repository_id=repository_id,
                        pr_number=pr_number,
                        head_sha=head_sha,
                        status="failed",
                        fail_reason=event.reason,
                        created_at=datetime.now(UTC),
                    )
                )
            else:
                job.status = "failed"
                job.fail_reason = event.reason

    async def upsert_feedback(self, feedback: ReviewFeedbackEvent) -> None:
        # ReviewFeedbackRow의 PK는 자동증가 id뿐이라 (review_job_id,
        # finding_index) 복합 유니크 키로 조회하려면 select()가 필요하다
        # (session.get()은 PK 조회 전용이라 여기 못 쓴다).
        try:
            async with self._session_factory() as session, session.begin():
                existing = await session.scalar(
                    select(ReviewFeedbackRow).where(
                        ReviewFeedbackRow.review_job_id == feedback.review_job_id,
                        ReviewFeedbackRow.finding_index == feedback.finding_index,
                    )
                )
                if existing is None:
                    session.add(
                        ReviewFeedbackRow(
                            review_job_id=feedback.review_job_id,
                            finding_index=feedback.finding_index,
                            reflected=feedback.reflected,
                            reason=feedback.reason,
                            updated_at=datetime.now(UTC),
                        )
                    )
                else:
                    existing.reflected = feedback.reflected
                    existing.reason = feedback.reason
                    existing.updated_at = datetime.now(UTC)
        except IntegrityError:
            logger.exception(
                "failed to upsert review_feedback (unknown reviewJobId=%s?)",
                feedback.review_job_id,
            )
