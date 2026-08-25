"""store device snapshots outside operation history

Revision ID: 20260825_0012
Revises: 20260824_0011
"""
from alembic import op
import sqlalchemy as sa


revision = "20260825_0012"
down_revision = "20260824_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_snapshots",
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("result_json", sa.String(), nullable=False),
        sa.Column("received_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("device_id"),
    )
    op.execute("""
        INSERT INTO device_snapshots (device_id, result_json, received_at)
        SELECT source.device_id, source.result_json, source.updated_at
        FROM operations AS source
        WHERE source.operation_type = 'device_status'
          AND source.status = 'success'
          AND source.result_json IS NOT NULL
          AND source.device_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM operations AS newer
              WHERE newer.device_id = source.device_id
                AND newer.operation_type = 'device_status'
                AND newer.status = 'success'
                AND newer.result_json IS NOT NULL
                AND (
                    newer.updated_at > source.updated_at
                    OR (newer.updated_at = source.updated_at AND newer.operation_id > source.operation_id)
                )
          )
    """)
    op.execute("""
        DELETE FROM command_queue
        WHERE operation_id IN (
            SELECT operation_id FROM operations WHERE operation_type = 'device_status'
        )
    """)
    op.execute("""
        DELETE FROM operation_events
        WHERE operation_id IN (
            SELECT operation_id FROM operations WHERE operation_type = 'device_status'
        )
    """)
    op.execute("DELETE FROM operations WHERE operation_type = 'device_status'")


def downgrade() -> None:
    op.drop_table("device_snapshots")
