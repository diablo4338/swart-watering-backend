"""add structured operation tracing

Revision ID: 20260823_0010
Revises: 20260822_0009
"""
import sqlalchemy as sa
from alembic import op


revision = "20260823_0010"
down_revision = "20260822_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("operations", sa.Column("correlation_id", sa.String(), nullable=True))
    op.add_column("operations", sa.Column("causation_id", sa.String(), nullable=True))
    op.execute("UPDATE operations SET correlation_id = operation_id")
    with op.batch_alter_table("operations") as batch_op:
        batch_op.alter_column("correlation_id", existing_type=sa.String(), nullable=False)
        batch_op.create_index("ix_operations_correlation_id", ["correlation_id"])
        batch_op.create_foreign_key(
            "fk_operations_causation_id", "operations", ["causation_id"], ["operation_id"],
            ondelete="SET NULL",
        )

    op.add_column("operation_events", sa.Column("source", sa.String(), nullable=True))
    op.add_column("operation_events", sa.Column("event_type", sa.String(), nullable=True))
    op.add_column("operation_events", sa.Column("data_json", sa.String(), nullable=True))
    op.execute("UPDATE operation_events SET source = 'backend', event_type = 'operation.status_changed'")
    with op.batch_alter_table("operation_events") as batch_op:
        batch_op.alter_column("source", existing_type=sa.String(), nullable=False)
        batch_op.alter_column("event_type", existing_type=sa.String(), nullable=False)
        batch_op.create_index("ix_operation_events_operation_created", ["operation_id", "created_at"])


def downgrade() -> None:
    with op.batch_alter_table("operation_events") as batch_op:
        batch_op.drop_index("ix_operation_events_operation_created")
        batch_op.drop_column("data_json")
        batch_op.drop_column("event_type")
        batch_op.drop_column("source")
    with op.batch_alter_table("operations") as batch_op:
        batch_op.drop_constraint("fk_operations_causation_id", type_="foreignkey")
        batch_op.drop_index("ix_operations_correlation_id")
        batch_op.drop_column("causation_id")
        batch_op.drop_column("correlation_id")
