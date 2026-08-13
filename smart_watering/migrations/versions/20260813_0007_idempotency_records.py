"""persist HTTP idempotency responses

Revision ID: 20260813_0007
Revises: 20260810_0006
"""
from alembic import op
import sqlalchemy as sa


revision = "20260813_0007"
down_revision = "20260810_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idempotency_records",
        sa.Column("scope_key", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.String(), nullable=True),
        sa.Column("content_type", sa.String(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("scope_key", "idempotency_key"),
    )


def downgrade() -> None:
    op.drop_table("idempotency_records")
