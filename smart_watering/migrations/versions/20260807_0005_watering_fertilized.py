"""fertilizer marker for detected plant watering events

Revision ID: 20260807_0005
Revises: 20260727_0004
Create Date: 2026-08-07
"""
from alembic import op
import sqlalchemy as sa


revision = "20260807_0005"
down_revision = "20260727_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plant_watering_events",
        sa.Column("fertilized", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("plant_watering_events", "fertilized")
