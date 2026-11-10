from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config


def test_upgrade_deletes_only_invalid_waterings_above_1000(tmp_path):
    database_path = tmp_path / "migration.db"
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260913_0014")
    samples = [(49, 1), (1000, 1), (1000.1, 1), (1111, 1), (2000, 1), (2000, 0)]
    with sqlite3.connect(database_path) as connection:
        for index, (amount, invalid) in enumerate(samples, start=1):
            connection.execute(
                """INSERT INTO plant_watering_events
                   (device_id, event_start_at, occurred_at, weight_before_g,
                    weight_after_g, amount_g, source, fertilized, invalid,
                    detected_at, updated_at)
                   VALUES (?, ?, ?, 100, ?, ?, 'prometheus', 1, ?, 1, 1)""",
                ("test-device", index, index, 100 + amount, amount, invalid),
            )

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT amount_g, invalid, fertilized FROM plant_watering_events ORDER BY id"
        ).fetchall()
    assert rows == [(49, 1, 1), (1000, 1, 1), (2000, 0, 1)]

    command.downgrade(config, "20260913_0014")
    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT amount_g, invalid, fertilized FROM plant_watering_events ORDER BY id"
        ).fetchall() == rows
