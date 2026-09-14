from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config


def test_small_waterings_are_soft_deleted_on_upgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "20260825_0013")

    samples = [(10.0, 0), (49.9, 0), (50.0, 0), (100.0, 0), (20.0, 1)]
    with sqlite3.connect(database_path) as connection:
        for index, (amount, invalid) in enumerate(samples, start=1):
            connection.execute(
                """INSERT INTO plant_watering_events
                   (device_id, event_start_at, occurred_at, weight_before_g,
                    weight_after_g, amount_g, source, fertilized, invalid,
                    detected_at, updated_at)
                   VALUES (?, ?, ?, 100, ?, ?, 'detected', 0, ?, 1, 1)""",
                ("test-device", index, index, 100 + amount, amount, invalid),
            )

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT amount_g, invalid, updated_at FROM plant_watering_events ORDER BY id"
        ).fetchall()
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "20260913_0014",
        )
    assert len(rows) == len(samples)
    assert [row[1] for row in rows] == [1, 1, 0, 0, 1]
    assert all(row[2] > 1 for row in rows[:2])
    assert rows[2:] == [(50.0, 0, 1.0), (100.0, 0, 1.0), (20.0, 1, 1.0)]

    # Downgrading must not resurrect previously invalidated events.
    command.downgrade(config, "20260825_0013")
    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT amount_g, invalid, updated_at FROM plant_watering_events ORDER BY id"
        ).fetchall() == rows
