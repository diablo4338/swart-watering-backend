#!/usr/bin/env python3
import argparse
import getpass
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from smart_watering.application.service import SmartWateringService
from smart_watering.application.watering_detection import PlantWateringDetector
from smart_watering.domain import (
    CONFIG_FLOAT_KEYS,
    CONFIG_KEYS,
    DEVICE_TYPES,
    DeviceType,
    RETRYABLE_COMMANDS,
    Device,
    OP_CANCELLED,
    OP_ERROR,
    OP_SUCCESS,
    OP_TERMINAL_STATUSES,
    OP_TIMEOUT,
    OperationLog,
    QueuedCommand,
    RetryableDeviceApiError,
    SmartWateringError,
    build_callback_url,
    parse_positive_int,
)
from smart_watering.public_api_app.config import (
    DEFAULT_PROMETHEUS_URL,
    PROMETHEUS_URL_ENV,
)

MAX_SLEEP_INTERVAL_MIN = 50
CLI_OPERATION_WAIT_TIMEOUT_SEC = 900
CLI_OPERATION_WAIT_TIMEOUT_SEC_ENV = "SMART_WATERING_CLI_OPERATION_WAIT_TIMEOUT_SEC"
CLI_OPERATION_POLL_INTERVAL_SEC = 0.5


def resolve_cli_operation_wait_timeout_sec() -> int:
    raw_value = os.environ.get(CLI_OPERATION_WAIT_TIMEOUT_SEC_ENV)
    if raw_value is None:
        return CLI_OPERATION_WAIT_TIMEOUT_SEC
    return parse_positive_int(raw_value, CLI_OPERATION_WAIT_TIMEOUT_SEC_ENV)


def parse_config_assignments(raw_values: list[str]) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for raw_value in raw_values:
        if "=" not in raw_value:
            raise SmartWateringError("config values must look like key=value")

        key, value = raw_value.split("=", 1)
        key = key.strip().lower()
        value = value.strip()

        aliases = {
            "type": "device_type",
            "tare": "tare_weight_g",
            "dry": "dry_weight_g",
        }
        key = aliases.get(key, key)

        if key not in CONFIG_KEYS:
            supported = ", ".join(sorted(CONFIG_KEYS | set(aliases)))
            raise SmartWateringError(f"unsupported config key: {key}; supported: {supported}")

        if key in CONFIG_FLOAT_KEYS:
            try:
                number = float(value)
            except ValueError as exc:
                raise SmartWateringError(f"{key} must be a number") from exc
            if number < 0:
                raise SmartWateringError(f"{key} must be >= 0")
            config[key] = number
        else:
            if key == "device_type" and value not in DEVICE_TYPES:
                raise SmartWateringError("device_type must be plant or tank")
            if key == "name" and not value:
                raise SmartWateringError("name must not be empty")
            config[key] = value

    return config


