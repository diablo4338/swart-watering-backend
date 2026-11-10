"""Delete invalid watering events above the former 1000 g limit.

Revision ID: 20261110_0015
Revises: 20260913_0014
"""
from alembic import op
import sqlalchemy as sa


revision = "20261110_0015"
down_revision = "20260913_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    events = sa.table(
        "plant_watering_events",
        sa.column("invalid", sa.Integer()),
        sa.column("amount_g", sa.Float()),
    )
    op.execute(events.delete().where(events.c.invalid == 1, events.c.amount_g > 1000.0))


def downgrade() -> None:
    # Deleted events cannot be recovered by a schema downgrade.
    pass
