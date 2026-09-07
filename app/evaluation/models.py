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
    reviews: Mapped[list[dict]] = mapped_column(_ReviewsJson, nullable=False)
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
