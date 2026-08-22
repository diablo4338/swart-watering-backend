"""separate backend device name from controller name

Revision ID: 20260822_0009
Revises: 20260818_0008
"""
import sqlalchemy as sa
from alembic import op


revision = "20260822_0009"
down_revision = "20260818_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("controller_name", sa.String(), nullable=True))
    op.execute("UPDATE devices SET controller_name = name")
    with op.batch_alter_table("devices") as batch_op:
        batch_op.alter_column("controller_name", existing_type=sa.String(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("devices") as batch_op:
        batch_op.drop_column("controller_name")
