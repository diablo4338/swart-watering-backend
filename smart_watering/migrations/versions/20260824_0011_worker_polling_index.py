"""add worker polling index

Revision ID: 20260824_0011
Revises: 20260823_0010
"""
from alembic import op


revision = "20260824_0011"
down_revision = "20260823_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_operations_status_updated_at",
        "operations",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_operations_status_updated_at", table_name="operations")
