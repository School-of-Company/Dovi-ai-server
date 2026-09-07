"""create evaluation tables

Revision ID: 0001
Revises:
Create Date: 2026-09-07

"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_jobs",
        sa.Column("review_job_id", sa.String(), primary_key=True),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("fail_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "review_records",
        sa.Column(
            "review_job_id",
            sa.String(),
            sa.ForeignKey("review_jobs.review_job_id"),
            primary_key=True,
        ),
        sa.Column("reviews", JSONB(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("model_version", sa.String(), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=False),
    )
    op.create_table(
        "review_feedback",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "review_job_id",
            sa.String(),
            sa.ForeignKey("review_jobs.review_job_id"),
            nullable=False,
        ),
        sa.Column("finding_index", sa.Integer(), nullable=False),
        sa.Column("reflected", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "review_job_id", "finding_index", name="uq_review_feedback_job_finding"
        ),
    )


def downgrade() -> None:
    op.drop_table("review_feedback")
    op.drop_table("review_records")
    op.drop_table("review_jobs")
