import json
import io
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import smart_watering.interfaces.cli as smart_cli
import smart_watering.domain as smart_core
import smart_watering.jobs.snapshotter as snapshotter
from smart_watering.application.watering_detection import PlantWateringDetector
from fastapi.testclient import TestClient
from smart_watering.callback_app.main import create_app as create_callback_app
from smart_watering.callback_app.service import CallbackService
from smart_watering.callback_app import utils as callback_utils
from smart_watering.public_api_app import config, domain, security, statistics
from smart_watering.public_api_app.errors import PublicApiError
from smart_watering.public_api_app.main import create_app
from smart_watering.public_api_app.routers import devices as public_api_devices
from smart_watering.public_api_app.runtime import ApiRuntime
from smart_watering.public_api_app.service import PublicApiService


def test_water_consumption_periods_cover_week_in_display_order() -> None:
    now = datetime(2026, 7, 26, 14, 0, tzinfo=timezone(timedelta(hours=2)))

    periods = statistics.water_consumption_periods(now)

    assert len(periods) == 14
    assert [(period_date.isoformat(), period_name) for period_date, period_name, _, _ in periods[:6]] == [
        ("2026-07-26", "night"),
        ("2026-07-26", "day"),
        ("2026-07-25", "night"),
        ("2026-07-25", "day"),
        ("2026-07-24", "night"),
        ("2026-07-24", "day"),
    ]
    today_night = periods[0]
    today_day = periods[1]
    assert (today_night[2].hour, today_night[3].hour) == (20, 8)
    assert today_night[2].date().isoformat() == "2026-07-26"
    assert today_night[3].date().isoformat() == "2026-07-27"
    assert (today_day[2].hour, today_day[3].hour) == (8, 20)


def test_water_consumption_periods_keep_previous_record_before_8_am() -> None:
    tz = timezone(timedelta(hours=2))
    now = datetime(2026, 7, 26, 7, 59, tzinfo=tz)

    periods = statistics.water_consumption_periods(now)

    assert len(periods) == 14
    assert [(period_date.isoformat(), period_name) for period_date, period_name, _, _ in periods[:6]] == [
        ("2026-07-25", "night"),
        ("2026-07-25", "day"),
        ("2026-07-24", "night"),
        ("2026-07-24", "day"),
        ("2026-07-23", "night"),
        ("2026-07-23", "day"),
    ]
    current_night = periods[0]
    assert current_night[2] == datetime(2026, 7, 25, 20, 0, tzinfo=tz)
    assert current_night[3] == datetime(2026, 7, 26, 8, 0, tzinfo=tz)


def test_water_consumption_periods_start_new_record_at_8_am() -> None:
    tz = timezone(timedelta(hours=2))
    now = datetime(2026, 7, 26, 8, 0, tzinfo=tz)

    periods = statistics.water_consumption_periods(now)

    assert periods[0][0].isoformat() == "2026-07-26"
    assert periods[1][0].isoformat() == "2026-07-26"


def test_prometheus_instance_adds_default_http_port() -> None:
    assert statistics.prometheus_instance("http://192.0.2.100") == "192.0.2.100:80"
    assert statistics.prometheus_instance("http://192.0.2.100:8080") == "192.0.2.100:8080"


def test_water_consumption_query_end_uses_now_for_active_period() -> None:
    tz = timezone(timedelta(hours=2))
    start = datetime(2026, 7, 26, 8, 0, tzinfo=tz)
    end = datetime(2026, 7, 26, 20, 0, tzinfo=tz)
    now = datetime(2026, 7, 26, 14, 30, tzinfo=tz)

    assert statistics.water_consumption_query_end(start, end, now) == now
    assert statistics.water_consumption_query_end(start, end, end + timedelta(hours=1)) == end
    assert statistics.water_consumption_query_end(start, end, start - timedelta(minutes=1)) is None


def test_water_consumption_elapsed_hours_uses_actual_period_length() -> None:
    tz = timezone(timedelta(hours=2))
    start = datetime(2026, 7, 26, 8, 0, tzinfo=tz)

    assert statistics.water_consumption_elapsed_hours(
        start,
        datetime(2026, 7, 26, 13, 30, tzinfo=tz),
    ) == 5.5
    assert statistics.water_consumption_elapsed_hours(
        start,
        datetime(2026, 7, 26, 20, 0, tzinfo=tz),
    ) == 12.0


def test_adaptive_weight_change_averages_all_sample_intervals() -> None:
    samples = [
        (0.0, 100.0),
        (1800.0, 96.0),
        (3600.0, 90.0),
    ]

    assert statistics.adaptive_weight_change_per_hour(samples) == -10.0


def test_consumption_drop_uses_median_of_previous_same_periods() -> None:
    previous = [-10.0, -9.0, -11.0, -10.0, -12.0, -8.0]

    assert statistics.consumption_is_below_median(-6.9, previous, 30)
    assert not statistics.consumption_is_below_median(-7.1, previous, 30)


def test_consumption_drop_requires_previous_nonzero_values() -> None:
    assert not statistics.consumption_is_below_median(-1.0, [], 30)
    assert not statistics.consumption_is_below_median(-1.0, [0.0, 0.0], 30)


def test_consumption_drop_threshold_percent_defaults_to_30(monkeypatch) -> None:
    monkeypatch.delenv(config.CONSUMPTION_DROP_THRESHOLD_PERCENT_ENV, raising=False)

    assert config.consumption_drop_threshold_percent() == 30


def test_consumption_drop_threshold_percent_reads_integer(monkeypatch) -> None:
    monkeypatch.setenv(config.CONSUMPTION_DROP_THRESHOLD_PERCENT_ENV, "45")

    assert config.consumption_drop_threshold_percent() == 45


def test_consumption_median_days_defaults_to_5(monkeypatch) -> None:
    monkeypatch.delenv(config.CONSUMPTION_MEDIAN_DAYS_ENV, raising=False)

    assert config.consumption_median_days() == 5


def test_consumption_median_days_reads_integer(monkeypatch) -> None:
    monkeypatch.setenv(config.CONSUMPTION_MEDIAN_DAYS_ENV, "9")

    assert config.consumption_median_days() == 9


def test_adaptive_weight_change_ignores_rapid_losses_around_increase() -> None:
    samples = [
        (0.0, 100.0),
        (300.0, 95.0),
        (600.0, 120.0),
        (900.0, 114.0),
    ]

    assert statistics.adaptive_weight_change_per_hour(samples) == -20.0


def test_adaptive_weight_change_ignores_increase_at_threshold() -> None:
    exactly_ten_grams = [(0.0, 100.0), (300.0, 110.0)]

    assert statistics.adaptive_weight_change_per_hour(exactly_ten_grams) == 0.0


def test_adaptive_weight_change_ignores_drop_above_rate_limit() -> None:
    samples = [
        (0.0, 100.0),
        (60.0, 80.0),
        (120.0, 100.0),
        (180.0, 95.0),
        (240.0, 65.0),
        (3600.0, 65.0),
    ]

    assert statistics.adaptive_weight_change_per_hour(samples) == -5.0


def test_adaptive_weight_change_ignores_invalid_values_before_delta_filter() -> None:
    samples = [
        (0.0, 100.0),
        (300.0, 0.0),
        (600.0, -50.0),
        (3600.0, 80.0),
    ]

    assert statistics.adaptive_weight_change_per_hour(samples) == -20.0


def test_adaptive_weight_change_applies_25_grams_per_hour_limit() -> None:
    samples = [
        (0.0, 100.0),
        (30 * 60.0, 86.0),
        (60 * 60.0, 80.0),
    ]

    # The ranked 30-minute limit is 12.5 g, so -14 g is ignored and the
    # following -6 g transition is included.
    assert statistics.adaptive_weight_change_per_hour(samples) == -6.0


def test_adaptive_weight_change_does_not_double_count_small_sensor_noise() -> None:
    samples = [
        (0.0, 100.0),
        (900.0, 99.0),
        (1800.0, 100.0),
        (2700.0, 99.0),
        (3600.0, 98.0),
    ]

    assert statistics.adaptive_weight_change_per_hour(samples) == -2.0


def test_adaptive_weight_change_allows_up_to_five_grams_between_close_points() -> None:
    samples = [
        (0.0, 100.0),
        (60.0, 99.0),
        (120.0, 98.0),
        (180.0, 97.0),
    ]

    assert statistics.adaptive_weight_change_per_hour(samples) == -60.0


def test_detect_watering_events_records_end_of_multi_sample_rise() -> None:
    samples = [
        (0.0, 100.0),
        (60.0, 104.0),
        (120.0, 112.0),
        (180.0, 125.0),
        (240.0, 130.0),
        (360.0, 132.0),
        (420.0, 129.0),
    ]

    assert statistics.detect_watering_events(samples) == [
        {
            "event_start_at": 0.0,
            "occurred_at": 360.0,
            "weight_before_g": 100.0,
            "weight_after_g": 132.0,
            "amount_g": 32.0,
        }
    ]


def test_detect_watering_events_treats_rises_more_than_five_minutes_apart_separately() -> None:
    samples = [
        (0.0, 100.0),
        (600.0, 103.0),
        (1200.0, 106.0),
        (1800.0, 109.0),
        (2400.0, 112.0),
        (3000.0, 115.0),
        (3060.0, 114.0),
        (3360.0, 113.0),
    ]

    assert statistics.detect_watering_events(samples) == []


def test_detect_watering_events_includes_rise_equal_to_threshold() -> None:
    samples = [(0.0, 100.0), (60.0, 110.0)]

    assert statistics.detect_watering_events(samples) == [
        {
            "event_start_at": 0.0,
            "occurred_at": 60.0,
            "weight_before_g": 100.0,
            "weight_after_g": 110.0,
            "amount_g": 10.0,
        }
    ]


def test_detect_watering_events_does_not_cap_accumulated_gradual_rise() -> None:
    samples = [
        (0.0, 1000.0),
        (60.0, 1400.0),
        (120.0, 1800.0),
        (180.0, 2200.0),
        (240.0, 2190.0),
        (540.0, 2180.0),
    ]

    assert statistics.detect_watering_events(
        samples, max_amount_g=1000.0
    ) == [
        {
            "event_start_at": 0.0,
            "occurred_at": 180.0,
            "weight_before_g": 1000.0,
            "weight_after_g": 2200.0,
            "amount_g": 1200.0,
        }
    ]


def test_detect_watering_events_merges_saw_teeth_until_last_rise_is_quiet() -> None:
    samples = [
        (0.0, 100.0),
        (60.0, 130.0),
        (120.0, 120.0),
        (240.0, 125.0),
        # This is already more than five minutes after the absolute maximum,
        # but only two minutes after the latest upward edge.
        (360.0, 121.0),
        (540.0, 119.0),
    ]

    assert statistics.detect_watering_events(samples) == [
        {
            "event_start_at": 0.0,
            "occurred_at": 60.0,
            "weight_before_g": 100.0,
            "weight_after_g": 130.0,
            "amount_g": 30.0,
        }
    ]


def test_detect_watering_events_uses_value_before_first_rise_as_baseline() -> None:
    samples = [
        (0.0, 500.0),
        (60.0, 501.0),
        (120.0, 490.0),
        (180.0, 480.0),
        (240.0, 600.0),
        (300.0, 780.0),
        (600.0, 775.0),
    ]

    assert statistics.detect_watering_events(samples) == [
        {
            "event_start_at": 0.0,
            "occurred_at": 300.0,
            "weight_before_g": 500.0,
            "weight_after_g": 780.0,
            "amount_g": 280.0,
        }
    ]


def test_detect_watering_events_uses_sparse_neighbouring_points() -> None:
    samples = [(0.0, 100.0), (1200.0, 350.0)]

    assert statistics.detect_watering_events(samples)[0]["amount_g"] == 250.0


def test_detector_uses_previous_sample_at_fifty_minute_interval() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("10.0.0.1", "plant", "plant_1")
        start = datetime(2026, 8, 3, 10, 25, tzinfo=timezone.utc)
        end = start + timedelta(hours=3)
        detector = PlantWateringDetector(app, "http://prometheus")
        requested_ranges = []

        def range_samples(_query, range_start, range_end):
            requested_ranges.append((range_start, range_end))
            return [
                ((start - timedelta(minutes=50)).timestamp(), 3962.0),
                (start.timestamp(), 4410.0),
                ((start + timedelta(minutes=21)).timestamp(), 4406.0),
            ]

        detector.prometheus.range_samples = range_samples

        result = detector.scan_device("plant_1", start, end)

        assert requested_ranges[0][0] == start - timedelta(minutes=50)
        assert result.detected == 1
        assert app.plant_waterings.list_valid("plant_1")[0]["amount_g"] == 448.0


def test_detector_does_not_store_events_before_requested_range() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("10.0.0.1", "plant", "plant_1")
        start = datetime(2026, 8, 3, 10, 25, tzinfo=timezone.utc)
        detector = PlantWateringDetector(app, "http://prometheus")
        detector.prometheus.range_samples = lambda *_args: [
            ((start - timedelta(minutes=50)).timestamp(), 3900.0),
            ((start - timedelta(minutes=30)).timestamp(), 4000.0),
            (start.timestamp(), 3990.0),
        ]

        result = detector.scan_device(
            "plant_1", start, start + timedelta(hours=3)
        )

        assert result.detected == 0
        assert app.plant_waterings.list_valid("plant_1") == []