class SmartWateringCliApp(SmartWateringService):
    def __init__(self, db_path: str | None = None) -> None:
        super().__init__(db_path)
        self.interactive_message_delay_sec = 2.0
        self.operation_wait_timeout_sec = resolve_cli_operation_wait_timeout_sec()
        self.operation_poll_interval_sec = CLI_OPERATION_POLL_INTERVAL_SEC

    @staticmethod
    def format_status(payload: dict[str, Any]) -> str:
        device = payload.get("device", {})
        watering = payload.get("watering", {})
        config = payload.get("config", {})
        weight = payload.get("weight", {})
        lines = [
            f"device: {device.get('name', 'unknown')} ({device.get('type', 'unknown')})",
            f"state: {watering.get('state', 'unknown')}",
            f"active: {watering.get('active', False)}",
            f"last operation type: {watering.get('last_operation_type', 'unknown')}",
            f"last operation status: {watering.get('last_operation_status', 'unknown')}",
        ]
        for label, key in (("tare weight", "tare_weight_g"), ("dry weight", "dry_weight_g"), ("target", "target_g")):
            value = config.get(key)
            lines.append(f"{label}: {value:.1f} g" if isinstance(value, (int, float)) else f"{label}: unknown")
        sleep_disabled = config.get("sleep_disabled")
        if isinstance(sleep_disabled, bool):
            lines.append(f"sleep mode: {'disabled' if sleep_disabled else 'enabled'}")
        sleep_interval_min = config.get("sleep_interval_min")
        if isinstance(sleep_interval_min, int):
            lines.append(f"sleep interval: {sleep_interval_min} min")
        zero_raw = config.get("zero_raw")
        if isinstance(zero_raw, int):
            lines.append(f"zero raw: {zero_raw}")
        raw_per_gram = config.get("raw_per_gram")
        if isinstance(raw_per_gram, (int, float)):
            lines.append(f"raw per gram: {raw_per_gram:.3f}")
        for label, key in (("useful weight", "useful_weight_g"), ("gross weight", "gross_weight_g"), ("water used", "water_used_g")):
            value = weight.get(key)
            lines.append(f"{label}: {value:.1f} g" if isinstance(value, (int, float)) else f"{label}: unknown")
        return "\n".join(lines)

    @staticmethod
    def format_devices(devices: list[Device]) -> str:
        if not devices:
            return "devices: none"
        return "\n".join(f"{device.name}\t{device.device_type}\t{device.ip}" for device in devices)

    @staticmethod
    def format_constants(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    @staticmethod
    def format_operations(rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "operations: none"
        return "\n".join(
            f"{row['operation_id']}\t{row['device_name']}\t"
            f"{OperationLog.public_type(row['operation_type'])}\t{row['status']}"
            for row in rows
        )

    @staticmethod
    def format_users(rows: list[Any]) -> str:
        if not rows:
            return "users: none"
        return "\n".join(user.username for user in rows)

    @staticmethod
    def format_operation_events(rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "operation events: none"
        return "\n".join(f"{row['status']}\t{row['detail']}" for row in rows)

    @staticmethod
    def format_operation_detail(operation: dict[str, Any] | None, events: list[dict[str, Any]]) -> str:
        if operation is None:
            return "operation: not found"
        return "\n".join([
            f"operation: {operation['operation_id']}",
            f"device: {operation['device_name']}",
            f"type: {OperationLog.public_type(operation['operation_type'])}",
            f"status: {operation['status']}",
            f"payload: {operation.get('payload_json') or '{}'}",
            "events:",
            SmartWateringCliApp.format_operation_events(events),
        ])

    @staticmethod
    def format_operation_result(operation: dict[str, Any], events: list[dict[str, Any]]) -> str:
        operation_type = OperationLog.public_type(operation["operation_type"])
        latest_detail = events[-1]["detail"] if events else operation["status"]
        if operation["status"] == OP_SUCCESS:
            return f"success: {operation_type} on {operation['device_name']} ({latest_detail})"
        if operation["status"] == OP_CANCELLED:
            return f"cancelled: {operation_type} on {operation['device_name']} ({latest_detail})"
        return f"failed: {operation_type} on {operation['device_name']} ({operation['status']}: {latest_detail})"

    @staticmethod
    def format_main_menu() -> str:
        return "\n".join([
            "", "Smart Watering", "", "Devices",
            "1. Register device", "2. List devices", "3. Remove device", "4. Configure device", "",
            "Read", "5. Show device status", "6. Show device metrics", "7. Show device constants", "",
            "Watering", "8. Start watering", "9. Stop watering", "",
            "Device actions", "10. Enable sleep", "11. Disable sleep", "12. Set sleep interval", "13. Set zero", "14. Calibrate scale", "",
            "Operations", "15. Show pending queue", "16. Clear device queue", "17. Show recent operations", "",
            "Callback", "18. Show callback URL", "",
            "Users", "19. Add API user", "20. List API users", "21. Drop API user", "",
            "Statistics", "22. Sync detected watering history",
            "23. Hard drop detected watering history", "", "0. Exit",
        ])

    @staticmethod
    def clear_interactive_screen() -> None:
        print("\033[2J\033[H", end="")

    def show_interactive_message(self, message: str) -> None:
        print()
        print(message)
        time.sleep(self.interactive_message_delay_sec)

    @staticmethod
    def wait_for_interactive_continue() -> None:
        input("Press Enter to continue...")

    def callback_url(self) -> str:
        return build_callback_url()

    def build_operation_payload(self, operation_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        result = dict(payload or {})
        result["operation_id"] = operation_id
        result["callback_url"] = self.callback_url()
        return result

    @staticmethod
    def is_retryable_command(method: str, path: str) -> bool:
        return (method, path) in RETRYABLE_COMMANDS

    def find_pending_retryable_command(self, base_url: str, method: str, path: str) -> QueuedCommand | None:
        if not self.is_retryable_command(method, path):
            return None
        for command in self.queue.list():
            if command.base_url == base_url and command.method == method and command.path == path:
                return command
        return None

    @staticmethod
    def format_command_values(payload: dict[str, Any] | None) -> str:
        values = {
            key: value
            for key, value in (payload or {}).items()
            if key not in {"operation_id", "callback_url"}
        }
        if not values:
            return "{}"
        return ", ".join(f"{key}={value}" for key, value in sorted(values.items()))

    def confirm_retryable_command_conflict(self, queued_command: QueuedCommand, new_payload: dict[str, Any]) -> bool:
        print(
            "warning: retryable command already queued: "
            f"{queued_command.operation_id} ({queued_command.method} {queued_command.path})"
        )
        print(f"queued: {self.format_command_values(queued_command.payload)}")
        print(f"new: {self.format_command_values(new_payload)}")
        try:
            answer = input("Add another command anyway? [y/N]: ").strip().lower()
        except EOFError:
            return False
        return answer in {"y", "yes"}

    def wait_for_operation(self, operation_id: str) -> tuple[bool, str]:
        print("waiting for controller result...")
        deadline = time.time() + self.operation_wait_timeout_sec
        last_status: str | None = None
        while True:
            operation = self.operations.get(operation_id)
            if operation is None:
                return False, "failed: operation disappeared"
            events = self.operations.events(operation_id)
            status = operation["status"]
            if status != last_status and status not in {OP_SUCCESS, OP_ERROR, OP_TIMEOUT, OP_CANCELLED}:
                latest_detail = events[-1]["detail"] if events else status
                print(f"status: {status} ({latest_detail})")
                last_status = status
            if status in OP_TERMINAL_STATUSES:
                return status == OP_SUCCESS, self.format_operation_result(operation, events)
            if time.time() >= deadline:
                return False, f"failed: controller result was not received within {self.operation_wait_timeout_sec}s"
            time.sleep(self.operation_poll_interval_sec)

    def report_operation(self, operation_id: str, wait: bool = True) -> bool:
        operation = self.operations.get(operation_id)
        if operation is None:
            print("failed: operation not found")
            return False
        if not wait:
            print(f"queued: {OperationLog.public_type(operation['operation_type'])} on {operation['device_name']}")
            return True
        success, message = self.wait_for_operation(operation_id)
        print(message)
        return success



    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description="CLI for smart watering devices")
        subparsers = parser.add_subparsers(dest="command")

        def add_wait_argument(command_parser: argparse.ArgumentParser) -> None:
            command_parser.add_argument(
                "--no-wait",
                action="store_true",
                help="Return after queueing instead of waiting for controller result",
            )

        devices = subparsers.add_parser("devices", help="Manage registered devices")
        device_subparsers = devices.add_subparsers(dest="device_command")
        add = device_subparsers.add_parser(
            "add", help="Discover and register a device; configure it only when offline and confirmed"
        )
        add.add_argument("ip")
        add.add_argument("name", nargs="?")
        add.add_argument("--type", choices=sorted(DEVICE_TYPES), default=DeviceType.PLANT)
        add_wait_argument(add)
        device_subparsers.add_parser("list", help="List registered devices")
        remove = device_subparsers.add_parser("remove", help="Remove a registered device")
        remove.add_argument("name")
        config = device_subparsers.add_parser("config", help="Update device config")
        config.add_argument("name")
        config.add_argument("values", nargs="+")
        add_wait_argument(config)
        status = subparsers.add_parser("status", help="Read /watering from a registered device")
        status.add_argument("device")
        constants = subparsers.add_parser("constants", help="Read MCU constants from a registered device")
        constants.add_argument("device")
        metrics = subparsers.add_parser("metrics", help="Read /metrics from a registered device")
        metrics.add_argument("device")
        ping = subparsers.add_parser("ping", help="Check whether a registered device is online")
        ping.add_argument("device")
        fill = subparsers.add_parser("fill", help="Queue watering start for a tank device")
        fill.add_argument("device")
        fill.add_argument("grams", type=float)
        add_wait_argument(fill)
        stop = subparsers.add_parser("stop", help="Queue watering stop for a tank device")
        stop.add_argument("device")
        add_wait_argument(stop)
        sleep = subparsers.add_parser("sleep", help="Queue sleep mode changes for a device")
        sleep_subparsers = sleep.add_subparsers(dest="sleep_command")
        sleep_enable = sleep_subparsers.add_parser("enable", help="Enable deep sleep for a device")
        sleep_enable.add_argument("device")
        add_wait_argument(sleep_enable)
        sleep_disable = sleep_subparsers.add_parser("disable", help="Disable deep sleep for a device")
        sleep_disable.add_argument("device")
        add_wait_argument(sleep_disable)
        sleep_interval = sleep_subparsers.add_parser("interval", help="Set deep sleep wake interval for a device")
        sleep_interval.add_argument("device")
        sleep_interval.add_argument("minutes", type=int)
        add_wait_argument(sleep_interval)
        zero = subparsers.add_parser("zero", help="Queue zero capture for a device")
        zero.add_argument("device")
        add_wait_argument(zero)
        calibration = subparsers.add_parser("calibration", help="Queue scale calibration for a device")
        calibration.add_argument("device")
        calibration.add_argument("weight_g", type=float)
        add_wait_argument(calibration)
        queue = subparsers.add_parser("queue", help="Manage queued commands")
        queue_subparsers = queue.add_subparsers(dest="queue_command")
        queue_clear = queue_subparsers.add_parser("clear", help="Clear queued commands for a device")
        queue_clear.add_argument("device")
        users = subparsers.add_parser("users", help="Manage public API users")
        user_subparsers = users.add_subparsers(dest="user_command")
        user_add = user_subparsers.add_parser("add", help="Add a public API user")
        user_add.add_argument("username")
        user_add.add_argument("--password", default=None)
        user_add.add_argument("--replace", action="store_true", help="Replace password when the user exists")
        user_subparsers.add_parser("list", help="List public API users")
        user_drop = user_subparsers.add_parser("drop", help="Drop a public API user")
        user_drop.add_argument("username")
        subparsers.add_parser("pending", help="Show queued commands")
        operations = subparsers.add_parser("operations", help="Show recent operations or one operation event log")
        operations.add_argument("operation_id", nargs="?")
        watering_history = subparsers.add_parser(
            "watering-history", help="Manage detected plant watering history"
        )
        watering_history_subparsers = watering_history.add_subparsers(
            dest="watering_history_command"
        )
        watering_history_sync = watering_history_subparsers.add_parser(
            "sync", help="Scan Prometheus and fill detected watering history"
        )
        watering_history_sync.add_argument("--days", type=int, default=30)
        watering_history_sync.add_argument("--device")
        watering_history_drop = watering_history_subparsers.add_parser(
            "drop", help="Permanently delete detected watering history"
        )
        watering_history_drop.add_argument("--device")
        watering_history_drop.add_argument("--yes", action="store_true")
        watering_history_rebuild = watering_history_subparsers.add_parser(
            "rebuild", help="Permanently delete and rebuild detected watering history"
        )
        watering_history_rebuild.add_argument("--days", type=int, default=30)
        watering_history_rebuild.add_argument("--device")
        watering_history_rebuild.add_argument("--yes", action="store_true")
        return parser

    @staticmethod
    def prompt(label: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default is not None else ""
        value = input(f"{label}{suffix}: ").strip()
        if not value and default is not None:
            return default
        return value

    @staticmethod
    def prompt_float(label: str, required: bool = False) -> float | None:
        while True:
            value = input(f"{label}: ").strip()
            if not value and not required:
                return None
            try:
                parsed = float(value)
            except ValueError:
                print("error: value must be a number")
                continue
            if parsed < 0:
                print("error: value must be >= 0")
                continue
            if required and parsed == 0:
                print("error: value must be > 0")
                continue
            return parsed

    @staticmethod
    def prompt_int(label: str, required: bool = False, max_value: int | None = None) -> int | None:
        while True:
            value = input(f"{label}: ").strip()
            if not value and not required:
                return None
            try:
                parsed = int(value)
            except ValueError:
                print("error: value must be an integer")
                continue
            if parsed <= 0:
                print("error: value must be > 0")
                continue
            if max_value is not None and parsed > max_value:
                print(f"error: value must be <= {max_value}")
                continue
            return parsed

    @staticmethod
    def prompt_device_type(default: str = DeviceType.PLANT) -> str:
        while True:
            value = input(f"Device type plant/tank [{default}]: ").strip().lower()
            if not value:
                return default
            if value in DEVICE_TYPES:
                return value
            print("error: device type must be plant or tank")

    def choose_device(self, prompt_text: str = "Select device") -> Device | None:
        devices = self.registry.list()
        if not devices:
            raise SmartWateringError("no registered devices")
        for index, device in enumerate(devices, start=1):
            print(f"{index}. {device.name} ({device.device_type}) {device.ip}")
        while True:
            raw_value = input(f"{prompt_text} [1-{len(devices)} or name, empty to cancel]: ").strip()
            if not raw_value:
                return None
            if raw_value.isdigit() and 1 <= int(raw_value) <= len(devices):
                return devices[int(raw_value) - 1]
            for device in devices:
                if device.name == raw_value:
                    return device
            print("unknown device selection")

    def interactive_register_device(self) -> None:
        ip = self.prompt("IP address or base URL")
        if not ip:
            print("cancelled")
            return
        self.register_device(ip)

    @staticmethod
    def discovered_identity(status: dict[str, Any]) -> tuple[str, str]:
        device = status.get("device")
        if not isinstance(device, dict):
            raise SmartWateringError("invalid /watering response: missing device object")
        name = device.get("name")
        device_type = device.get("type")
        if not isinstance(name, str) or not name:
            raise SmartWateringError("invalid /watering response: missing device.name")
        if device_type not in DEVICE_TYPES:
            raise SmartWateringError("invalid /watering response: unsupported device.type")
        return name, device_type

    def register_device(
        self,
        ip_or_url: str,
        fallback_type: str | None = None,
        fallback_name: str | None = None,
        wait: bool = True,
    ) -> bool:
        _, base_url = self.registry.normalize_base_url(ip_or_url)
        try:
            status = self.api.request_json(base_url, "/watering", "GET")
        except RetryableDeviceApiError as exc:
            print(f"warning: device is not powered on or is sleeping ({exc})")
            print(
                "warning: continuing will overwrite the controller's persisted fields: "
                "device_type and name"
            )
            confirmation = input("Continue with the current add procedure? [y/N]: ").strip().lower()
            if confirmation not in {"y", "yes"}:
                print("cancelled")
                return True

            if fallback_type is None:
                fallback_type = self.prompt_device_type(DeviceType.PLANT)
                fallback_name = self.prompt("Device name", "") or None
            device = self.registry.add(ip_or_url, fallback_type, fallback_name)
            operation_id = self.queue_device_config(
                device,
                {"device_type": device.device_type, "name": device.name},
                f"configure {device.name}",
            )
            print(f"registered: {device.name} ({device.device_type}) {device.ip}")
            return self.report_operation(operation_id, wait=wait)

        name, device_type = self.discovered_identity(status)
        device = self.registry.upsert_discovered(ip_or_url, device_type, name)
        print("device responded with:")
        print(f"  name: {device.name}")
        print(f"  device_type: {device.device_type}")
        print(f"  address: {device.ip}")
        print("registered without changing device config")
        return True

    def interactive_remove_device(self) -> None:
        device = self.choose_device("Remove device")
        if device is None:
            return
        confirmation = self.prompt(f"Type {device.name} to confirm removal")
        if confirmation != device.name:
            print("cancelled")
            return
        self.registry.remove(device.name)
        print(f"removed: {device.name}")

    def interactive_configure_device(self) -> None:
        device = self.choose_device("Configure device")
        if device is None:
            return
        config: dict[str, Any] = {}
        new_type = self.prompt_device_type(device.device_type)
        if new_type != device.device_type:
            config["device_type"] = new_type
        new_name = self.prompt("Device name", device.name)
        if new_name != device.name:
            config["name"] = new_name
        tare_weight_g = self.prompt_float("Tare weight g, empty to keep")
        if tare_weight_g is not None:
            config["tare_weight_g"] = tare_weight_g
        dry_weight_g = self.prompt_float("Dry weight g, empty to keep")
        if dry_weight_g is not None:
            config["dry_weight_g"] = dry_weight_g
        if not config:
            print("no changes")
            return
        desired_device = self.registry.validate_config_update(device.name, config)
        operation_id = self.queue_device_config(
            device,
            config,
            f"configure {desired_device.name}",
            confirm_retry_duplicate=True,
        )
        if operation_id is None:
            return
        self.report_operation(operation_id)

    def interactive_show_status(self) -> None:
        device = self.choose_device("Show status for")
        if device is not None:
            print(self.format_status(self.api.request_json(device.base_url, "/watering", "GET")))

    def interactive_show_metrics(self) -> None:
        device = self.choose_device("Show metrics for")
        if device is not None:
            print(self.api.request_text(device.base_url, "/metrics", "GET"))

    def interactive_show_constants(self) -> None:
        device = self.choose_device("Show constants for")
        if device is not None:
            print(self.format_constants(self.api.request_json(device.base_url, "/constants", "GET")))

    def interactive_fill_tank(self) -> None:
        device = self.choose_device("Start watering on")
        if device is None:
            return
        grams = self.prompt_float("Watering target grams", required=True)
        if grams is None:
            return
        self.report_operation(self.queue_fill(device.name, grams))

    def interactive_stop_watering(self) -> None:
        device = self.choose_device("Stop device")
        if device is not None:
            self.report_operation(self.queue_stop(device.name))

    def interactive_enable_sleep(self) -> None:
        device = self.choose_device("Enable sleep for")
        if device is not None:
            self.report_operation(self.queue_sleep(device.name, True))

    def interactive_disable_sleep(self) -> None:
        device = self.choose_device("Disable sleep for")
        if device is not None:
            self.report_operation(self.queue_sleep(device.name, False))

    def interactive_set_sleep_interval(self) -> None:
        device = self.choose_device("Set sleep interval for")
        if device is None:
            return
        minutes = self.prompt_int("Sleep interval minutes", required=True, max_value=MAX_SLEEP_INTERVAL_MIN)
        if minutes is not None:
            operation_id = self.queue_sleep_interval(device.name, minutes, confirm_retry_duplicate=True)
            if operation_id is not None:
                self.report_operation(operation_id)

    def interactive_set_zero(self) -> None:
        device = self.choose_device("Set zero for")
        if device is not None:
            self.report_operation(self.queue_zero(device.name))

    def interactive_calibrate_scale(self) -> None:
        device = self.choose_device("Calibrate scale for")
        if device is None:
            return
        weight_g = self.prompt_float("Known weight g", required=True)
        if weight_g is not None:
            self.report_operation(self.queue_calibration(device.name, weight_g))

    def interactive_clear_device_queue(self) -> None:
        device = self.choose_device("Clear queue for")
        if device is not None:
            self.clear_device_queue(device.name)

    def interactive_show_operations(self) -> None:
        rows = self.operations.list_recent()
        print(self.format_operations(rows))
        if not rows:
            return
        operation_id = self.prompt("Operation id for details, empty to return", "")
        if operation_id:
            print(self.format_operation_detail(self.operations.get(operation_id), self.operations.events(operation_id)))

    @staticmethod
    def read_password(confirm: bool = True) -> str:
        password = getpass.getpass("Password: ")
        if confirm:
            confirmation = getpass.getpass("Confirm password: ")
            if password != confirmation:
                raise SmartWateringError("passwords do not match")
        return password

    def add_user(self, username: str, password: str | None, replace: bool = False) -> None:
        user = self.auth.add_user(username, password if password is not None else self.read_password(), replace)
        action = "updated" if replace else "added"
        print(f"user {action}: {user.username}")

    def drop_user(self, username: str) -> None:
        self.auth.drop_user(username)
        print(f"user dropped: {username}")

    def interactive_add_user(self) -> None:
        username = self.prompt("Username")
        if not username:
            print("cancelled")
            return
        self.add_user(username, None)

    def interactive_drop_user(self) -> None:
        username = self.prompt("Username")
        if not username:
            print("cancelled")
            return
        confirmation = self.prompt(f"Type {username} to confirm removal")
        if confirmation != username:
            print("cancelled")
            return
        self.drop_user(username)

    def sync_detected_watering_history(
        self, days: int, device_name: str | None = None
    ) -> None:
        if days <= 0:
            raise SmartWateringError("days must be > 0")
        detector = PlantWateringDetector(
            self,
            os.environ.get(PROMETHEUS_URL_ENV, DEFAULT_PROMETHEUS_URL),
        )
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        results = (
            [detector.scan_device(device_name, start, end)]
            if device_name
            else detector.scan_all(start, end)
        )
        for result in results:
            print(
                f"device={result.device} "
                f"scanned_points={result.scanned_points} "
                f"detected={result.detected} created={result.created} "
                f"existing={result.existing}"
            )

    def interactive_sync_detected_watering_history(self) -> None:
        raw_days = self.prompt("Days to scan", "30")
        try:
            days = int(raw_days)
        except ValueError as exc:
            raise SmartWateringError("days must be an integer") from exc
        device_name = self.prompt("Device name, empty for all plants", "") or None
        self.sync_detected_watering_history(days, device_name)

    def hard_drop_detected_watering_history(
        self, device_name: str | None = None
    ) -> int:
        if device_name is not None:
            self.registry.get(device_name)
        deleted = self.plant_waterings.hard_drop(device_name)
        target = device_name or "all devices"
        print(f"hard dropped: {deleted} detected watering records ({target})")
        return deleted

    def interactive_hard_drop_detected_watering_history(self) -> None:
        device_name = self.prompt(
            "Device name, empty to hard drop all watering history", ""
        ) or None
        confirmation = "DROP ALL" if device_name is None else device_name
        typed = self.prompt(f"Type {confirmation} to confirm permanent deletion")
        if typed != confirmation:
            print("cancelled")
            return
        self.hard_drop_detected_watering_history(device_name)

    def run_interactive(self) -> int:
        actions = {
            "1": self.interactive_register_device,
            "2": lambda: print(self.format_devices(self.registry.list())),
            "3": self.interactive_remove_device,
            "4": self.interactive_configure_device,
            "5": self.interactive_show_status,
            "6": self.interactive_show_metrics,
            "7": self.interactive_show_constants,
            "8": self.interactive_fill_tank,
            "9": self.interactive_stop_watering,
            "10": self.interactive_enable_sleep,
            "11": self.interactive_disable_sleep,
            "12": self.interactive_set_sleep_interval,
            "13": self.interactive_set_zero,
            "14": self.interactive_calibrate_scale,
            "15": lambda: print(self.queue.format_status()),
            "16": self.interactive_clear_device_queue,
            "17": self.interactive_show_operations,
            "18": lambda: print(self.callback_url()),
            "19": self.interactive_add_user,
            "20": lambda: print(self.format_users(self.auth.list_users())),
            "21": self.interactive_drop_user,
            "22": self.interactive_sync_detected_watering_history,
            "23": self.interactive_hard_drop_detected_watering_history,
        }
        while True:
            self.clear_interactive_screen()
            print(self.format_main_menu())
            choice = input("Choice: ").strip()
            if choice in {"0", "q", "quit", "exit"}:
                return 0
            action = actions.get(choice)
            if action is None:
                self.show_interactive_message("error: unknown menu item")
                continue
            try:
                action()
                self.wait_for_interactive_continue()
            except SmartWateringError as exc:
                self.show_interactive_message(f"error: {exc}")

    def run(self, argv: list[str] | None = None) -> int:
        args = self.build_parser().parse_args(argv)
        try:
            if args.command is None:
                return self.run_interactive()
            if args.command == "devices":
                if args.device_command == "add":
                    return 0 if self.register_device(
                        args.ip, args.type, args.name, wait=not args.no_wait
                    ) else 1
                if args.device_command == "list":
                    print(self.format_devices(self.registry.list()))
                    return 0
                if args.device_command == "remove":
                    self.registry.remove(args.name)
                    print(f"removed: {args.name}")
                    return 0
                if args.device_command == "config":
                    config = parse_config_assignments(args.values)
                    device = self.registry.get(args.name)
                    desired_device = self.registry.validate_config_update(args.name, config)
                    operation_id = self.queue_device_config(
                        device,
                        config,
                        f"configure {desired_device.name}",
                        confirm_retry_duplicate=True,
                    )
                    if operation_id is None:
                        return 0
                    return 0 if self.report_operation(operation_id, wait=not args.no_wait) else 1
            if args.command == "status":
                device = self.registry.get(args.device)
                print(self.format_status(self.api.request_json(device.base_url, "/watering", "GET")))
                return 0
            if args.command == "constants":
                device = self.registry.get(args.device)
                print(self.format_constants(self.api.request_json(device.base_url, "/constants", "GET")))
                return 0
            if args.command == "metrics":
                device = self.registry.get(args.device)
                print(self.api.request_text(device.base_url, "/metrics", "GET"))
                return 0
            if args.command == "ping":
                device = self.registry.get(args.device)
                self.api.request_text(device.base_url, "/healthz", "GET")
                print(f"{device.name}: online")
                return 0
            if args.command == "fill":
                return 0 if self.report_operation(self.queue_fill(args.device, args.grams), wait=not args.no_wait) else 1
            if args.command == "stop":
                return 0 if self.report_operation(self.queue_stop(args.device), wait=not args.no_wait) else 1
            if args.command == "sleep":
                if args.sleep_command == "enable":
                    return 0 if self.report_operation(self.queue_sleep(args.device, True), wait=not args.no_wait) else 1
                if args.sleep_command == "disable":
                    return 0 if self.report_operation(self.queue_sleep(args.device, False), wait=not args.no_wait) else 1
                if args.sleep_command == "interval":
                    operation_id = self.queue_sleep_interval(args.device, args.minutes, confirm_retry_duplicate=True)
                    if operation_id is None:
                        return 0
                    return 0 if self.report_operation(operation_id, wait=not args.no_wait) else 1
            if args.command == "zero":
                return 0 if self.report_operation(self.queue_zero(args.device), wait=not args.no_wait) else 1
            if args.command == "calibration":
                return 0 if self.report_operation(self.queue_calibration(args.device, args.weight_g), wait=not args.no_wait) else 1
            if args.command == "queue":
                if args.queue_command == "clear":
                    self.clear_device_queue(args.device)
                    return 0
            if args.command == "users":
                if args.user_command == "add":
                    self.add_user(args.username, args.password, args.replace)
                    return 0
                if args.user_command == "list":
                    print(self.format_users(self.auth.list_users()))
                    return 0
                if args.user_command == "drop":
                    self.drop_user(args.username)
                    return 0
            if args.command == "pending":
                print(self.queue.format_status())
                return 0
            if args.command == "operations":
                if args.operation_id:
                    print(self.format_operation_detail(self.operations.get(args.operation_id), self.operations.events(args.operation_id)))
                else:
                    print(self.format_operations(self.operations.list_recent()))
                return 0
            if args.command == "watering-history":
                if args.watering_history_command == "sync":
                    self.sync_detected_watering_history(args.days, args.device)
                    return 0
                if args.watering_history_command == "drop":
                    if not args.yes:
                        raise SmartWateringError(
                            "watering-history drop requires --yes"
                        )
                    self.hard_drop_detected_watering_history(args.device)
                    return 0
                if args.watering_history_command == "rebuild":
                    if not args.yes:
                        raise SmartWateringError(
                            "watering-history rebuild requires --yes"
                        )
                    self.hard_drop_detected_watering_history(args.device)
                    self.sync_detected_watering_history(args.days, args.device)
                    return 0
                raise SmartWateringError("watering-history action is required")
            raise SmartWateringError("action is required")
        except SmartWateringError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1


def main() -> int:
    try:
        return SmartWateringCliApp().run()
    except SmartWateringError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
