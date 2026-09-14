"""Soft-delete watering events below 50 grams.

Revision ID: 20260913_0014
Revises: 20260825_0013
"""
import time

from alembic import op
import sqlalchemy as sa


revision = "20260913_0014"
down_revision = "20260825_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    events = sa.table(
        "plant_watering_events",
        sa.column("amount_g", sa.Float()),
        sa.column("invalid", sa.Integer()),
        sa.column("updated_at", sa.Float()),
    )
    op.execute(
        events.update()
        .where(events.c.amount_g < 50.0, events.c.invalid == 0)
        .values(invalid=1, updated_at=time.time())
    )


def downgrade() -> None:
    # Previous invalidation reasons are not stored, so restoring these rows
    # could also restore events that users had already soft-deleted.
    pass