def test_detect_watering_events_plant_2_real_samples_2026_07_24() -> None:
    samples = [
        (1784848260.0, 3717.0),
        (1784851920.0, 3716.0),
        (1784855580.0, 3714.0),
        (1784857380.0, 3712.0),
        (1784861040.0, 3711.0),
        (1784864700.0, 3709.0),
        (1784868360.0, 3707.0),
        (1784872020.0, 3705.0),
        (1784873820.0, 3705.0),
        (1784877480.0, 3698.0),
        (1784881140.0, 3692.0),
        (1784884800.0, 3687.0),
        (1784886600.0, 3685.0),
        (1784890260.0, 3679.0),
        (1784893920.0, 3675.0),
        (1784897580.0, 3670.0),
        (1784899380.0, 3669.0),
        (1784903040.0, 3664.0),
        (1784906700.0, 3660.0),
        (1784910360.0, 4073.0),
        (1784914020.0, 4063.0),
        (1784915820.0, 4060.0),
        (1784919480.0, 4057.0),
        (1784923140.0, 4054.0),
        (1784926800.0, 4051.0),
    ]

    assert statistics.detect_watering_events(samples) == [
        {
            "event_start_at": 1784906700.0,
            "occurred_at": 1784910360.0,
            "weight_before_g": 3660.0,
            "weight_after_g": 4073.0,
            "amount_g": 413.0,
        }
    ]


def test_detect_watering_events_does_not_reject_positive_values_by_amount() -> None:
    samples = [(0.0, 100.0), (1200.0, 1101.0)]

    assert statistics.detect_watering_events(
        samples, max_amount_g=1000.0
    )[0]["amount_g"] == 1001.0


def test_detect_watering_events_discards_zero_and_negative_values() -> None:
    samples = [
        (0.0, 4551.0),
        (60.0, -86.0),
        (120.0, -138.0),
        (180.0, -25.0),
        (240.0, 0.0),
        (300.0, 4455.0),
        (360.0, 4926.0),
        (420.0, 4927.0),
    ]

    assert statistics.detect_watering_events(samples) == [
        {
            "event_start_at": 300.0,
            "occurred_at": 420.0,
            "weight_before_g": 4455.0,
            "weight_after_g": 4927.0,
            "amount_g": 472.0,
        }
    ]


def test_invalid_detected_watering_is_not_recreated() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("10.0.0.1", "plant", "plant_1")
        event = {
            "event_start_at": 100.0,
            "occurred_at": 200.0,
            "weight_before_g": 100.0,
            "weight_after_g": 130.0,
            "amount_g": 30.0,
        }

        stored, created = app.plant_waterings.upsert_detected("plant_1", event)
        assert created is True
        assert app.plant_waterings.invalidate("plant_1", stored["id"]) is True

        same, created_again = app.plant_waterings.upsert_detected(
            "plant_1",
            {**event, "occurred_at": 220.0, "weight_after_g": 140.0, "amount_g": 40.0},
        )

        assert created_again is False
        assert same["invalid"] is True
        assert app.plant_waterings.list_valid("plant_1") == []


def test_detected_watering_new_start_does_not_duplicate_same_new_baseline() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("10.0.0.1", "plant", "plant_1")
        event = {
            "event_start_at": 100.0,
            "occurred_at": 200.0,
            "weight_before_g": 100.0,
            "weight_after_g": 130.0,
            "amount_g": 30.0,
        }
        app.plant_waterings.upsert_detected("plant_1", event)

        _stored, created = app.plant_waterings.upsert_detected(
            "plant_1",
            {**event, "event_start_at": 110.0},
        )

        assert created is False
        assert len(app.plant_waterings.list_valid("plant_1")) == 1


def make_store(temp_dir: str) -> smart_core.SQLiteStore:
    store = smart_core.SQLiteStore(str(Path(temp_dir) / "test.db"))
    store.init_schema()
    return store


def add_confirmed_tank(app: smart_cli.SmartWateringCliApp, ip: str = "192.168.1.51", name: str = "main_tank") -> smart_core.Device:
    app.registry.add(ip, "tank", name)
    return app.registry.apply_confirmed_config(name, {"device_type": "tank", "name": name})


