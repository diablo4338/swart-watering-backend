from pathlib import Path

import yaml


def test_mcu_openapi_response_enums_are_explicit() -> None:
    schema_path = Path(__file__).resolve().parents[2] / "firmware" / "mcu_api.openapi.yaml"

    with schema_path.open(encoding="utf-8") as handle:
        schema = yaml.safe_load(handle)

    schemas = schema["components"]["schemas"]

    assert set(schemas["StatusResponse"]["properties"]["status"]["enum"]) == {
        "accepted",
        "calibrated",
        "config_updated",
        "no_active_watering",
        "sleep_disabled",
        "sleep_enabled",
        "sleep_interval_updated",
        "stop_requested",
        "zero_captured",
    }
    assert set(schemas["ErrorResponse"]["properties"]["error"]["enum"]) == {
        "calibration_failed",
        "config_update_failed",
        "device_not_tank",
        "empty_config",
        "invalid_calibration_weight_g",
        "invalid_device_type",
        "invalid_dry_weight_g",
        "invalid_wet_weight_g",
        "invalid_watering_loss_threshold_percent",
        "invalid_name",
        "invalid_sleep_interval",
        "invalid_target_g",
        "invalid_tare_weight_g",
        "no_memory",
        "read_failed",
        "sleep_disable_failed",
        "sleep_enable_failed",
        "sleep_interval_update_failed",
        "task_create_failed",
        "watering_already_active",
        "watering_start_failed",
        "zero_capture_failed",
    }


def test_deep_sleep_does_not_release_unowned_rc_fast_power_domain() -> None:
    main_source_path = Path(__file__).resolve().parents[2] / "firmware" / "main" / "main.c"
    main_source = main_source_path.read_text(encoding="utf-8")

    assert "esp_sleep_pd_config(ESP_PD_DOMAIN_RC_FAST, ESP_PD_OPTION_OFF)" not in main_source


def test_preinitialized_task_watchdog_is_reconfigured_without_duplicate_init() -> None:
    watchdog_source_path = (
        Path(__file__).resolve().parents[2] / "firmware" / "main" / "app_watchdog.c"
    )
    watchdog_source = watchdog_source_path.read_text(encoding="utf-8")

    assert "#if CONFIG_ESP_TASK_WDT_INIT" in watchdog_source
    preinitialized_branch = watchdog_source.split(
        "#if CONFIG_ESP_TASK_WDT_INIT", 1
    )[1].split("#else", 1)[0]
    assert "esp_task_wdt_reconfigure(&config)" in preinitialized_branch
    assert "esp_task_wdt_init(&config)" not in preinitialized_branch
