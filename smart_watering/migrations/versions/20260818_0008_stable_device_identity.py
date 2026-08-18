"""replace mutable device-name keys with stable device ids

Revision ID: 20260818_0008
Revises: 20260813_0007
"""
from alembic import op


revision = "20260818_0008"
down_revision = "20260813_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys=OFF")
    op.execute("""
        CREATE TABLE devices_new (
            id VARCHAR NOT NULL PRIMARY KEY,
            name VARCHAR NOT NULL,
            ip VARCHAR NOT NULL,
            base_url VARCHAR NOT NULL,
            device_type VARCHAR NOT NULL,
            created_at FLOAT NOT NULL,
            updated_at FLOAT NOT NULL,
            CONSTRAINT ck_devices_device_type CHECK (device_type IN ('plant', 'tank'))
        )
    """)
    op.execute("""
        INSERT INTO devices_new (id, name, ip, base_url, device_type, created_at, updated_at)
        SELECT lower(hex(randomblob(16))), name, ip, base_url, device_type, created_at, updated_at
        FROM devices
    """)
    op.execute("""
        CREATE TABLE operations_new (
            operation_id VARCHAR NOT NULL PRIMARY KEY,
            device_id VARCHAR REFERENCES devices_new(id) ON DELETE RESTRICT,
            target_name VARCHAR,
            operation_type VARCHAR NOT NULL,
            payload_json VARCHAR,
            result_json VARCHAR,
            status VARCHAR NOT NULL,
            created_at FLOAT NOT NULL,
            updated_at FLOAT NOT NULL
        )
    """)
    op.execute("""
        INSERT INTO operations_new
        SELECT o.operation_id, d.id, CASE WHEN d.id IS NULL THEN o.device_name END,
               o.operation_type, o.payload_json, o.result_json,
               o.status, o.created_at, o.updated_at
        FROM operations o LEFT JOIN devices_new d ON d.name = o.device_name
    """)
    op.execute("""
        CREATE TABLE command_queue_new (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            operation_id VARCHAR NOT NULL REFERENCES operations_new(operation_id) ON DELETE CASCADE,
            device_id VARCHAR REFERENCES devices_new(id) ON DELETE CASCADE,
            target_name VARCHAR,
            base_url VARCHAR NOT NULL, path VARCHAR NOT NULL, method VARCHAR NOT NULL,
            payload_json VARCHAR, description VARCHAR NOT NULL,
            created_at FLOAT NOT NULL, started_at FLOAT
        )
    """)
    op.execute("""
        INSERT INTO command_queue_new
        SELECT q.id, q.operation_id, d.id, CASE WHEN d.id IS NULL THEN q.device_name END,
               q.base_url, q.path, q.method,
               q.payload_json, q.description, q.created_at, q.started_at
        FROM command_queue q LEFT JOIN devices_new d ON d.name = q.device_name
    """)
    op.execute("""
        CREATE TABLE plant_watering_events_new (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            device_id VARCHAR NOT NULL REFERENCES devices_new(id) ON DELETE CASCADE,
            event_start_at FLOAT NOT NULL, occurred_at FLOAT NOT NULL,
            weight_before_g FLOAT NOT NULL, weight_after_g FLOAT NOT NULL,
            amount_g FLOAT NOT NULL, source VARCHAR NOT NULL,
            fertilized INTEGER DEFAULT 0 NOT NULL, invalid INTEGER DEFAULT 0 NOT NULL,
            detected_at FLOAT NOT NULL, updated_at FLOAT NOT NULL,
            CONSTRAINT uq_plant_watering_events_device_start UNIQUE (device_id, event_start_at)
        )
    """)
    op.execute("""
        INSERT INTO plant_watering_events_new
        SELECT e.id, d.id, e.event_start_at, e.occurred_at, e.weight_before_g,
               e.weight_after_g, e.amount_g, e.source, e.fertilized, e.invalid,
               e.detected_at, e.updated_at
        FROM plant_watering_events e JOIN devices_new d ON d.name = e.device_name
    """)
    op.execute("""
        CREATE TABLE device_watering_settings_new (
            device_id VARCHAR NOT NULL PRIMARY KEY REFERENCES devices_new(id) ON DELETE CASCADE,
            dry_weight_g INTEGER, dry_weight_updated_at FLOAT,
            wet_weight_g INTEGER, wet_weight_updated_at FLOAT,
            watering_loss_threshold_percent INTEGER,
            watering_loss_threshold_updated_at FLOAT
        )
    """)
    op.execute("""
        INSERT INTO device_watering_settings_new
        SELECT d.id, s.dry_weight_g, s.dry_weight_updated_at, s.wet_weight_g,
               s.wet_weight_updated_at, s.watering_loss_threshold_percent,
               s.watering_loss_threshold_updated_at
        FROM device_watering_settings s JOIN devices_new d ON d.name = s.device_name
    """)

    op.drop_table("command_queue")
    op.drop_table("plant_watering_events")
    op.drop_table("device_watering_settings")
    op.drop_table("operations")
    op.drop_table("devices")
    op.rename_table("devices_new", "devices")
    op.rename_table("operations_new", "operations")
    op.rename_table("command_queue_new", "command_queue")
    op.rename_table("plant_watering_events_new", "plant_watering_events")
    op.rename_table("device_watering_settings_new", "device_watering_settings")
    op.create_index("ix_devices_name", "devices", ["name"], unique=True)
    op.create_index("ix_operations_device_id", "operations", ["device_id"])
    op.create_index("ix_command_queue_device_id", "command_queue", ["device_id"])
    op.create_index(
        "ix_plant_watering_events_device_occurred",
        "plant_watering_events", ["device_id", "occurred_at"],
    )
    op.execute("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    raise RuntimeError("stable device identities cannot be safely downgraded")