def test_store_uses_env_database_path(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "custom" / "smart.db"
        monkeypatch.setenv(smart_core.DB_PATH_ENV, str(db_path))

        store = smart_core.SQLiteStore()
        store.init_schema()

        assert store.db_path == str(db_path)
        assert db_path.exists()


def test_store_rejects_uncreatable_database_path() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        parent_file = Path(temp_dir) / "not_a_dir"
        parent_file.write_text("x", encoding="utf-8")
        store = smart_core.SQLiteStore(str(parent_file / "smart.db"))

        try:
            store.init_schema()
        except smart_core.SmartWateringError as exc:
            assert "cannot create database directory" in str(exc)
        else:
            raise AssertionError("expected SmartWateringError")


def test_device_name_is_database_primary_key() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test.db"
        store = smart_core.SQLiteStore(str(db_path))
        store.init_schema()
        connection = sqlite3.connect(db_path)
        try:
            columns = connection.execute("PRAGMA table_info(devices)").fetchall()
        finally:
            connection.close()

        name_column = next(column for column in columns if column[1] == "name")
        assert name_column[5] == 1


def test_watering_setting_dates_are_updated_per_field_after_confirmed_changes() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        device = app.registry.add("192.168.1.10", "plant", "plant_1")

        operation_id = app.queue_device_config(
            device,
            {
                "dry_weight_g": 120,
                "wet_weight_g": 500,
                "watering_loss_threshold_percent": 35,
            },
            "configure watering parameters",
        )
        app.operations.event(operation_id, "accepted", "device accepted command")
        assert all(value is None for value in app.registry.watering_settings("plant_1").values())

        app.operations.event(operation_id, "success", "config_updated")
        confirmed = app.registry.watering_settings("plant_1")

        assert confirmed["dry_weight_g"] == 120
        assert confirmed["dry_weight_updated_at"] is not None
        assert confirmed["wet_weight_g"] == 500
        assert confirmed["wet_weight_updated_at"] is not None
        assert confirmed["watering_loss_threshold_percent"] == 35
        assert confirmed["watering_loss_threshold_updated_at"] is not None

        dry_updated_at = confirmed["dry_weight_updated_at"]
        wet_updated_at = confirmed["wet_weight_updated_at"]
        wet_operation_id = app.queue_device_config(
            device, {"wet_weight_g": 510}, "update wet weight"
        )
        app.operations.event(wet_operation_id, "success", "config_updated")
        partially_updated = app.registry.watering_settings("plant_1")

        assert partially_updated["dry_weight_g"] == 120
        assert partially_updated["dry_weight_updated_at"] == dry_updated_at
        assert partially_updated["wet_weight_g"] == 510
        assert partially_updated["wet_weight_updated_at"] >= wet_updated_at
        assert partially_updated["watering_loss_threshold_percent"] == 35


def test_registry_assigns_default_plant_names() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        registry = smart_core.DeviceRegistry(make_store(temp_dir))

        first = registry.add("192.168.1.10", "plant", None)
        second = registry.add("192.168.1.11", "plant", None)

        assert first.name == "plant_1"
        assert second.name == "plant_2"


def test_registry_rejects_duplicate_device_name() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        registry = smart_core.DeviceRegistry(make_store(temp_dir))

        registry.add("192.168.1.10", "plant", "plant_1")

        try:
            registry.add("192.168.1.11", "plant", "plant_1")
        except smart_core.SmartWateringError as exc:
            assert "device name already exists" in str(exc)
        else:
            raise AssertionError("expected SmartWateringError")


def test_registry_rejects_config_rename_to_existing_device_name() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        registry = smart_core.DeviceRegistry(make_store(temp_dir))
        registry.add("192.168.1.10", "plant", "plant_1")
        registry.add("192.168.1.11", "plant", "plant_2")

        try:
            registry.validate_config_update("plant_1", {"name": "plant_2"})
        except smart_core.SmartWateringError as exc:
            assert "device name already exists" in str(exc)
        else:
            raise AssertionError("expected SmartWateringError")


def test_registry_demotes_previous_tank_when_new_tank_config_is_confirmed() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        registry = smart_core.DeviceRegistry(make_store(temp_dir))

        registry.add("192.168.1.10", "tank", "tank_a")
        registry.apply_confirmed_config("tank_a", {"device_type": "tank", "name": "tank_a"})
        registry.add("192.168.1.11", "tank", "tank_b")
        assert registry.get("tank_b").device_type == "plant"

        registry.apply_confirmed_config("tank_b", {"device_type": "tank", "name": "tank_b"})

        devices = registry.list()
        assert [(device.name, device.device_type, device.ip) for device in devices] == [
            ("tank_a", "plant", "192.168.1.10"),
            ("tank_b", "tank", "192.168.1.11")
        ]


def test_registry_add_tank_updates_existing_device_type_without_replacing_address() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        registry = smart_core.DeviceRegistry(make_store(temp_dir))

        registry.add("192.168.1.20", "tank", "main_tank")
        registry.apply_confirmed_config("main_tank", {"device_type": "tank", "name": "main_tank"})
        registry.add("192.168.1.10", "plant", "plant_1")
        updated = registry.add("192.168.1.99", "tank", "plant_1")

        assert updated.name == "plant_1"
        assert updated.device_type == "tank"
        assert updated.ip == "192.168.1.10"
        assert registry.get("plant_1").device_type == "plant"
        registry.apply_confirmed_config("plant_1", {"device_type": "tank", "name": "plant_1"})
        assert registry.get("plant_1").device_type == "tank"
        assert registry.get("plant_1").ip == "192.168.1.10"
        assert registry.get("main_tank").device_type == "plant"


def test_registry_config_to_tank_demotes_previous_tank() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        registry = smart_core.DeviceRegistry(make_store(temp_dir))
        registry.add("192.168.1.20", "tank", "main_tank")
        registry.apply_confirmed_config("main_tank", {"device_type": "tank", "name": "main_tank"})
        registry.add("192.168.1.10", "plant", "plant_1")

        updated = registry.apply_confirmed_config("plant_1", {"device_type": "tank"})

        assert updated.device_type == "tank"
        assert registry.get("plant_1").device_type == "tank"
        assert registry.get("main_tank").device_type == "plant"


def test_device_config_updates_registry_only_after_controller_success() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        device = app.registry.add("192.168.1.10", "plant", "plant_1")
        failed_operation_id = app.queue_device_config(
            device,
            {"device_type": "tank", "name": "main_tank"},
            "configure main_tank",
        )

        assert app.registry.get("plant_1").device_type == "plant"
        app.operations.event(failed_operation_id, "accepted", "device accepted command")
        assert app.registry.get("plant_1").device_type == "plant"
        app.operations.event(failed_operation_id, "error", "config_update_failed")
        app.queue.drop_device("plant_1")
        assert app.registry.get("plant_1").device_type == "plant"

        success_operation_id = app.queue_device_config(
            device,
            {"device_type": "tank", "name": "main_tank"},
            "configure main_tank",
        )
        app.operations.event(success_operation_id, "success", "config_updated")

        updated = app.registry.get("main_tank")
        assert updated.device_type == "tank"
        try:
            app.registry.get("plant_1")
        except smart_core.SmartWateringError:
            pass
        else:
            raise AssertionError("old device name should be removed after confirmed rename")


def test_parse_config_assignments_supports_aliases() -> None:
    config = smart_cli.parse_config_assignments(["tare=120", "dry=40", "type=tank", "name=main_tank"])

    assert config == {
        "tare_weight_g": 120.0,
        "dry_weight_g": 40.0,
        "device_type": "tank",
        "name": "main_tank",
    }


def test_queue_preserves_fifo_order() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = make_store(temp_dir)
        queue = smart_core.CommandQueue(store)

        queue.enqueue("op-1", "plant_1", "http://device", "/config", "POST", {"name": "plant_1"}, "config")
        queue.enqueue("op-2", "plant_1", "http://device", "/watering/start", "POST", {"target_g": 100}, "fill")

        commands = queue.list()
        assert [command.description for command in commands] == ["config", "fill"]


def test_stop_drops_all_queued_fill_commands() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = make_store(temp_dir)
        queue = smart_core.CommandQueue(store)

        queue.enqueue("op-1", "tank", "http://device", "/watering/start", "POST", {"target_g": 100}, "fill 100")
        queue.enqueue("op-2", "tank", "http://device", "/config", "POST", {"dry_weight_g": 50}, "config")
        queue.enqueue("op-3", "tank", "http://device", "/watering/start", "POST", {"target_g": 50}, "fill 50")
        queue.enqueue("op-4", "tank", "http://device", "/watering/stop", "POST", {}, "stop", drop_fill_commands=True)

        commands = queue.list()
        assert [command.description for command in commands] == ["config", "stop"]


def test_clear_device_queue_cancels_queued_operations() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        sleep_operation_id = app.queue_sleep("plant_1", True)
        zero_operation_id = app.queue_zero("plant_1")

        assert app.clear_device_queue("plant_1") == 2

        assert app.queue.list() == []
        assert app.operations.get(sleep_operation_id)["status"] == "cancelled"
        assert app.operations.get(zero_operation_id)["status"] == "cancelled"
        assert app.operations.events(sleep_operation_id)[-1]["detail"] == "cancelled by device queue clear"


def test_clear_device_queue_cancels_accepted_operation_after_command_was_removed() -> None:
    class AcceptedApi:
        def request_json(self, base_url, path, method, payload=None):
            return {"status": "accepted"}

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        operation_id = app.queue_sleep("plant_1", True)
        worker = smart_core.BackgroundWorker(
            AcceptedApi(),
            app.queue,
            app.operations,
            smart_core.WorkerState(str(Path(temp_dir) / "worker.pid")),
            retry_interval_sec=0,
            max_wait_sec=1,
        )
        worker.run()

        assert app.queue.list() == []
        assert app.operations.get(operation_id)["status"] == "accepted"
        assert app.clear_device_queue("plant_1") == 1
        assert app.operations.get(operation_id)["status"] == "cancelled"
        assert app.operations.events(operation_id)[-1]["detail"] == "cancelled by device queue clear"


def test_list_non_terminal_operations_filters_by_device_without_limit() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        app.registry.add("192.168.1.11", "plant", "plant_2")
        active_ids = [app.queue_zero("plant_1") for _ in range(25)]
        terminal_id = app.queue_sleep("plant_1", True)
        other_device_id = app.queue_zero("plant_2")
        app.operations.event(terminal_id, "success", "completed")

        operations = app.operations.list_non_terminal("plant_1")

        assert {operation["operation_id"] for operation in operations} == set(active_ids)
        assert terminal_id not in {operation["operation_id"] for operation in operations}
        assert other_device_id not in {operation["operation_id"] for operation in operations}


def test_stop_cancels_pending_watering_start_operation() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        add_confirmed_tank(app)

        start_operation_id = app.queue_fill("main_tank", 200)
        second_start_operation_id = app.queue_fill("main_tank", 200)
        stop_operation_id = app.queue_stop("main_tank")

        assert app.operations.get(start_operation_id)["status"] == "cancelled"
        assert app.operations.get(second_start_operation_id)["status"] == "cancelled"
        assert app.operations.events(start_operation_id)[-1]["detail"] == "cancelled by watering stop"
        assert app.operations.get(stop_operation_id)["status"] == "queued"
        assert [command.operation_id for command in app.queue.list()] == [stop_operation_id]


def test_watering_start_deduplicates_identical_queued_commands() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        add_confirmed_tank(app)

        first_operation_id = app.queue_fill("main_tank", 200)
        second_operation_id = app.queue_fill("main_tank", 200)

        assert second_operation_id == first_operation_id
        assert [command.operation_id for command in app.queue.list()] == [first_operation_id]
        assert [operation["operation_id"] for operation in app.operations.list_recent()] == [first_operation_id]


def test_watering_start_reuses_existing_non_terminal_start_for_tank() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        add_confirmed_tank(app)

        first_operation_id = app.queue_fill("main_tank", 200)
        second_operation_id = app.queue_fill("main_tank", 300)

        assert second_operation_id == first_operation_id
        assert [command.operation_id for command in app.queue.list()] == [first_operation_id]
        assert app.operations.detail(first_operation_id)["target_g"] == 200


def test_watering_start_reconfirms_failed_tank_config_before_fill() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        device = add_confirmed_tank(app)
        config_operation_id = app.queue_device_config(
            device,
            {"device_type": "tank", "name": "main_tank"},
            "configure main_tank",
        )
        app.queue.drop_device("main_tank")
        app.operations.event(config_operation_id, "timeout", "controller result was not received")

        fill_operation_id = app.queue_fill("main_tank", 200)

        commands = app.queue.list()
        assert [command.path for command in commands] == ["/config", "/watering/start"]
        assert commands[0].payload["device_type"] == "tank"
        assert commands[0].payload["name"] == "main_tank"
        assert commands[1].operation_id == fill_operation_id


def test_sleep_and_zero_commands_queue_separate_device_actions() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")

        sleep_enable_operation_id = app.queue_sleep("plant_1", True)
        sleep_disable_operation_id = app.queue_sleep("plant_1", False)
        sleep_interval_operation_id = app.queue_sleep_interval("plant_1", 20)
        zero_operation_id = app.queue_zero("plant_1")
        calibration_operation_id = app.queue_calibration("plant_1", 500)

        commands = app.queue.list()
        assert [command.operation_id for command in commands] == [
            sleep_enable_operation_id,
            sleep_disable_operation_id,
            sleep_interval_operation_id,
            zero_operation_id,
            calibration_operation_id,
        ]
        assert [command.path for command in commands] == ["/sleep/enable", "/sleep/disable", "/sleep/interval", "/zero", "/calibration"]
        assert commands[2].payload["minutes"] == 20
        assert commands[4].payload["weight_g"] == 500
        assert app.operations.detail(sleep_enable_operation_id)["type"] == "sleep_enable"
        assert app.operations.detail(sleep_disable_operation_id)["type"] == "sleep_disable"
        assert app.operations.detail(sleep_interval_operation_id)["type"] == "sleep_interval"
        assert app.operations.detail(zero_operation_id)["type"] == "zero_capture"
        assert app.operations.detail(calibration_operation_id)["type"] == "scale_calibration"


def test_calibration_rejects_non_positive_weight() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")

        try:
            app.queue_calibration("plant_1", 0)
        except smart_core.SmartWateringError as exc:
            assert "calibration weight must be > 0" in str(exc)
        else:
            raise AssertionError("expected SmartWateringError")


def test_sleep_command_deduplicates_identical_queued_command() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")

        first_operation_id = app.queue_sleep("plant_1", True)
        second_operation_id = app.queue_sleep("plant_1", True)

        assert second_operation_id == first_operation_id
        assert [command.operation_id for command in app.queue.list()] == [first_operation_id]


def test_sleep_interval_command_deduplicates_identical_queued_command() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")

        first_operation_id = app.queue_sleep_interval("plant_1", 20)
        second_operation_id = app.queue_sleep_interval("plant_1", 20)

        assert second_operation_id == first_operation_id
        assert [command.operation_id for command in app.queue.list()] == [first_operation_id]


def test_cli_warns_before_adding_conflicting_retryable_command(monkeypatch, capsys) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        first_operation_id = app.queue_sleep_interval("plant_1", 10)

        answers = iter(["n", "y"])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

        assert app.run(["sleep", "interval", "plant_1", "20", "--no-wait"]) == 0
        assert [command.operation_id for command in app.queue.list()] == [first_operation_id]

        assert app.run(["sleep", "interval", "plant_1", "20", "--no-wait"]) == 0
        commands = app.queue.list()
        assert len(commands) == 2
        assert commands[0].operation_id == first_operation_id
        assert commands[1].operation_id != first_operation_id
        assert [command.path for command in commands] == ["/sleep/interval", "/sleep/interval"]
        assert [command.payload["minutes"] for command in commands] == [10, 20]

        output = capsys.readouterr().out
        assert output.count("warning: retryable command already queued") == 2
        assert output.count("queued: minutes=10") == 2
        assert output.count("new: minutes=20") == 2
        assert "cancelled: new command was not queued; existing operation kept" in output


def test_cli_exact_retryable_duplicate_keeps_existing_behavior(monkeypatch, capsys) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        first_operation_id = app.queue_sleep_interval("plant_1", 20)

        def fail_on_prompt(_prompt):
            raise AssertionError("exact duplicate should not ask for confirmation")

        monkeypatch.setattr("builtins.input", fail_on_prompt)

        assert app.run(["sleep", "interval", "plant_1", "20", "--no-wait"]) == 0
        assert [command.operation_id for command in app.queue.list()] == [first_operation_id]
        assert "warning: retryable command already queued" not in capsys.readouterr().out


def test_cli_rejected_retryable_conflict_does_not_wait_for_existing_operation(monkeypatch, capsys) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        first_operation_id = app.queue_sleep_interval("plant_1", 10)
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")

        assert app.run(["sleep", "interval", "plant_1", "20"]) == 0

        output = capsys.readouterr().out
        assert [command.operation_id for command in app.queue.list()] == [first_operation_id]
        assert "cancelled: new command was not queued; existing operation kept" in output
        assert "waiting for controller result" not in output


def test_cli_warns_before_adding_conflicting_config_command(monkeypatch, capsys) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        first_operation_id = app.run(["devices", "config", "plant_1", "dry=100", "--no-wait"])
        assert first_operation_id == 0

        answers = iter(["n", "y"])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

        assert app.run(["devices", "config", "plant_1", "dry=120", "--no-wait"]) == 0
        assert len(app.queue.list()) == 1

        assert app.run(["devices", "config", "plant_1", "dry=120", "--no-wait"]) == 0
        commands = app.queue.list()
        assert len(commands) == 2
        assert [command.path for command in commands] == ["/config", "/config"]
        assert [command.payload["dry_weight_g"] for command in commands] == [100.0, 120.0]

        output = capsys.readouterr().out
        assert output.count("warning: retryable command already queued") == 2
        assert output.count("queued: device_type=plant, dry_weight_g=100.0, name=plant_1") == 2
        assert output.count("new: device_type=plant, dry_weight_g=120.0, name=plant_1") == 2
        assert "cancelled: new command was not queued; existing operation kept" in output


def test_cli_exact_config_duplicate_keeps_existing_behavior(monkeypatch, capsys) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        assert app.run(["devices", "config", "plant_1", "dry=100", "--no-wait"]) == 0
        first_operation_id = app.queue.list()[0].operation_id

        def fail_on_prompt(_prompt):
            raise AssertionError("exact duplicate should not ask for confirmation")

        monkeypatch.setattr("builtins.input", fail_on_prompt)

        assert app.run(["devices", "config", "plant_1", "dry=100", "--no-wait"]) == 0
        assert [command.operation_id for command in app.queue.list()] == [first_operation_id]
        assert "warning: retryable command already queued" not in capsys.readouterr().out


def test_cli_rejected_config_conflict_does_not_wait_for_existing_operation(monkeypatch, capsys) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        assert app.run(["devices", "config", "plant_1", "dry=100", "--no-wait"]) == 0
        first_operation_id = app.queue.list()[0].operation_id
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")

        assert app.run(["devices", "config", "plant_1", "dry=120"]) == 0

        output = capsys.readouterr().out
        assert [command.operation_id for command in app.queue.list()] == [first_operation_id]
        assert "cancelled: new command was not queued; existing operation kept" in output
        assert "waiting for controller result" not in output


def test_device_status_command_deduplicates_identical_queued_command() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")

        first_operation_id = app.queue_device_status("plant_1")
        second_operation_id = app.queue_device_status("plant_1")

        commands = app.queue.list()
        assert second_operation_id == first_operation_id
        assert [command.operation_id for command in commands] == [first_operation_id]
        assert commands[0].method == "GET"
        assert commands[0].path == "/watering"
        assert commands[0].payload is None
        operation = app.operations.detail(first_operation_id)
        assert operation["type"] == "device_status"
        assert operation["payload"]["operation_id"] == first_operation_id


def test_snapshotter_queues_status_for_registered_devices() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        app.registry.add("192.168.1.11", "plant", "plant_2")
        scheduler = snapshotter.StatusSnapshotScheduler(app, interval_sec=60)

        assert scheduler.enqueue_once() == 2
        assert scheduler.enqueue_once() == 2

        commands = app.queue.list()
        assert [command.device_name for command in commands] == ["plant_1", "plant_2"]
        assert [command.path for command in commands] == ["/watering", "/watering"]


def test_snapshotter_uses_interval_from_env(monkeypatch) -> None:
    monkeypatch.setenv(snapshotter.SNAPSHOT_INTERVAL_SEC_ENV, "42")

    assert snapshotter.resolve_snapshot_interval_sec() == 42


def test_snapshotter_rejects_invalid_interval(monkeypatch) -> None:
    monkeypatch.setenv(snapshotter.SNAPSHOT_INTERVAL_SEC_ENV, "0")

    try:
        snapshotter.resolve_snapshot_interval_sec()
    except smart_core.SmartWateringError as exc:
        assert snapshotter.SNAPSHOT_INTERVAL_SEC_ENV in str(exc)
    else:
        raise AssertionError("expected SmartWateringError")


def test_worker_idle_interval_uses_env(monkeypatch) -> None:
    monkeypatch.setenv(smart_core.NODE_WORKER_IDLE_INTERVAL_SEC_ENV, "1")

    assert smart_core.resolve_node_worker_idle_interval_sec() == 1


def test_worker_idle_interval_rejects_invalid_env(monkeypatch) -> None:
    monkeypatch.setenv(smart_core.NODE_WORKER_IDLE_INTERVAL_SEC_ENV, "0")

    try:
        smart_core.resolve_node_worker_idle_interval_sec()
    except smart_core.SmartWateringError as exc:
        assert smart_core.NODE_WORKER_IDLE_INTERVAL_SEC_ENV in str(exc)
    else:
        raise AssertionError("expected SmartWateringError")


def test_worker_retry_settings_use_env(monkeypatch) -> None:
    monkeypatch.setenv(smart_core.WORKER_RETRY_INTERVAL_SEC_ENV, "7")
    monkeypatch.setenv(smart_core.WORKER_MAX_WAIT_SEC_ENV, "1800")

    assert smart_core.resolve_worker_retry_interval_sec() == 7
    assert smart_core.resolve_worker_max_wait_sec() == 1800


def test_worker_retry_settings_reject_invalid_env(monkeypatch) -> None:
    monkeypatch.setenv(smart_core.WORKER_RETRY_INTERVAL_SEC_ENV, "0")

    try:
        smart_core.resolve_worker_retry_interval_sec()
    except smart_core.SmartWateringError as exc:
        assert smart_core.WORKER_RETRY_INTERVAL_SEC_ENV in str(exc)
    else:
        raise AssertionError("expected SmartWateringError")


def test_stop_cancels_active_retrying_watering_start() -> None:
    class RoutingApi:
        def request_json(self, base_url, path, method, payload=None):
            if path == "/watering/start":
                raise smart_core.RetryableDeviceApiError("device sleeping")
            return {"status": "accepted"}

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        add_confirmed_tank(app)
        start_operation_id = app.queue_fill("main_tank", 200)
        worker = smart_core.BackgroundWorker(
            RoutingApi(),
            app.queue,
            app.operations,
            smart_core.WorkerState(str(Path(temp_dir) / "worker.pid")),
            retry_interval_sec=0.01,
            max_wait_sec=5,
        )
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()

        deadline = time.time() + 1
        while time.time() < deadline:
            if app.operations.get(start_operation_id)["status"] == "sending":
                break
            time.sleep(0.01)
        assert app.operations.get(start_operation_id)["status"] == "sending"

        stop_operation_id = app.queue_stop("main_tank")
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert app.operations.get(start_operation_id)["status"] == "cancelled"
        assert app.operations.get(stop_operation_id)["status"] == "accepted"
        assert app.queue.list() == []


def test_stop_reaches_controller_when_watering_is_active() -> None:
    class ActiveWateringApi:
        def __init__(self) -> None:
            self.active = False
            self.requests: list[str] = []

        def request_json(self, base_url, path, method, payload=None):
            self.requests.append(path)
            if path == "/watering/start":
                self.active = True
                return {"status": "accepted"}
            if path == "/watering/stop":
                assert self.active is True
                self.active = False
                return {"status": "stop_requested"}
            return {}

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        add_confirmed_tank(app)
        api = ActiveWateringApi()
        worker = smart_core.BackgroundWorker(
            api,
            app.queue,
            app.operations,
            smart_core.WorkerState(str(Path(temp_dir) / "worker.pid")),
            retry_interval_sec=0,
            max_wait_sec=1,
        )

        start_operation_id = app.queue_fill("main_tank", 200)
        worker.run()
        assert api.active is True
        assert app.operations.get(start_operation_id)["status"] == "accepted"

        stop_operation_id = app.queue_stop("main_tank")
        worker.run()

        assert api.active is False
        assert api.requests == ["/watering/start", "/watering/stop"]
        assert app.operations.get(start_operation_id)["status"] == "cancelled"
        assert app.operations.get(stop_operation_id)["status"] == "accepted"


def test_stop_when_watering_is_not_active_is_idempotent_success() -> None:
    class InactiveWateringApi:
        def __init__(self, operations: smart_core.OperationLog) -> None:
            self.operations = operations
            self.requests = 0

        def request_json(self, base_url, path, method, payload=None):
            self.requests += 1
            self.operations.event(payload["operation_id"], "error", "watering_not_active")
            raise smart_core.DeviceHttpError(409, path, '{"error":"watering_not_active"}')

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        add_confirmed_tank(app)
        api = InactiveWateringApi(app.operations)
        worker = smart_core.BackgroundWorker(
            api,
            app.queue,
            app.operations,
            smart_core.WorkerState(str(Path(temp_dir) / "worker.pid")),
            retry_interval_sec=0,
            max_wait_sec=1,
        )

        stop_operation_id = app.queue_stop("main_tank")
        worker.run()

        assert api.requests == 1
        assert app.queue.list() == []
        assert app.operations.get(stop_operation_id)["status"] == "success"
        events = app.operations.events(stop_operation_id)
        assert [event["status"] for event in events] == [
            "queued",
            "sending",
            "success",
        ]
        assert events[-1]["detail"] == "no active watering"


def test_watering_start_firmware_failure_is_terminal_error() -> None:
    class FailingStartApi:
        def request_json(self, base_url, path, method, payload=None):
            raise smart_core.DeviceHttpError(500, path, '{"error":"task_create_failed"}')

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        add_confirmed_tank(app)
        worker = smart_core.BackgroundWorker(
            FailingStartApi(),
            app.queue,
            app.operations,
            smart_core.WorkerState(str(Path(temp_dir) / "worker.pid")),
            retry_interval_sec=0,
            max_wait_sec=10,
        )

        operation_id = app.queue_fill("main_tank", 200)
        worker.run()

        assert app.queue.list() == []
        assert app.operations.get(operation_id)["status"] == "error"
        assert app.operations.events(operation_id)[-1]["detail"] == "task_create_failed"


def test_sleep_disable_retries_until_controller_wakes() -> None:
    class SleepingThenAwakeApi:
        def __init__(self) -> None:
            self.requests = 0

        def request_json(self, base_url, path, method, payload=None):
            assert method == "POST"
            assert path == "/sleep/disable"
            self.requests += 1
            if self.requests == 1:
                raise smart_core.RetryableDeviceApiError("device sleeping")
            return {"status": "sleep_disabled"}

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        api = SleepingThenAwakeApi()
        worker = smart_core.BackgroundWorker(
            api,
            app.queue,
            app.operations,
            smart_core.WorkerState(str(Path(temp_dir) / "worker.pid")),
            retry_interval_sec=0,
            max_wait_sec=1,
        )

        operation_id = app.queue_sleep("plant_1", False)
        worker.run()

        assert api.requests == 2
        assert app.queue.list() == []
        assert app.operations.get(operation_id)["status"] == "accepted"
        assert [event["status"] for event in app.operations.events(operation_id)] == [
            "queued",
            "sending",
            "accepted",
        ]


def test_sleep_interval_retries_until_controller_wakes() -> None:
    class SleepingThenAwakeApi:
        def __init__(self) -> None:
            self.requests = 0

        def request_json(self, base_url, path, method, payload=None):
            assert method == "POST"
            assert path == "/sleep/interval"
            assert payload["minutes"] == 20
            self.requests += 1
            if self.requests == 1:
                raise smart_core.RetryableDeviceApiError("device sleeping")
            return {"status": "sleep_interval_updated"}

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        api = SleepingThenAwakeApi()
        worker = smart_core.BackgroundWorker(
            api,
            app.queue,
            app.operations,
            smart_core.WorkerState(str(Path(temp_dir) / "worker.pid")),
            retry_interval_sec=0,
            max_wait_sec=1,
        )

        operation_id = app.queue_sleep_interval("plant_1", 20)
        worker.run()

        assert api.requests == 2
        assert app.queue.list() == []
        assert app.operations.get(operation_id)["status"] == "accepted"
        assert [event["status"] for event in app.operations.events(operation_id)] == [
            "queued",
            "sending",
            "accepted",
        ]


def test_worker_stores_device_status_result() -> None:
    class StatusApi:
        def request_json(self, base_url, path, method, payload=None):
            assert method == "GET"
            assert path == "/watering"
            return {
                "device": {"name": "plant_1", "type": "plant"},
                "watering": {"active": False, "state": "waiting"},
                "config": {"sleep_interval_min": 20},
                "weight": {"useful_weight_g": 42.0},
            }

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        worker = smart_core.BackgroundWorker(
            StatusApi(),
            app.queue,
            app.operations,
            smart_core.WorkerState(str(Path(temp_dir) / "worker.pid")),
            retry_interval_sec=0,
            max_wait_sec=1,
        )

        operation_id = app.queue_device_status("plant_1")
        worker.run()

        operation = app.operations.detail(operation_id)
        assert operation["status"] == "success"
        assert operation["result"]["device"]["name"] == "plant_1"
        assert operation["result"]["config"]["sleep_interval_min"] == 20
        assert isinstance(operation["result_received_at"], float)
        assert [event["status"] for event in app.operations.events(operation_id)] == ["queued", "sending", "success"]


def test_operation_status_does_not_regress_after_terminal_event() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        operations = smart_core.OperationLog(make_store(temp_dir))
        operation_id = operations.create("main_tank", "watering_start", {"target_g": 200})

        operations.event(operation_id, "sending", "worker picked operation")
        operations.event(operation_id, "success", "target_reached")
        operations.event(operation_id, "accepted", "late accepted response")

        assert operations.get(operation_id)["status"] == "success"
        assert [event["status"] for event in operations.events(operation_id)] == ["queued", "sending", "success"]


def test_format_status_includes_device_type_and_dry_weight() -> None:
    payload = {
        "device": {"type": "plant", "name": "plant_1"},
        "watering": {
            "state": "waiting",
            "active": False,
            "last_operation_type": "config",
            "last_operation_status": "completed",
        },
        "config": {
            "target_g": 0.0,
            "tare_weight_g": 450.0,
            "dry_weight_g": 120.0,
            "zero_raw": -123456,
            "raw_per_gram": 214.5,
            "sleep_interval_min": 20,
        },
        "weight": {
            "useful_weight_g": 83.0,
            "gross_weight_g": 533.0,
            "water_used_g": 0.0,
        },
    }

    text = smart_cli.SmartWateringCliApp.format_status(payload)

    assert "device: plant_1 (plant)" in text
    assert "dry weight: 120.0 g" in text
    assert "tare weight: 450.0 g" in text
    assert "zero raw: -123456" in text
    assert "raw per gram: 214.500" in text
    assert "sleep interval: 20 min" in text


def test_format_constants_outputs_sorted_json() -> None:
    text = smart_cli.SmartWateringCliApp.format_constants({
        "runtime": {"zero_raw": -123456, "raw_per_gram": 214.5},
        "weight": {"default_raw_per_gram": 214.0},
    })

    assert json.loads(text) == {
        "runtime": {"zero_raw": -123456, "raw_per_gram": 214.5},
        "weight": {"default_raw_per_gram": 214.0},
    }
    assert text.startswith("{\n  \"runtime\"")


def test_interactive_menu_can_exit(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.interactive_message_delay_sec = 0
        monkeypatch.setattr("builtins.input", lambda _prompt: "0")

        assert app.run_interactive() == 0
        menu = app.format_main_menu()
        assert "Devices" in menu
        assert "Watering" in menu
        assert "7. Show device constants" in menu
        assert "8. Start watering" in menu
        assert "9. Stop watering" in menu
        assert "10. Enable sleep" in menu
        assert "12. Set sleep interval" in menu
        assert "13. Set zero" in menu
        assert "14. Calibrate scale" in menu
        assert "16. Clear device queue" in menu
        assert "19. Add API user" in menu
        assert "21. Drop API user" in menu
        assert "22. Sync detected watering history" in menu
        assert "23. Hard drop detected watering history" in menu


def test_interactive_can_sync_detected_watering_history(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.interactive_message_delay_sec = 0
        calls = []
        monkeypatch.setattr(
            app,
            "sync_detected_watering_history",
            lambda days, device_name=None: calls.append((days, device_name)),
        )
        answers = iter(["22", "30", "", "", "0"])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

        assert app.run_interactive() == 0
        assert calls == [(30, None)]


def test_interactive_can_hard_drop_all_detected_watering_history(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.interactive_message_delay_sec = 0
        calls = []
        monkeypatch.setattr(
            app,
            "hard_drop_detected_watering_history",
            lambda device_name=None: calls.append(device_name),
        )
        answers = iter(["23", "", "DROP ALL", "", "0"])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

        assert app.run_interactive() == 0
        assert calls == [None]


def test_interactive_can_show_constants(monkeypatch, capsys) -> None:
    class FakeConstantsApi:
        def request_json(self, base_url, path, method, payload=None):
            assert base_url == "http://192.168.1.10"
            assert path == "/constants"
            assert method == "GET"
            assert payload is None
            return {"runtime": {"zero_raw": -123456, "raw_per_gram": 214.5}}

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.interactive_message_delay_sec = 0
        app.registry.add("192.168.1.10", "plant", "plant_1")
        app.api = FakeConstantsApi()
        answers = iter(["7", "1", "", "0"])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

        assert app.run_interactive() == 0

        output = capsys.readouterr().out
        assert "\"raw_per_gram\": 214.5" in output


def test_cli_adds_public_api_user() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))

        assert app.run(["users", "add", "api_user", "--password", "secret-password"]) == 0

        assert app.auth.verify_password("api_user", "secret-password")


def test_cli_drops_public_api_user() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.auth.add_user("api_user", "secret-password")

        assert app.run(["users", "drop", "api_user"]) == 0

        assert app.auth.list_users() == []
        assert not app.auth.verify_password("api_user", "secret-password")


def test_cli_queues_sleep_and_zero_commands() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")

        assert app.run(["sleep", "disable", "plant_1", "--no-wait"]) == 0
        assert app.run(["sleep", "enable", "plant_1", "--no-wait"]) == 0
        assert app.run(["sleep", "interval", "plant_1", "20", "--no-wait"]) == 0
        assert app.run(["zero", "plant_1", "--no-wait"]) == 0
        assert app.run(["calibration", "plant_1", "500", "--no-wait"]) == 0

        assert [command.path for command in app.queue.list()] == [
            "/sleep/disable",
            "/sleep/enable",
            "/sleep/interval",
            "/zero",
            "/calibration",
        ]


def test_cli_reads_constants(capsys) -> None:
    class FakeConstantsApi:
        def request_json(self, base_url, path, method, payload=None):
            assert base_url == "http://192.168.1.10"
            assert path == "/constants"
            assert method == "GET"
            assert payload is None
            return {
                "runtime": {"zero_raw": -123456, "raw_per_gram": 214.5},
                "weight": {"default_raw_per_gram": 214.0},
            }

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        app.api = FakeConstantsApi()

        assert app.run(["constants", "plant_1"]) == 0

        output = json.loads(capsys.readouterr().out)
        assert output["runtime"] == {"zero_raw": -123456, "raw_per_gram": 214.5}


def test_cli_waits_for_operation_success(capsys) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.operation_wait_timeout_sec = 1
        app.operation_poll_interval_sec = 0.01
        operation_id = app.operations.create("plant_1", "config", {"name": "plant_1"})

        def complete_operation() -> None:
            time.sleep(0.02)
            app.operations.event(operation_id, "success", "config_updated")

        thread = threading.Thread(target=complete_operation)
        thread.start()
        try:
            assert app.report_operation(operation_id) is True
        finally:
            thread.join(timeout=1)

        output = capsys.readouterr().out
        assert "waiting for controller result" in output
        assert "success: device_config on plant_1 (config_updated)" in output


def test_cli_rejects_sleep_interval_over_50_minutes(capsys) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")

        assert app.run(["sleep", "interval", "plant_1", "51"]) == 1

        output = capsys.readouterr()
        assert "sleep interval minutes must be <= 50" in output.err
        assert app.queue.list() == []


def test_cli_pings_device(capsys) -> None:
    class HealthApi:
        def request_text(self, base_url, path, method, payload=None):
            assert path == "/healthz"
            assert method == "GET"
            return "ok"

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        app.api = HealthApi()

        assert app.run(["ping", "plant_1"]) == 0

        output = capsys.readouterr()
        assert "plant_1: online" in output.out


def test_cli_add_discovers_device_without_writing_config(capsys) -> None:
    class OnlineApi:
        def request_json(self, base_url, path, method, payload=None):
            assert (base_url, path, method) == ("http://192.168.1.10", "/watering", "GET")
            return {
                "device": {"type": "tank", "name": "remote_tank"},
                "config": {
                    "dry_weight_g": 120,
                    "wet_weight_g": 510,
                    "watering_loss_threshold_percent": 35,
                },
            }

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.api = OnlineApi()

        assert app.run(["devices", "add", "192.168.1.10", "local_name", "--type", "plant"]) == 0

        output = capsys.readouterr()
        assert "name: remote_tank" in output.out
        assert "device_type: tank" in output.out
        assert "address: 192.168.1.10" in output.out
        assert "registered without changing device config" in output.out
        assert app.registry.get("remote_tank").device_type == "tank"
        settings = app.registry.watering_settings("remote_tank")
        assert settings["dry_weight_g"] == 120
        assert settings["wet_weight_g"] == 510
        assert settings["watering_loss_threshold_percent"] == 35
        assert settings["dry_weight_updated_at"] is not None
        assert settings["wet_weight_updated_at"] is not None
        assert settings["watering_loss_threshold_updated_at"] is not None
        assert "imported watering settings" in output.out
        assert app.queue.list() == []


def test_interactive_add_does_not_prompt_for_config_when_device_responds(monkeypatch, capsys) -> None:
    class OnlineApi:
        def request_json(self, base_url, path, method, payload=None):
            return {"device": {"type": "plant", "name": "balcony"}}

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.api = OnlineApi()
        answers = iter(["192.168.1.12"])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

        app.interactive_register_device()

        assert app.registry.get("balcony").ip == "192.168.1.12"
        assert app.queue.list() == []
        assert "registered without changing device config" in capsys.readouterr().out


def test_cli_add_offline_device_requires_confirmation(monkeypatch, capsys) -> None:
    class OfflineApi:
        def request_json(self, base_url, path, method, payload=None):
            raise smart_core.RetryableDeviceApiError("device sleeping")

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.api = OfflineApi()
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")

        assert app.run(["devices", "add", "192.168.1.10", "plant_1"]) == 0

        output = capsys.readouterr().out
        assert "device is not powered on or is sleeping" in output
        assert "device_type and name" in output
        assert "cancelled" in output
        assert app.registry.list() == []
        assert app.queue.list() == []


def test_cli_add_offline_device_continues_after_confirmation(monkeypatch, capsys) -> None:
    class OfflineApi:
        def request_json(self, base_url, path, method, payload=None):
            raise smart_core.RetryableDeviceApiError("device sleeping")

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.api = OfflineApi()
        monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

        assert app.run(["devices", "add", "192.168.1.10", "plant_1", "--no-wait"]) == 0

        commands = app.queue.list()
        assert len(commands) == 1
        assert commands[0].path == "/config"
        assert commands[0].payload["device_type"] == "plant"
        assert commands[0].payload["name"] == "plant_1"
        assert "registered: plant_1 (plant) 192.168.1.10" in capsys.readouterr().out


def test_cli_can_defer_offline_device_discovery_to_worker(capsys) -> None:
    class SleepingThenOnlineApi:
        def __init__(self) -> None:
            self.requests = 0

        def request_json(self, base_url, path, method, payload=None):
            self.requests += 1
            if self.requests <= 2:
                raise smart_core.RetryableDeviceApiError("device sleeping")
            return {
                "device": {"type": "plant", "name": "sleepy_plant"},
                "config": {
                    "dry_weight_g": 140,
                    "wet_weight_g": 520,
                    "watering_loss_threshold_percent": 30,
                },
            }

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.api = SleepingThenOnlineApi()

        assert app.run([
            "devices", "add", "192.168.1.14", "--when-offline", "defer"
        ]) == 0
        assert app.registry.list() == []
        assert len(app.queue.list()) == 1

        worker = smart_core.BackgroundWorker(
            app.api,
            app.queue,
            app.operations,
            smart_core.WorkerState(str(Path(temp_dir) / "worker.pid")),
            retry_interval_sec=0,
            max_wait_sec=1,
        )
        worker.run()

        device = app.registry.get("sleepy_plant")
        settings = app.registry.watering_settings(device.name)
        assert device.ip == "192.168.1.14"
        assert settings["dry_weight_g"] == 140
        assert settings["wet_weight_g"] == 520
        assert settings["watering_loss_threshold_percent"] == 30
        assert app.queue.list() == []
        assert "queued discovery" in capsys.readouterr().out


def test_deferred_discovery_uses_normal_worker_timeout() -> None:
    class SleepingApi:
        def request_json(self, base_url, path, method, payload=None):
            raise smart_core.RetryableDeviceApiError("device sleeping")

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.api = SleepingApi()
        operation_id = app.queue_device_discovery("192.168.1.15")
        worker = smart_core.BackgroundWorker(
            app.api,
            app.queue,
            app.operations,
            smart_core.WorkerState(str(Path(temp_dir) / "worker.pid")),
            retry_interval_sec=0,
            max_wait_sec=0,
        )

        worker.run()

        assert app.operations.detail(operation_id)["status"] == "timeout"
        assert app.queue.list() == []
        assert app.registry.list() == []


def test_explicit_offline_configuration_supersedes_pending_discovery(capsys) -> None:
    class SleepingApi:
        def request_json(self, base_url, path, method, payload=None):
            raise smart_core.RetryableDeviceApiError("device sleeping")

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.api = SleepingApi()
        discovery_id = app.queue_device_discovery("192.168.1.16")

        assert app.run([
            "devices", "add", "192.168.1.16", "replacement", "--type", "plant",
            "--when-offline", "configure", "--no-wait",
        ]) == 0

        commands = app.queue.list()
        assert len(commands) == 1
        assert commands[0].path == "/config"
        assert commands[0].device_name == "replacement"
        assert app.operations.detail(discovery_id)["status"] == "cancelled"
        assert "cancelled pending discovery" in capsys.readouterr().out


def test_interactive_device_action_reports_missing_devices(monkeypatch, capsys) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.interactive_message_delay_sec = 0
        answers = iter(["5", "0"])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

        assert app.run_interactive() == 0

        output = capsys.readouterr().out
        assert "error: no registered devices" in output


def test_worker_records_all_failed_send_stages() -> None:
    class SleepingApi:
        def __init__(self) -> None:
            self.attempts = 0

        def request_json(self, base_url, path, method, payload=None):
            self.attempts += 1
            raise smart_core.RetryableDeviceApiError("device sleeping")

    with tempfile.TemporaryDirectory() as temp_dir:
        store = make_store(temp_dir)
        operations = smart_core.OperationLog(store)
        queue = smart_core.CommandQueue(store)
        state = smart_core.WorkerState(str(Path(temp_dir) / "worker.pid"))
        operation_id = operations.create("plant_1", "config", {"name": "plant_1"})
        queue.enqueue(
            operation_id,
            "plant_1",
            "http://device",
            "/config",
            "POST",
            {"name": "plant_1"},
            "configure plant_1",
        )
        api = SleepingApi()
        worker = smart_core.BackgroundWorker(
            api,
            queue,
            operations,
            state,
            retry_interval_sec=0,
            max_wait_sec=0,
        )

        assert worker.run() == 0

        events = operations.events(operation_id)
        assert [event["status"] for event in events] == ["queued", "sending", "timeout"]
        assert "device did not respond" in events[-1]["detail"]
        assert operations.get(operation_id)["status"] == "timeout"
        assert queue.list() == []
        assert api.attempts == 1


def test_per_device_worker_does_not_block_other_devices() -> None:
    class RoutingApi:
        def __init__(self) -> None:
            self.plant_1_online = threading.Event()
            self.sent_base_urls: list[str] = []
            self.lock = threading.Lock()

        def request_json(self, base_url, path, method, payload=None):
            if base_url == "http://plant-1" and not self.plant_1_online.is_set():
                raise smart_core.RetryableDeviceApiError("device sleeping")
            with self.lock:
                self.sent_base_urls.append(base_url)
            return {}

    with tempfile.TemporaryDirectory() as temp_dir:
        store = make_store(temp_dir)
        operations = smart_core.OperationLog(store)
        queue = smart_core.CommandQueue(store)
        state = smart_core.WorkerState(str(Path(temp_dir) / "worker.pid"))
        plant_1_operation_id = operations.create("plant_1", "watering_start", {"target_g": 100})
        plant_2_operation_id = operations.create("plant_2", "config", {"name": "plant_2"})
        queue.enqueue(
            plant_1_operation_id,
            "plant_1",
            "http://plant-1",
            "/watering/start",
            "POST",
            {"target_g": 100},
            "fill plant_1 100.0 g",
        )
        queue.enqueue(
            plant_2_operation_id,
            "plant_2",
            "http://plant-2",
            "/config",
            "POST",
            {"name": "plant_2"},
            "configure plant_2",
        )
        api = RoutingApi()
        supervisor = smart_core.DeviceWorkerSupervisor(
            api,
            queue,
            operations,
            state,
            retry_interval_sec=0.01,
            max_wait_sec=5,
        )

        supervisor.start_pending_workers()
        deadline = time.time() + 1
        while time.time() < deadline:
            with api.lock:
                if "http://plant-2" in api.sent_base_urls:
                    break
            time.sleep(0.01)

        try:
            with api.lock:
                assert "http://plant-2" in api.sent_base_urls
            deadline = time.time() + 1
            while time.time() < deadline:
                if [command.device_name for command in queue.list()] == ["plant_1"]:
                    break
                time.sleep(0.01)
            assert [command.device_name for command in queue.list()] == ["plant_1"]
        finally:
            api.plant_1_online.set()
            supervisor.wait_for_idle()

        assert queue.list() == []


def test_retryable_watering_start_moves_behind_same_device_queue() -> None:
    class RoutingApi:
        def __init__(self) -> None:
            self.paths: list[str] = []

        def request_json(self, base_url, path, method, payload=None):
            self.paths.append(path)
            if path == "/watering/start":
                raise smart_core.RetryableDeviceApiError("device sleeping")
            return {}

    with tempfile.TemporaryDirectory() as temp_dir:
        store = make_store(temp_dir)
        operations = smart_core.OperationLog(store)
        queue = smart_core.CommandQueue(store)
        state = smart_core.WorkerState(str(Path(temp_dir) / "worker.pid"))
        start_operation_id = operations.create("plant_1", "watering_start", {"target_g": 100})
        config_operation_id = operations.create("plant_1", "config", {"name": "plant_1"})
        queue.enqueue(
            start_operation_id,
            "plant_1",
            "http://plant-1",
            "/watering/start",
            "POST",
            {"target_g": 100},
            "fill plant_1 100.0 g",
        )
        queue.enqueue(
            config_operation_id,
            "plant_1",
            "http://plant-1",
            "/config",
            "POST",
            {"name": "plant_1"},
            "configure plant_1",
        )
        api = RoutingApi()
        worker = smart_core.BackgroundWorker(
            api,
            queue,
            operations,
            state,
            retry_interval_sec=0.01,
            max_wait_sec=0.03,
        )

        assert worker.run() == 0

        assert api.paths[:2] == ["/watering/start", "/config"]
        assert operations.get(config_operation_id)["status"] == "accepted"
        assert operations.get(start_operation_id)["status"] == "timeout"
        assert [event["status"] for event in operations.events(start_operation_id)] == [
            "queued",
            "sending",
            "timeout",
        ]
        assert queue.list() == []


def test_worker_records_invalid_device_url_as_operation_error() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = make_store(temp_dir)
        operations = smart_core.OperationLog(store)
        queue = smart_core.CommandQueue(store)
        state = smart_core.WorkerState(str(Path(temp_dir) / "worker.pid"))
        operation_id = operations.create("plant_1", "config", {"name": "plant_1"})
        queue.enqueue(
            operation_id,
            "plant_1",
            "http://ю",
            "/config",
            "POST",
            {"name": "plant_1"},
            "configure plant_1",
        )
        worker = smart_core.BackgroundWorker(
            smart_core.DeviceApiClient(timeout_sec=1),
            queue,
            operations,
            state,
            retry_interval_sec=0,
            max_wait_sec=0,
        )

        assert worker.run() == 0

        events = operations.events(operation_id)
        assert [event["status"] for event in events] == ["queued", "sending", "error"]
        assert "invalid URL" in events[-1]["detail"]
        assert queue.list() == []


def test_watering_commands_require_tank_device() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")

        for action in (lambda: app.queue_fill("plant_1", 100), lambda: app.queue_stop("plant_1")):
            try:
                action()
            except smart_core.SmartWateringError as exc:
                assert "tank devices" in str(exc)
            else:
                raise AssertionError("expected SmartWateringError")


def test_operation_history_stores_payload_sent_to_controller(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setenv(smart_core.NODE_URL_ENV, "http://node.example:8080")
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        add_confirmed_tank(app, "192.168.1.10")

        operation_id = app.queue_fill("main_tank", 200)

        operation = app.operations.get(operation_id)
        assert operation is not None
        stored_payload = json.loads(operation["payload_json"])
        assert stored_payload == app.queue.list()[0].payload
        assert stored_payload["operation_id"] == operation_id
        assert stored_payload["callback_url"] == "http://node.example:8080/operations/callback"
        assert stored_payload["target_g"] == 200


def test_loopback_node_url_detection() -> None:
    assert callback_utils.is_loopback_node_url("http://localhost:8080")
    assert callback_utils.is_loopback_node_url("http://127.0.0.1:8080")
    assert not callback_utils.is_loopback_node_url("http://192.168.1.10:8080")


def test_normalize_operation_status_uses_simple_lifecycle() -> None:
    assert callback_utils.normalize_operation_status("queued") == "queued"
    assert callback_utils.normalize_operation_status("received") == "accepted"
    assert callback_utils.normalize_operation_status("accepted") == "accepted"
    assert callback_utils.normalize_operation_status("started") == "running"
    assert callback_utils.normalize_operation_status("completed") == "success"
    assert callback_utils.normalize_operation_status("failed") == "error"


def test_callback_received_ack_does_not_mark_operation_error() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        operations = smart_core.OperationLog(make_store(temp_dir))
        operation_id = operations.create("main_tank", "watering_start", {"target_g": 200})

        operations.event(operation_id, "sending", "worker picked operation")
        operations.event(operation_id, callback_utils.normalize_operation_status("received"), "accepted")

        operation = operations.detail(operation_id)
        assert operation["status"] == "accepted"
        assert operation["error"] is None


def test_misclassified_error_accepted_event_is_reported_as_ack() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        operations = smart_core.OperationLog(make_store(temp_dir))
        operation_id = operations.create("main_tank", "watering_start", {"target_g": 200})

        operations.event(operation_id, "error", "accepted")

        operation = operations.detail(operation_id)
        assert operation["status"] == "accepted"
        assert operation["error"] is None

        operations.event(operation_id, "success", "target_reached")
        operation = operations.detail(operation_id)
        assert operation["status"] == "success"
        assert operation["error"] is None


def test_worker_does_not_mark_success_without_callback() -> None:
    class AcceptedApi:
        def request_json(self, base_url, path, method, payload=None):
            return {"status": "accepted"}

    with tempfile.TemporaryDirectory() as temp_dir:
        store = make_store(temp_dir)
        operations = smart_core.OperationLog(store)
        queue = smart_core.CommandQueue(store)
        state = smart_core.WorkerState(str(Path(temp_dir) / "worker.pid"))
        operation_id = operations.create("plant_1", "config", {"name": "plant_1"})
        queue.enqueue(
            operation_id,
            "plant_1",
            "http://device",
            "/config",
            "POST",
            {"name": "plant_1"},
            "configure plant_1",
        )
        worker = smart_core.BackgroundWorker(
            AcceptedApi(),
            queue,
            operations,
            state,
            retry_interval_sec=0,
            max_wait_sec=0,
        )

        assert worker.run() == 0

        events = operations.events(operation_id)
        assert [event["status"] for event in events] == ["queued", "sending", "accepted"]
        assert queue.list() == []
        assert operations.list_recent(1)[0]["status"] == "accepted"


def test_worker_retries_config_command_after_retryable_error() -> None:
    class RetryThenAcceptedApi:
        def __init__(self):
            self.calls = 0

        def request_json(self, base_url, path, method, payload=None):
            self.calls += 1
            if self.calls == 1:
                raise smart_core.RetryableDeviceApiError("device sleeping")
            return {"status": "accepted"}

    with tempfile.TemporaryDirectory() as temp_dir:
        store = make_store(temp_dir)
        operations = smart_core.OperationLog(store)
        queue = smart_core.CommandQueue(store)
        state = smart_core.WorkerState(str(Path(temp_dir) / "worker.pid"))
        operation_id = operations.create("plant_1", "config", {"name": "plant_1"})
        queue.enqueue(
            operation_id,
            "plant_1",
            "http://device",
            "/config",
            "POST",
            {"name": "plant_1"},
            "configure plant_1",
        )
        api = RetryThenAcceptedApi()
        worker = smart_core.BackgroundWorker(
            api,
            queue,
            operations,
            state,
            retry_interval_sec=0,
            max_wait_sec=30,
        )

        assert worker.run() == 0

        assert api.calls == 2
        assert [event["status"] for event in operations.events(operation_id)] == ["queued", "sending", "accepted"]
        assert queue.list() == []


def test_worker_supervisor_times_out_accepted_operation_without_callback() -> None:
    class AcceptedApi:
        def request_json(self, base_url, path, method, payload=None):
            return {"status": "accepted"}

    with tempfile.TemporaryDirectory() as temp_dir:
        store = make_store(temp_dir)
        operations = smart_core.OperationLog(store)
        queue = smart_core.CommandQueue(store)
        state = smart_core.WorkerState(str(Path(temp_dir) / "worker.pid"))
        operation_id = operations.create("plant_1", "config", {"name": "plant_1"})
        queue.enqueue(
            operation_id,
            "plant_1",
            "http://device",
            "/config",
            "POST",
            {"name": "plant_1"},
            "configure plant_1",
        )
        supervisor = smart_core.DeviceWorkerSupervisor(
            AcceptedApi(),
            queue,
            operations,
            state,
            retry_interval_sec=0,
            max_wait_sec=0,
        )

        supervisor.start_pending_workers()
        supervisor.wait_for_idle()
        assert operations.get(operation_id)["status"] == "accepted"

        supervisor.start_pending_workers()

        events = operations.events(operation_id)
        assert [event["status"] for event in events] == ["queued", "sending", "accepted", "timeout"]
        assert "controller result was not received within 0s" in events[-1]["detail"]
        assert operations.get(operation_id)["status"] == "timeout"


def test_callback_access_log_includes_request_status_and_detail(capsys) -> None:
    class Operations:
        def get(self, operation_id):
            return {"device_name": "plant", "operation_type": "fill"}

        def event(self, operation_id, status, detail):
            pass

    client = TestClient(
        create_callback_app(CallbackService(Operations())),
        client=("192.168.1.20", 12345),
    )
    response = client.post(
        "/operations/callback",
        json={"operation_id": "op-1", "status": "success"},
    )
    assert response.status_code == 200
    output = capsys.readouterr().out
    assert '192.168.1.20 "POST /operations/callback" 200' in output
    assert "operation_id=op-1" in output
    assert "status=success" in output


def test_callback_log_includes_operation_context(capsys) -> None:
    class Operations:
        def __init__(self) -> None:
            self.events = []

        def get(self, operation_id):
            assert operation_id == "op-1"
            return {
                "operation_id": "op-1",
                "device_name": "main_tank",
                "operation_type": "fill",
            }

        def event(self, operation_id, status, detail):
            self.events.append((operation_id, status, detail))

    operations = Operations()
    client = TestClient(
        create_callback_app(CallbackService(operations)),
        client=("192.168.1.22", 12345),
    )
    response = client.post(
        "/operations/callback",
        json={
            "operation_id": "op-1",
            "status": "failed",
            "detail": "pump start failed",
        },
    )

    assert response.status_code == 200
    assert operations.events == [("op-1", "error", "pump start failed")]
    output = capsys.readouterr().out
    assert '192.168.1.22 "POST /operations/callback" 200' in output
    assert "operation_id=op-1" in output
    assert "device=main_tank" in output
    assert "operation_type=fill" in output
    assert "status=error" in output
    assert 'detail="pump start failed"' in output


def test_callback_healthz_logs_access(capsys) -> None:
    class Operations:
        pass

    client = TestClient(
        create_callback_app(CallbackService(Operations())),
        client=("192.168.1.21", 12345),
    )
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    output = capsys.readouterr().out
    assert '192.168.1.21 "GET /healthz" 200' in output
    assert "healthz" in output


def test_public_api_jwt_roundtrip() -> None:
    token = security.create_jwt("secret", "client", ttl_sec=60)

    payload = security.verify_jwt(token, "secret")

    assert payload["sub"] == "client"


def test_public_api_healthz_logs_access(capsys) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        client = TestClient(
            create_app(
                ApiRuntime(
                    app,
                    config.ApiSettings(
                        jwt_secret="secret",
                        session_ttl_sec=3600,
                        google_web_client_id="",
                        google_allowed_emails=set(),
                        google_allowed_domains=set(),
                        prometheus_url=config.DEFAULT_PROMETHEUS_URL,
                        statistics_timezone=ZoneInfo("Europe/Berlin"),
                    ),
                )
            ),
            client=("192.168.1.31", 12345),
        )
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    output = capsys.readouterr().out
    assert '192.168.1.31 "GET /healthz" 200' in output


def test_public_api_default_session_ttl_is_one_hour(monkeypatch) -> None:
    monkeypatch.delenv(config.PUBLIC_API_SESSION_TTL_SEC_ENV, raising=False)

    assert config.public_api_session_ttl_sec() == 3600


def test_public_api_session_ttl_comes_from_env(monkeypatch) -> None:
    monkeypatch.setenv(config.PUBLIC_API_SESSION_TTL_SEC_ENV, "7200")

    assert config.public_api_session_ttl_sec() == 7200


def test_public_api_rejects_invalid_session_ttl_env(monkeypatch) -> None:
    monkeypatch.setenv(config.PUBLIC_API_SESSION_TTL_SEC_ENV, "0")

    try:
        config.public_api_session_ttl_sec()
    except PublicApiError as exc:
        assert exc.status_code == 500
        assert exc.code == "invalid_session_ttl"
    else:
        raise AssertionError("expected PublicApiError")


def test_public_api_rejects_invalid_jwt() -> None:
    token = security.create_jwt("secret", "client", ttl_sec=60)

    try:
        security.verify_jwt(token, "other-secret")
    except PublicApiError as exc:
        assert exc.status_code == 401
        assert exc.code == "invalid_token"
    else:
        raise AssertionError("expected PublicApiError")


def test_auth_store_creates_users_and_sessions() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = make_store(temp_dir)
        auth = smart_core.AuthStore(store)

        auth.add_user("api_user", "secret-password")
        user_session = auth.create_session("api_user", "secret-password", ttl_sec=60)

        assert auth.verify_password("api_user", "secret-password")
        assert not auth.verify_password("api_user", "wrong-password")
        assert auth.require_active_session(user_session.session_id).username == "api_user"

        auth.revoke_session(user_session.session_id)
        try:
            auth.require_active_session(user_session.session_id)
        except smart_core.SmartWateringError as exc:
            assert "revoked" in str(exc)
        else:
            raise AssertionError("expected SmartWateringError")


def test_auth_store_creates_external_user_session() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = make_store(temp_dir)
        auth = smart_core.AuthStore(store)

        user_session = auth.create_external_session("google", "1234567890", ttl_sec=60)

        assert user_session.username == "google:1234567890"
        assert auth.require_active_session(user_session.session_id).username == "google:1234567890"
        assert auth.list_users()[0].username == "google:1234567890"


def test_auth_store_drops_user_sessions() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = make_store(temp_dir)
        auth = smart_core.AuthStore(store)

        auth.add_user("api_user", "secret-password")
        user_session = auth.create_session("api_user", "secret-password", ttl_sec=60)
        auth.drop_user("api_user")

        assert auth.list_users() == []
        try:
            auth.require_active_session(user_session.session_id)
        except smart_core.SmartWateringError as exc:
            assert "unknown session" in str(exc)
        else:
            raise AssertionError("expected SmartWateringError")


def test_public_api_builds_watering_status() -> None:
    status = domain.build_watering_status(
        {
            "device": {"name": "main_tank", "type": "tank"},
            "watering": {
                "active": True,
                "state": "watering",
                "last_operation_type": "start",
                "last_operation_status": "in_progress",
            },
            "config": {"target_g": 200.0},
            "weight": {"water_used_g": 75.0},
        }
    )

    assert status == {
        "device": {"name": "main_tank", "type": "tank"},
        "active": True,
        "state": "watering",
        "gap_g": 125.0,
        "percent_complete": 37.5,
        "last_operation": {"type": "start", "status": "in_progress"},
    }


def make_public_api_handler(
    method: str,
    path: str,
    app: smart_cli.SmartWateringCliApp,
    token: str | None = None,
    body: dict[str, object] | None = None,
    session_ttl_sec: int = config.DEFAULT_TOKEN_TTL_SEC,
):
    class ApiClient:
        def __init__(self) -> None:
            settings = config.ApiSettings(
                jwt_secret="secret",
                session_ttl_sec=session_ttl_sec,
                google_web_client_id="",
                google_allowed_emails={"user@example.com"},
                google_allowed_domains=set(),
                prometheus_url=config.DEFAULT_PROMETHEUS_URL,
                statistics_timezone=ZoneInfo("Europe/Berlin"),
            )
            self.client = TestClient(create_app(ApiRuntime(app, settings)))
            self.responses: list[int] = []
            self.wfile = io.BytesIO()

        def request(self) -> None:
            headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
            kwargs = {"headers": headers}
            if method != "GET":
                kwargs["json"] = body or {}
            response = self.client.request(method, path, **kwargs)
            self.responses.append(response.status_code)
            self.wfile.write(response.content)

        def do_GET(self) -> None:
            self.request()

        def do_POST(self) -> None:
            self.request()

    return ApiClient()


def public_api_response_body(handler) -> dict[str, object]:
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def make_public_api_token(app: smart_cli.SmartWateringCliApp) -> str:
    try:
        app.auth.add_user("client", "secret-password")
    except smart_core.SmartWateringError:
        pass
    user_session = app.auth.create_session("client", "secret-password", ttl_sec=60)
    return security.create_session_jwt("secret", user_session)


def test_public_api_login_creates_session_token() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.auth.add_user("client", "secret-password")
        handler = make_public_api_handler(
            "POST",
            "/api/v2/auth/login",
            app,
            body={"username": "client", "password": "secret-password"},
        )

        handler.do_POST()

        assert handler.responses == [200]
        body = public_api_response_body(handler)
        payload = security.verify_jwt(body["token"], "secret")
        assert payload["sub"] == "client"
        assert app.auth.require_active_session(payload["sid"]).username == "client"
        assert 3500 <= payload["exp"] - payload["iat"] <= 3600


def test_public_api_login_uses_configured_session_ttl() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.auth.add_user("client", "secret-password")
        handler = make_public_api_handler(
            "POST",
            "/api/v2/auth/login",
            app,
            body={"username": "client", "password": "secret-password"},
            session_ttl_sec=7200,
        )

        handler.do_POST()

        assert handler.responses == [200]
        body = public_api_response_body(handler)
        payload = security.verify_jwt(body["token"], "secret")
        assert 7100 <= payload["exp"] - payload["iat"] <= 7200


def test_public_api_google_login_creates_session_token(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        monkeypatch.setattr(
            security,
            "verify_google_id_token",
            lambda token, web_client_id: {"sub": "1234567890", "email": "user@example.com"},
        )
        handler = make_public_api_handler(
            "POST",
            "/api/v2/auth/google",
            app,
            body={"id_token": "google-id-token"},
        )

        handler.do_POST()

        assert handler.responses == [200]
        body = public_api_response_body(handler)
        payload = security.verify_jwt(body["token"], "secret")
        assert payload["sub"] == "google:1234567890"
        assert app.auth.require_active_session(payload["sid"]).username == "google:1234567890"


def test_public_api_google_login_requires_allowed_account(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        monkeypatch.setattr(
            security,
            "verify_google_id_token",
            lambda token, web_client_id: {
                "sub": "1234567890",
                "email": "blocked@example.com",
                "email_verified": True,
            },
        )
        handler = make_public_api_handler(
            "POST",
            "/api/v2/auth/google",
            app,
            body={"id_token": "google-id-token"},
        )

        handler.do_POST()

        assert handler.responses == [403]
        assert public_api_response_body(handler)["error"] == "google_account_not_allowed"


def test_public_api_requires_bearer_token() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        handler = make_public_api_handler("GET", "/api/v2/devices", app)

        handler.do_GET()

        assert handler.responses == [401]
        assert public_api_response_body(handler)["error"] == "missing_token"


def test_public_api_lists_devices_with_pending_operation_flag() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.50", "plant", "plant_1")
        app.registry.add("192.168.1.51", "plant", "plant_2")
        app.queue_zero("plant_1")
        token = make_public_api_token(app)
        handler = make_public_api_handler("GET", "/api/v2/devices", app, token)

        handler.do_GET()

        assert handler.responses == [200]
        assert public_api_response_body(handler)["devices"] == [
            {"name": "plant_1", "type": "plant", "has_pending_operations": True},
            {"name": "plant_2", "type": "plant", "has_pending_operations": False},
        ]


def test_public_api_lists_supported_device_types() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        token = make_public_api_token(app)
        handler = make_public_api_handler("GET", "/api/v2/device-types", app, token)

        handler.do_GET()

        assert handler.responses == [200]
        assert public_api_response_body(handler) == {"types": ["plant", "tank"]}


def test_public_api_does_not_expose_device_health() -> None:
    class HealthApi:
        def request_text(self, base_url, path, method, payload=None):
            raise AssertionError("health endpoint should not call device API")

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.50", "plant", "plant_1")
        app.api = HealthApi()
        token = make_public_api_token(app)
        handler = make_public_api_handler("GET", "/api/v2/devices/plant_1/health", app, token)

        handler.do_GET()

        assert handler.responses == [404]
        assert public_api_response_body(handler)["error"] == "not_found"


def test_public_api_queues_watering_start_and_stop() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        add_confirmed_tank(app)
        token = make_public_api_token(app)

        start_handler = make_public_api_handler(
            "POST",
            "/api/v2/devices/main_tank/watering/start",
            app,
            token,
            {"target_g": 200},
        )
        start_handler.do_POST()
        stop_handler = make_public_api_handler(
            "POST",
            "/api/v2/devices/main_tank/watering/stop",
            app,
            token,
            {},
        )
        stop_handler.do_POST()

        assert start_handler.responses == [202]
        assert stop_handler.responses == [202]
        start_body = public_api_response_body(start_handler)
        assert start_body["operation_id"]
        assert start_body["device"] == "main_tank"
        assert start_body["type"] == "watering_start"
        assert start_body["status"] == "queued"
        assert start_body["target_g"] == 200
        assert "payload" not in start_body
        assert "operation_url" not in start_body
        assert "events_url" not in start_body
        assert [command.path for command in app.queue.list()] == ["/watering/stop"]


def test_public_api_queues_device_status_read_and_returns_result() -> None:
    class StatusApi:
        def request_json(self, base_url, path, method, payload=None):
            return {
                "device": {"name": "plant_1", "type": "plant"},
                "watering": {"active": False, "state": "waiting"},
                "config": {"sleep_interval_min": 20},
                "weight": {"useful_weight_g": 42.0},
            }

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        app.api = StatusApi()
        token = make_public_api_token(app)

        queue_handler = make_public_api_handler("POST", "/api/v2/devices/plant_1/status", app, token, {})
        queue_handler.do_POST()

        assert queue_handler.responses == [202]
        queue_body = public_api_response_body(queue_handler)
        operation_id = queue_body["operation_id"]
        assert queue_body["type"] == "device_status"
        assert queue_body["status"] == "queued"
        assert "operation_url" not in queue_body
        assert [command.path for command in app.queue.list()] == ["/watering"]

        worker = smart_core.BackgroundWorker(
            app.api,
            app.queue,
            app.operations,
            smart_core.WorkerState(str(Path(temp_dir) / "worker.pid")),
            retry_interval_sec=0,
            max_wait_sec=1,
        )
        worker.run()

        operation_handler = make_public_api_handler("GET", f"/api/v2/operations/{operation_id}", app, token)
        operation_handler.do_GET()

        assert operation_handler.responses == [200]
        operation_body = public_api_response_body(operation_handler)
        assert operation_body["status"] == "success"
        assert "result" not in operation_body
        assert "result_received_at" not in operation_body


def test_public_api_status_latest_returns_none_without_snapshot_without_live_request() -> None:
    class UnexpectedLiveStatusApi:
        def __init__(self) -> None:
            self.timeout_sec = 5

        def request_json(self, base_url, path, method, payload=None):
            raise AssertionError("status/latest must not call the device API")

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        app.api = UnexpectedLiveStatusApi()
        service = PublicApiService(app, "http://127.0.0.1:9090", ZoneInfo("Europe/Berlin"))

        body = service.latest_device_status_response("plant_1")
        assert body == {
            "device": "plant_1",
            "status": "offline",
            "source": "none",
            "available": False,
            "result": None,
            "result_received_at": None,
            "operation_id": None,
            "pending_operation_id": None,
            "pending_operation_status": None,
            "error": {
                "code": "device_status_snapshot_not_found",
                "message": "no stored status snapshot exists for device 'plant_1'",
                "retryable": True,
            },
        }
        assert app.api.timeout_sec == 5


def test_public_api_status_latest_returns_minimal_snapshot_result() -> None:
    class StatusApi:
        timeout_sec = 5

        def request_text(self, base_url, path, method, payload=None):
            raise AssertionError("status/latest must not call the device API")

        def request_json(self, base_url, path, method, payload=None):
            return {
                "device": {"name": "plant_1", "type": "plant"},
                "watering": {"active": False, "state": "waiting", "debug": "ignored"},
                "config": {"dry_weight_g": 120.0, "sleep_interval_min": 20},
                "weight": {"gross_weight_g": 180.0, "useful_weight_g": 60.0, "raw": 123},
            }

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        app.api = StatusApi()
        operation_id = app.queue_device_status("plant_1")
        worker = smart_core.BackgroundWorker(
            app.api,
            app.queue,
            app.operations,
            smart_core.WorkerState(str(Path(temp_dir) / "worker.pid")),
            retry_interval_sec=0,
            max_wait_sec=1,
        )
        worker.run()
        service = PublicApiService(app, "http://127.0.0.1:9090", ZoneInfo("Europe/Berlin"))

        body = service.latest_device_status_response("plant_1")
        assert body["status"] == "unknown"
        assert body["source"] == "snapshot"
        assert body["available"] is True
        assert body["operation_id"] == operation_id
        assert body["result"] == {
            "device": {"name": "plant_1", "type": "plant"},
            "watering": {
                "active": False,
                "state": "waiting",
                "last_operation_type": None,
                "last_operation_status": None,
            },
            "config": {
                "target_g": None,
                "dry_weight_g": 120.0,
                "wet_weight_g": None,
                "watering_loss_threshold_percent": None,
                "tare_weight_g": None,
                "zero_raw": None,
                "raw_per_gram": None,
                "sleep_disabled": None,
                "sleep_interval_min": 20,
            },
            "weight": {"gross_weight_g": 180.0, "useful_weight_g": 60.0, "water_used_g": None},
        }
        assert app.api.timeout_sec == 5


def test_public_api_status_live_route_is_registered() -> None:
    paths = [route.path for route in public_api_devices.router.routes]

    assert "/api/v2/devices/{device_name}/status/live" in paths


def test_public_api_status_live_returns_live_result() -> None:
    class LiveStatusApi:
        def __init__(self) -> None:
            self.timeout_sec = 5
            self.seen_timeout_sec = None
            self.requests = []

        def request_json(self, base_url, path, method, payload=None):
            self.seen_timeout_sec = self.timeout_sec
            self.requests.append((base_url, path, method, payload))
            return {
                "device": {"name": "plant_1", "type": "plant"},
                "watering": {"active": False, "state": "waiting"},
                "config": {"sleep_interval_min": 20},
                "weight": {"useful_weight_g": 42.0},
            }

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        app.api = LiveStatusApi()
        service = PublicApiService(app, "http://127.0.0.1:9090", ZoneInfo("Europe/Berlin"))

        body = service.live_device_status_response("plant_1")
        assert body["source"] == "live"
        assert body["available"] is True
        assert body["result"]["device"]["name"] == "plant_1"
        assert body["operation_id"] is None
        assert body["error"] is None
        assert app.api.requests == [("http://192.168.1.10", "/watering", "GET", None)]
        assert app.api.seen_timeout_sec == 3
        assert app.api.timeout_sec == 5


def test_public_api_status_latest_returns_snapshot_without_live_request() -> None:
    class StatusApi:
        def __init__(self, available: bool) -> None:
            self.available = available

        def request_json(self, base_url, path, method, payload=None):
            if not self.available:
                raise smart_core.RetryableDeviceApiError("device sleeping")
            return {
                "device": {"name": "plant_1", "type": "plant"},
                "watering": {"active": False, "state": "waiting"},
                "config": {"sleep_interval_min": 20},
                "weight": {"useful_weight_g": 42.0},
            }

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        app.api = StatusApi(True)
        operation_id = app.queue_device_status("plant_1")
        worker = smart_core.BackgroundWorker(
            app.api,
            app.queue,
            app.operations,
            smart_core.WorkerState(str(Path(temp_dir) / "worker.pid")),
            retry_interval_sec=0,
            max_wait_sec=1,
        )
        worker.run()
        app.api = StatusApi(False)
        service = PublicApiService(app, "http://127.0.0.1:9090", ZoneInfo("Europe/Berlin"))

        body = service.latest_device_status_response("plant_1")
        assert body["source"] == "snapshot"
        assert body["available"] is True
        assert body["operation_id"] == operation_id
        assert body["pending_operation_id"] is None
        assert body["pending_operation_status"] is None
        assert "pending_operation_url" not in body
        assert body["result"]["config"] == {
            "target_g": None,
            "dry_weight_g": None,
            "wet_weight_g": None,
            "watering_loss_threshold_percent": None,
            "tare_weight_g": None,
            "zero_raw": None,
            "raw_per_gram": None,
            "sleep_disabled": None,
            "sleep_interval_min": 20,
        }
        assert isinstance(body["result_received_at"], float)
        assert body["error"] is None


def test_public_api_status_live_does_not_fall_back_to_snapshot() -> None:
    class StatusApi:
        def __init__(self, available: bool) -> None:
            self.available = available

        def request_json(self, base_url, path, method, payload=None):
            if not self.available:
                raise smart_core.RetryableDeviceApiError("device sleeping")
            return {
                "device": {"name": "plant_1", "type": "plant"},
                "watering": {"active": False, "state": "waiting"},
                "config": {"sleep_interval_min": 20},
                "weight": {"useful_weight_g": 42.0},
            }

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        app.api = StatusApi(True)
        app.queue_device_status("plant_1")
        worker = smart_core.BackgroundWorker(
            app.api,
            app.queue,
            app.operations,
            smart_core.WorkerState(str(Path(temp_dir) / "worker.pid")),
            retry_interval_sec=0,
            max_wait_sec=1,
        )
        worker.run()
        app.api = StatusApi(False)
        service = PublicApiService(app, "http://127.0.0.1:9090", ZoneInfo("Europe/Berlin"))

        body = service.live_device_status_response("plant_1")
        assert body == {
            "device": "plant_1",
            "status": "offline",
            "source": "none",
            "available": False,
            "result": None,
            "result_received_at": None,
            "operation_id": None,
            "pending_operation_id": None,
            "pending_operation_status": None,
            "error": {
                "code": "device_status_unavailable",
                "message": "device sleeping",
                "retryable": True,
            },
        }


def test_public_api_status_latest_includes_pending_operation_without_snapshot() -> None:
    class SleepingApi:
        def request_json(self, base_url, path, method, payload=None):
            raise AssertionError("status/latest must not call the device API")

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        app.api = SleepingApi()
        operation_id = app.queue_device_status("plant_1")
        service = PublicApiService(app, "http://127.0.0.1:9090", ZoneInfo("Europe/Berlin"))

        body = service.latest_device_status_response("plant_1")
        assert body["source"] == "none"
        assert body["available"] is False
        assert body["pending_operation_id"] == operation_id
        assert body["pending_operation_status"] == "queued"


def test_public_api_status_latest_includes_pending_operation_with_snapshot() -> None:
    class StatusApi:
        def __init__(self, available: bool) -> None:
            self.available = available

        def request_json(self, base_url, path, method, payload=None):
            if not self.available:
                raise smart_core.RetryableDeviceApiError("device sleeping")
            return {
                "device": {"name": "plant_1", "type": "plant"},
                "watering": {"active": False, "state": "waiting"},
                "config": {"sleep_interval_min": 20},
                "weight": {"useful_weight_g": 42.0},
            }

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        app.api = StatusApi(True)
        completed_operation_id = app.queue_device_status("plant_1")
        worker = smart_core.BackgroundWorker(
            app.api,
            app.queue,
            app.operations,
            smart_core.WorkerState(str(Path(temp_dir) / "worker.pid")),
            retry_interval_sec=0,
            max_wait_sec=1,
        )
        worker.run()
        pending_operation_id = app.queue_device_status("plant_1")
        app.api = StatusApi(False)
        service = PublicApiService(app, "http://127.0.0.1:9090", ZoneInfo("Europe/Berlin"))

        body = service.latest_device_status_response("plant_1")
        assert body["source"] == "snapshot"
        assert body["available"] is True
        assert body["operation_id"] == completed_operation_id
        assert body["pending_operation_id"] == pending_operation_id
        assert body["pending_operation_status"] == "queued"


def test_public_api_exposes_authenticated_sleep_and_zero_actions() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        app.registry.add("192.168.1.10", "plant", "plant_1")
        token = make_public_api_token(app)

        sleep_handler = make_public_api_handler(
            "POST",
            "/api/v2/devices/plant_1/sleep/disable",
            app,
            token,
            {},
        )
        sleep_handler.do_POST()
        zero_handler = make_public_api_handler(
            "POST",
            "/api/v2/devices/plant_1/zero",
            app,
            token,
            {},
        )
        zero_handler.do_POST()

        assert sleep_handler.responses == [202]
        assert public_api_response_body(sleep_handler)["type"] == "sleep_disable"
        assert zero_handler.responses == [202]
        assert public_api_response_body(zero_handler)["type"] == "zero_capture"
        assert [command.path for command in app.queue.list()] == ["/sleep/disable", "/zero"]


def test_public_api_returns_operation_and_events_by_id() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        add_confirmed_tank(app)
        token = make_public_api_token(app)
        operation_id = app.queue_fill("main_tank", 200)

        operation_handler = make_public_api_handler("GET", f"/api/v2/operations/{operation_id}", app, token)
        operation_handler.do_GET()
        events_handler = make_public_api_handler("GET", f"/api/v2/operations/{operation_id}/events", app, token)
        events_handler.do_GET()

        assert operation_handler.responses == [200]
        operation_body = public_api_response_body(operation_handler)
        assert operation_body["operation_id"] == operation_id
        assert operation_body["device"] == "main_tank"
        assert operation_body["type"] == "watering_start"
        assert operation_body["status"] == "queued"
        assert operation_body["target_g"] == 200
        assert "payload" not in operation_body
        assert "progress" not in operation_body
        assert "error" not in operation_body
        assert "operation_url" not in operation_body
        assert "events_url" not in operation_body

        assert events_handler.responses == [200]
        events_body = public_api_response_body(events_handler)
        assert events_body["operation_id"] == operation_id
        assert events_body["events"][0]["status"] == "queued"
        assert events_body["events"][0]["message"] == "operation queued"


def test_public_api_exposes_controller_error_detail_by_operation_id() -> None:
    class FakeDeviceApi:
        def request_json(self, base_url, path, method, payload=None):
            return {
                "device": {"name": "main_tank", "type": "tank"},
                "watering": {
                    "active": False,
                    "state": "waiting",
                    "last_operation_type": "start",
                    "last_operation_status": "failed",
                },
                "config": {"target_g": 200.0},
                "weight": {"water_used_g": 22.0},
            }

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        add_confirmed_tank(app)
        app.api = FakeDeviceApi()
        token = make_public_api_token(app)
        operation_id = app.queue_fill("main_tank", 200)
        app.operations.event(operation_id, "sending", "worker picked operation")
        app.operations.event(operation_id, "accepted", "device accepted command")
        app.operations.event(operation_id, "running", "pump_on")
        app.operations.event(operation_id, "error", "weight_not_changing")
        handler = make_public_api_handler("GET", f"/api/v2/operations/{operation_id}", app, token)

        handler.do_GET()

        assert handler.responses == [200]
        body = public_api_response_body(handler)
        assert body["status"] == "error"
        assert body["error"] == {
            "code": "error",
            "message": "weight_not_changing",
            "detail": "weight_not_changing",
            "retryable": True,
        }
        assert "progress" not in body


def test_public_api_returns_watering_status() -> None:
    class FakeDeviceApi:
        def request_json(self, base_url, path, method, payload=None):
            return {
                "device": {"name": "main_tank", "type": "tank"},
                "watering": {
                    "active": True,
                    "state": "watering",
                    "last_operation_type": "start",
                    "last_operation_status": "in_progress",
                },
                "config": {"target_g": 100.0},
                "weight": {"water_used_g": 25.0},
            }

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        add_confirmed_tank(app)
        app.api = FakeDeviceApi()
        token = make_public_api_token(app)
        handler = make_public_api_handler("GET", "/api/v2/devices/main_tank/watering/status", app, token)

        handler.do_GET()

        assert handler.responses == [200]
        body = public_api_response_body(handler)
        assert body["gap_g"] == 75.0
        assert body["percent_complete"] == 25.0
        assert body["last_operation"] == {"type": "start", "status": "in_progress"}
        assert body["source"] == "live"
        assert body["available"] is True
        assert body["planned_watering"] is None


def test_public_api_watering_status_includes_planned_watering() -> None:
    class FakeDeviceApi:
        timeout_sec = 5

        def request_json(self, base_url, path, method, payload=None):
            return {
                "device": {"name": "main_tank", "type": "tank"},
                "watering": {
                    "active": False,
                    "state": "waiting",
                    "last_operation_type": "none",
                    "last_operation_status": "none",
                },
                "config": {"target_g": 0.0, "tare_weight_g": 450.0},
                "weight": {"gross_weight_g": 500.0, "water_used_g": 0.0},
            }

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        add_confirmed_tank(app)
        operation_id = app.queue_fill("main_tank", 200)
        app.api = FakeDeviceApi()
        token = make_public_api_token(app)
        handler = make_public_api_handler("GET", "/api/v2/devices/main_tank/watering/status", app, token)

        handler.do_GET()

        assert handler.responses == [200]
        planned_watering = public_api_response_body(handler)["planned_watering"]
        assert planned_watering == {
            "operation_id": operation_id,
            "target_g": 200,
            "status": "queued",
        }
        body = public_api_response_body(handler)
        assert "pending_operation_url" not in body
        assert body["result"]["config"] == {
            "target_g": 0.0,
            "dry_weight_g": None,
            "wet_weight_g": None,
            "watering_loss_threshold_percent": None,
            "tare_weight_g": 450.0,
            "zero_raw": None,
            "raw_per_gram": None,
            "sleep_disabled": None,
            "sleep_interval_min": None,
        }


def test_public_api_last_watering_is_not_limited_by_recent_operations() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        add_confirmed_tank(app)
        watering_operation_id = app.operations.create("main_tank", "watering_start", {"target_g": 200})
        app.operations.event(watering_operation_id, "success", "target_reached")
        for index in range(25):
            operation_id = app.operations.create("main_tank", "device_status", {"index": index})
            app.operations.event(operation_id, "success", "status fetched")
        token = make_public_api_token(app)
        handler = make_public_api_handler("GET", "/api/v2/devices/main_tank/watering/last", app, token)

        handler.do_GET()

        assert handler.responses == [200]
        operation = public_api_response_body(handler)["operation"]
        assert operation["operation_id"] == watering_operation_id
        assert operation["type"] == "watering_start"
        assert operation["status"] == "success"
        assert "operation_url" not in operation
        assert "events_url" not in operation


def test_public_api_watering_history_returns_recent_and_successful_only() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        add_confirmed_tank(app)
        created_ids = []
        successful_ids = []
        for index in range(12):
            operation_id = app.operations.create("main_tank", "watering_start", {"target_g": 100 + index})
            status = "success" if index % 2 == 0 else "error"
            app.operations.event(operation_id, status, "target_reached" if status == "success" else "pump_error")
            created_ids.append(operation_id)
            if status == "success":
                successful_ids.append(operation_id)
        config_operation_id = app.operations.create("main_tank", "config", {"name": "main_tank"})
        app.operations.event(config_operation_id, "success", "config_updated")
        token = make_public_api_token(app)

        handler = make_public_api_handler("GET", "/api/v2/watering/history", app, token)
        handler.do_GET()
        success_handler = make_public_api_handler("GET", "/api/v2/watering/history?successful=true", app, token)
        success_handler.do_GET()

        assert handler.responses == [200]
        operations = public_api_response_body(handler)["operations"]
        assert [operation["operation_id"] for operation in operations] == list(reversed(created_ids[-10:]))
        assert all(operation["type"] == "watering_start" for operation in operations)
        assert success_handler.responses == [200]
        successful_operations = public_api_response_body(success_handler)["operations"]
        assert [operation["operation_id"] for operation in successful_operations] == list(reversed(successful_ids))
        assert all(operation["status"] == "success" for operation in successful_operations)


def test_public_api_watering_status_returns_503_without_live_snapshot_fallback() -> None:
    class StatusApi:
        def __init__(self, available: bool) -> None:
            self.available = available
            self.timeout_sec = 5

        def request_json(self, base_url, path, method, payload=None):
            if not self.available:
                raise smart_core.RetryableDeviceApiError("device sleeping")
            return {
                "device": {"name": "main_tank", "type": "tank"},
                "watering": {
                    "active": True,
                    "state": "watering",
                    "last_operation_type": "start",
                    "last_operation_status": "in_progress",
                },
                "config": {"target_g": 100.0},
                "weight": {"water_used_g": 25.0},
            }

    with tempfile.TemporaryDirectory() as temp_dir:
        app = smart_cli.SmartWateringCliApp(str(Path(temp_dir) / "test.db"))
        add_confirmed_tank(app)
        app.api = StatusApi(True)
        operation_id = app.queue_device_status("main_tank")
        worker = smart_core.BackgroundWorker(
            app.api,
            app.queue,
            app.operations,
            smart_core.WorkerState(str(Path(temp_dir) / "worker.pid")),
            retry_interval_sec=0,
            max_wait_sec=1,
        )
        worker.run()
        app.api = StatusApi(False)
        token = make_public_api_token(app)
        handler = make_public_api_handler("GET", "/api/v2/devices/main_tank/watering/status", app, token)

        handler.do_GET()

        assert handler.responses == [503]
        body = public_api_response_body(handler)
        assert body == {"error": "device_status_unavailable", "message": "device sleeping"}
