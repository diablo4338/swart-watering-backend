"""detected plant watering events

Revision ID: 20260727_0004
Revises: 20260630_0003
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa


revision = "20260727_0004"
down_revision = "20260630_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plant_watering_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("device_name", sa.String(), nullable=False),
        sa.Column("event_start_at", sa.Float(), nullable=False),
        sa.Column("occurred_at", sa.Float(), nullable=False),
        sa.Column("weight_before_g", sa.Float(), nullable=False),
        sa.Column("weight_after_g", sa.Float(), nullable=False),
        sa.Column("amount_g", sa.Float(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("invalid", sa.Integer(), server_default="0", nullable=False),
        sa.Column("detected_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["device_name"], ["devices.name"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "device_name", "event_start_at",
            name="uq_plant_watering_events_device_start",
        ),
    )
    op.create_index(
        "ix_plant_watering_events_device_occurred",
        "plant_watering_events",
        ["device_name", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_plant_watering_events_device_occurred",
        table_name="plant_watering_events",
    )
    op.drop_table("plant_watering_events")
