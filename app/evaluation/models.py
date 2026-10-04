from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_ReviewsJson = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class ReviewJobRow(Base):
    __tablename__ = "review_jobs"

    review_job_id: Mapped[str] = mapped_column(String, primary_key=True)
    repository_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    fail_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReviewRecordRow(Base):
    __tablename__ = "review_records"

    review_job_id: Mapped[str] = mapped_column(
        String, ForeignKey("review_jobs.review_job_id"), primary_key=True
    )
    reviews: Mapped[list[dict[str, object]]] = mapped_column(_ReviewsJson, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)


class ReviewFeedbackRow(Base):
    __tablename__ = "review_feedback"
    __table_args__ = (
        UniqueConstraint(
            "review_job_id", "finding_index", name="uq_review_feedback_job_finding"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_job_id: Mapped[str] = mapped_column(
        String, ForeignKey("review_jobs.review_job_id"), nullable=False
    )
    finding_index: Mapped[int] = mapped_column(Integer, nullable=False)
    reflected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReviewLineCheckRow(Base):
    """finding 줄 번호가 diff 안에 있었는지 측정한 결과(이슈 #122).

    review_jobs를 참조하지 않는다 — 파이프라인 실행 중에 기록되고, review_jobs 행은
    그 뒤 consumer가 결과를 저장할 때 만들어지므로 FK를 걸면 항상 위반된다.
    """

    __tablename__ = "review_line_checks"

    review_job_id: Mapped[str] = mapped_column(String, primary_key=True)
    annotated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    llm_ok: Mapped[int] = mapped_column(Integer, nullable=False)
    llm_line_not_in_diff: Mapped[int] = mapped_column(Integer, nullable=False)
    llm_file_not_in_diff: Mapped[int] = mapped_column(Integer, nullable=False)
    final_ok: Mapped[int] = mapped_column(Integer, nullable=False)
    final_line_not_in_diff: Mapped[int] = mapped_column(Integer, nullable=False)
    final_file_not_in_diff: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReviewTimingRow(Base):
    """리뷰 한 건의 단계별 소요 시간(이슈 #134). 병목을 확인한 뒤 캐시·추론 설정을 정하는 근거다.

    review_line_checks와 같은 이유로 review_jobs를 참조하지 않는다.
    """

    __tablename__ = "review_timings"

    review_job_id: Mapped[str] = mapped_column(String, primary_key=True)
    total_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    prep_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    generate_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    summary_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    verify_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    batches: Mapped[int] = mapped_column(Integer, nullable=False)
    targets: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_chars: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
