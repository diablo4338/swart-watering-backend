"""remove duplicated MCU name from device registry

Revision ID: 20260825_0013
Revises: 20260825_0012
"""
from alembic import op
import sqlalchemy as sa


revision = "20260825_0013"
down_revision = "20260825_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch_op:
        batch_op.drop_column("controller_name")


def downgrade() -> None:
    with op.batch_alter_table("devices") as batch_op:
        batch_op.add_column(sa.Column("controller_name", sa.String(), nullable=True))

