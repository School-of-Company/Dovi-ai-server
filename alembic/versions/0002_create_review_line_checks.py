"""create review_line_checks

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01

"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_line_checks",
        sa.Column("review_job_id", sa.String(), primary_key=True),
        sa.Column("annotated", sa.Boolean(), nullable=False),
        sa.Column("llm_ok", sa.Integer(), nullable=False),
        sa.Column("llm_line_not_in_diff", sa.Integer(), nullable=False),
        sa.Column("llm_file_not_in_diff", sa.Integer(), nullable=False),
        sa.Column("final_ok", sa.Integer(), nullable=False),
        sa.Column("final_line_not_in_diff", sa.Integer(), nullable=False),
        sa.Column("final_file_not_in_diff", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("review_line_checks")
