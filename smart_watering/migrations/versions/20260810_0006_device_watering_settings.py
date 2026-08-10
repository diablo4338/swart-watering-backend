"""per-field device watering settings timestamps

Revision ID: 20260810_0006
Revises: 20260807_0005
"""
from alembic import op
import sqlalchemy as sa


revision = "20260810_0006"
down_revision = "20260807_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_watering_settings",
        sa.Column("device_name", sa.String(), nullable=False),
        sa.Column("dry_weight_g", sa.Integer(), nullable=True),
        sa.Column("dry_weight_updated_at", sa.Float(), nullable=True),
        sa.Column("wet_weight_g", sa.Integer(), nullable=True),
        sa.Column("wet_weight_updated_at", sa.Float(), nullable=True),
        sa.Column("watering_loss_threshold_percent", sa.Integer(), nullable=True),
        sa.Column("watering_loss_threshold_updated_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["device_name"], ["devices.name"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("device_name"),
    )


def downgrade() -> None:
    op.drop_table("device_watering_settings")
