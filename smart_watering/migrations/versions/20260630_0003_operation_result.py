"""operation result

Revision ID: 20260630_0003
Revises: 20260629_0002
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa


revision = "20260630_0003"
down_revision = "20260629_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("operations", sa.Column("result_json", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("operations", "result_json")
