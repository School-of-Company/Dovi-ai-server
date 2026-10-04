"""create review_timings

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-04

"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_timings",
        sa.Column("review_job_id", sa.String(), primary_key=True),
        sa.Column("total_ms", sa.Integer(), nullable=False),
        sa.Column("prep_ms", sa.Integer(), nullable=False),
        sa.Column("generate_ms", sa.Integer(), nullable=False),
        sa.Column("summary_ms", sa.Integer(), nullable=False),
        sa.Column("verify_ms", sa.Integer(), nullable=False),
        sa.Column("batches", sa.Integer(), nullable=False),
        sa.Column("targets", sa.Integer(), nullable=False),
        sa.Column("prompt_chars", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("review_timings")
