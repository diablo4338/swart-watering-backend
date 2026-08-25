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
    discovered_device_config,
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
CALLBACK_CHECK_TIMEOUT_SEC = 60 * 60
CALLBACK_CHECK_DEVICE_WAIT_SEC = 5.0
CALLBACK_CHECK_POLL_INTERVAL_SEC = 0.1


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
        self.callback_check_timeout_sec = CALLBACK_CHECK_TIMEOUT_SEC
        self.callback_check_device_wait_sec = CALLBACK_CHECK_DEVICE_WAIT_SEC
        self.callback_check_poll_interval_sec = CALLBACK_CHECK_POLL_INTERVAL_SEC

    def _resolve_cli_device_id(self, selector: str) -> str:
        """Resolve a human CLI selector once; application services receive only IDs."""
        try:
            return self.registry.get_by_id(selector).id
        except SmartWateringError:
            return self.registry.get(selector).id

    def clear_device_queue(self, device_selector: str) -> int:
        return super().clear_device_queue(self._resolve_cli_device_id(device_selector))

    def queue_controller_name(self, device_selector: str, controller_name: str) -> str:
        return super().queue_controller_name(
            self._resolve_cli_device_id(device_selector), controller_name
        )

    def queue_fill(self, device_selector: str, grams: float) -> str:
        return super().queue_fill(self._resolve_cli_device_id(device_selector), grams)

    def queue_stop(self, device_selector: str) -> str:
        return super().queue_stop(self._resolve_cli_device_id(device_selector))

    def queue_sleep(self, device_selector: str, enabled: bool) -> str:
        return super().queue_sleep(self._resolve_cli_device_id(device_selector), enabled)

    def queue_sleep_interval(
        self, device_selector: str, minutes: int, confirm_retry_duplicate: bool = False
    ) -> str | None:
        return super().queue_sleep_interval(
            self._resolve_cli_device_id(device_selector), minutes, confirm_retry_duplicate
        )

    def queue_zero(self, device_selector: str) -> str:
        return super().queue_zero(self._resolve_cli_device_id(device_selector))

    def queue_calibration(self, device_selector: str, weight_g: float) -> str:
        return super().queue_calibration(
            self._resolve_cli_device_id(device_selector), weight_g
        )

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
    def _trace_safe(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: ("<redacted>" if key in {"callback_url", "authorization", "token"} else SmartWateringCliApp._trace_safe(item))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [SmartWateringCliApp._trace_safe(item) for item in value]
        return value

    def operation_trace(self, operation_id: str) -> dict[str, Any] | None:
        operation = self.operations.detail(operation_id)
        if operation is None:
            return None
        return {
            "operation": self._trace_safe(operation),
            "events": self._trace_safe(self.operations.events(operation_id)),
            "related_operations": self._trace_safe(self.operations.related(operation_id)),
        }

    @staticmethod
    def format_trace_event(event: dict[str, Any], started_at: float) -> str:
        occurred_at = float(event["created_at"])
        timestamp = datetime.fromtimestamp(occurred_at, timezone.utc).astimezone().isoformat(timespec="milliseconds")
        delta_ms = int(round((occurred_at - started_at) * 1000))
        line = (
            f"{timestamp}\t+{delta_ms}ms\t{event['source']}\t"
            f"{event['event_type']}\t{event['status']}\t{event['detail']}"
        )
        if event.get("data"):
            line += "\n  " + json.dumps(event["data"], ensure_ascii=False, sort_keys=True)
        return line

    @staticmethod
    def format_operation_trace(trace: dict[str, Any]) -> str:
        operation = trace["operation"]
        duration_end = operation.get("finished_at") or operation.get("updated_at")
        duration = max(0.0, float(duration_end) - float(operation["created_at"]))
        lines = [
            f"operation: {operation['operation_id']}",
            f"correlation: {operation['correlation_id']}",
            f"caused by: {operation.get('causation_id') or '-'}",
            f"device: {operation['device']}",
            f"type: {operation['type']}",
            f"status: {operation['status']}",
            f"duration: {duration:.3f}s",
            "payload: " + json.dumps(operation.get("payload") or {}, ensure_ascii=False, sort_keys=True),
            "result: " + json.dumps(operation.get("result"), ensure_ascii=False, sort_keys=True),
            "",
            "time\tdelta\tsource\tevent\tstatus\tdetail",
        ]
        lines.extend(
            SmartWateringCliApp.format_trace_event(event, float(operation["created_at"]))
            for event in trace["events"]
        )
        related = trace["related_operations"]
        if related:
            lines.extend(("", "related operations:"))
            lines.extend(
                f"{row['operation_id']}\t{OperationLog.public_type(row['operation_type'])}\t"
                f"{row['status']}\tcaused_by={row.get('causation_id') or '-'}"
                for row in related
            )
        return "\n".join(lines)

    def trace_operation(self, operation_id: str, follow: bool, as_json: bool) -> bool:
        trace = self.operation_trace(operation_id)
        if trace is None:
            print("failed: operation not found")
            return False
        if not follow:
            print(json.dumps(trace, ensure_ascii=False, indent=2, sort_keys=True) if as_json else self.format_operation_trace(trace))
            return True

        deadline = time.time() + self.operation_wait_timeout_sec
        shown_event_ids: set[int] = set()
        if not as_json:
            operation = trace["operation"]
            print(f"tracing {operation_id} ({operation['type']} on {operation['device']})")
        while True:
            trace = self.operation_trace(operation_id)
            if trace is None:
                print("failed: operation disappeared")
                return False
            if not as_json:
                for event in trace["events"]:
                    if event["id"] not in shown_event_ids:
                        print(self.format_trace_event(event, float(trace["operation"]["created_at"])))
                        shown_event_ids.add(event["id"])
            if trace["operation"]["status"] in OP_TERMINAL_STATUSES:
                if as_json:
                    print(json.dumps(trace, ensure_ascii=False, indent=2, sort_keys=True))
                return True
            if time.time() >= deadline:
                print(f"failed: trace did not finish within {self.operation_wait_timeout_sec}s")
                return False
            time.sleep(self.operation_poll_interval_sec)

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
            "", "Smart Watering", "",
            "1. Devices",
            "2. Read",
            "3. Watering",
            "4. Device actions",
            "5. Operations",
            "6. Callback",
            "7. Users",
            "8. Statistics",
            "", "0. Exit",
        ])

    @staticmethod
    def format_submenu(title: str, items: list[tuple[str, str, Any]]) -> str:
        return "\n".join([
            "", title, "",
            *(f"{key}. {label}" for key, label, _action in items),
            "", "0. Back",
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
            "add", help="Add a backend record without contacting the MCU"
        )
        add.add_argument("ip")
        add.add_argument("name")
        add.add_argument("--type", choices=sorted(DEVICE_TYPES), default=DeviceType.PLANT)
        discover = device_subparsers.add_parser(
            "discover", help="Queue read-only MCU discovery by address"
        )
        discover.add_argument("ip")
        device_subparsers.add_parser("list", help="List registered devices")
        remove = device_subparsers.add_parser("remove", help="Remove a registered device")
        remove.add_argument("name")
        config = device_subparsers.add_parser("config", help="Update device config")
        config.add_argument("name")
        config.add_argument("values", nargs="+")
        add_wait_argument(config)
        controller_id = device_subparsers.add_parser(
            "controller-id", help="Queue an MCU id change without changing the backend name"
        )
        controller_id.add_argument("name", help="Unique backend device name")
        controller_id.add_argument("controller_id", help="New id written to the MCU without validation")
        add_wait_argument(controller_id)
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
        trace = subparsers.add_parser("trace", help="Show a structured chronological operation trace")
        trace.add_argument("operation_id")
        trace.add_argument("--follow", action="store_true", help="Follow events until a terminal status")
        trace.add_argument("--json", action="store_true", help="Print machine-readable JSON")
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
            print(
                f"{index}. backend={device.name} MCU_ID={device.controller_name} "
                f"({device.device_type}) {device.ip}"
            )
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
        device_type = self.prompt_device_type(DeviceType.PLANT)
        backend_name = self.prompt("Backend name")
        if not backend_name:
            print("cancelled")
            return
        self.register_device(ip, device_type, backend_name)

    def interactive_discover_device(self) -> None:
        ip = self.prompt("IP address or base URL")
        if not ip:
            print("cancelled")
            return
        operation_id = self.discover_device(ip)
        print(f"queued read-only discovery: operation_id={operation_id}")

    @staticmethod
    def discovered_identity(status: dict[str, Any]) -> tuple[str, str]:
        name, device_type, _settings = discovered_device_config(status)
        return name, device_type

    @staticmethod
    def discovered_watering_settings(status: dict[str, Any]) -> dict[str, int]:
        _name, _device_type, settings = discovered_device_config(status)
        return settings

    def register_device(
        self,
        ip_or_url: str,
        device_type: str,
        backend_name: str,
    ) -> bool:
        device = self.registry.add(ip_or_url, device_type, backend_name)
        print(f"registered in backend only: {device.name} ({device.device_type}) {device.ip}")
        return True

    def discover_device(self, ip_or_url: str) -> str:
        operation_id = self.queue_device_discovery(ip_or_url)
        print("discovery is read-only; the worker will import the MCU identity and config")
        return operation_id

    def interactive_remove_device(self) -> None:
        device = self.choose_device("Remove device")
        if device is None:
            return
        confirmation = self.prompt(f"Type {device.name} to confirm removal")
        if confirmation != device.name:
            print("cancelled")
            return
        self.registry.remove(device.id)
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
            config["backend_name"] = new_name
        tare_weight_g = self.prompt_float("Tare weight g, empty to keep")
        if tare_weight_g is not None:
            config["tare_weight_g"] = tare_weight_g
        dry_weight_g = self.prompt_float("Dry weight g, empty to keep")
        if dry_weight_g is not None:
            config["dry_weight_g"] = dry_weight_g
        if not config:
            print("no changes")
            return
        desired_device = self.registry.validate_config_update(device.id, config)
        operation_id = self.queue_device_config(
            device,
            config,
            f"configure {desired_device.name}",
            confirm_retry_duplicate=True,
        )
        if operation_id is None:
            return
        self.report_operation(operation_id)

    def interactive_change_controller_id(self) -> None:
        device = self.choose_device("Change MCU ID")
        if device is None:
            return
        controller_id = self.prompt("New MCU ID", device.controller_name)
        operation_id = self.queue_controller_name(device.id, controller_id)
        self.report_operation(operation_id)

    def _first_received_callback(
        self, probes: dict[str, Device],
    ) -> tuple[str, Device, dict[str, Any]] | None:
        received: list[tuple[float, str, Device, dict[str, Any]]] = []
        for operation_id, device in probes.items():
            for event in self.operations.events(operation_id):
                if event["event_type"] == "callback.received":
                    received.append((event["created_at"], operation_id, device, event))
                    break
        if not received:
            return None
        _created_at, operation_id, device, event = min(received, key=lambda item: item[0])
        return operation_id, device, event

    def interactive_check_callback(self) -> None:
        devices = self.registry.list()
        if not devices:
            raise SmartWateringError("no registered devices")

        started_at = time.monotonic()
        deadline = started_at + self.callback_check_timeout_sec
        probes: dict[str, Device] = {}
        probe_ids_by_device: dict[str, str] = {}
        attempt = 0
        print(f"callback URL: {self.callback_url()}")
        print(
            f"checking {len(devices)} device(s) for up to "
            f"{self.callback_check_timeout_sec:.0f}s"
        )

        while time.monotonic() < deadline:
            for registered_device in devices:
                if time.monotonic() >= deadline:
                    break
                attempt += 1
                # Resolve the record again on every pass, then use this exact
                # device's URL for both the read and the no-op config write.
                device = self.registry.get(registered_device.name)
                print(
                    f"probe {attempt}: backend={device.name} "
                    f"MCU_ID={device.controller_name} address={device.base_url}"
                )
                operation_id: str | None = None
                device_deadline = min(
                    deadline,
                    time.monotonic() + self.callback_check_device_wait_sec,
                )
                previous_api_timeout = getattr(self.api, "timeout_sec", None)
                try:
                    if previous_api_timeout is not None:
                        self.api.timeout_sec = max(
                            0.1,
                            min(previous_api_timeout, device_deadline - time.monotonic()),
                        )
                    status = self.api.request_json(device.base_url, "/watering", "GET")
                    status_device = status.get("device") if isinstance(status, dict) else None
                    actual_type = status_device.get("type") if isinstance(status_device, dict) else None
                    actual_mcu_id = status_device.get("name") if isinstance(status_device, dict) else None
                    if actual_type not in DEVICE_TYPES:
                        raise SmartWateringError(
                            f"controller returned unsupported device type: {actual_type}"
                        )
                    if time.monotonic() >= device_deadline:
                        raise RetryableDeviceApiError("device probe window expired after status read")

                    operation_id = probe_ids_by_device.get(device.id)
                    if operation_id is None:
                        operation_id = self.operations.create(
                            device.id,
                            "callback_probe",
                            {"device_type": actual_type},
                        )
                        probe_ids_by_device[device.id] = operation_id
                        probes[operation_id] = device
                    payload = self.build_operation_payload(
                        operation_id,
                        {"device_type": actual_type},
                    )
                    self.operations.update_payload(operation_id, payload)
                    if previous_api_timeout is not None:
                        self.api.timeout_sec = max(
                            0.1,
                            min(previous_api_timeout, device_deadline - time.monotonic()),
                        )
                    response = self.api.request_json(device.base_url, "/config", "POST", payload)
                    self.operations.update_result(operation_id, response)
                    self.operations.event(
                        operation_id,
                        "accepted",
                        "callback probe accepted by controller",
                        source="controller",
                        event_type="command.accepted",
                    )
                    print(
                        f"  request accepted: actual_MCU_ID={actual_mcu_id} "
                        f"device_type={actual_type}; waiting for callback"
                    )
                except SmartWateringError as exc:
                    print(f"  request failed: {exc}")
                finally:
                    if previous_api_timeout is not None:
                        self.api.timeout_sec = previous_api_timeout

                wait_deadline = device_deadline
                while True:
                    received = self._first_received_callback(probes)
                    if received is not None:
                        received_operation_id, received_device, event = received
                        data = event.get("data") or {}
                        elapsed = time.monotonic() - started_at
                        print("callback check: SUCCESS")
                        print(
                            f"device: backend={received_device.name} "
                            f"MCU_ID={received_device.controller_name}"
                        )
                        print(f"operation_id: {received_operation_id}")
                        print(f"callback status: {data.get('status', 'unknown')}")
                        print(f"elapsed: {elapsed:.1f}s")
                        return
                    if time.monotonic() >= wait_deadline:
                        break
                    time.sleep(self.callback_check_poll_interval_sec)

                if operation_id is not None:
                    self.operations.trace_event(
                        operation_id,
                        "cli",
                        "callback_probe.no_callback",
                        f"callback was not received within {self.callback_check_device_wait_sec:g}s",
                        {"attempt": attempt},
                    )
                print("  no callback; trying next device")

        for operation_id in probes:
            operation = self.operations.get(operation_id)
            if operation is not None and operation["status"] not in OP_TERMINAL_STATUSES:
                self.operations.event(
                    operation_id,
                    OP_TIMEOUT,
                    f"callback check timed out after {self.callback_check_timeout_sec:.0f}s",
                    source="cli",
                    event_type="operation.timed_out",
                )
        print(
            "callback check: TIMEOUT - no controller callback received within "
            f"{self.callback_check_timeout_sec:.0f}s"
        )

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
        self.report_operation(self.queue_fill(device.id, grams))

    def interactive_stop_watering(self) -> None:
        device = self.choose_device("Stop device")
        if device is not None:
            self.report_operation(self.queue_stop(device.id))

    def interactive_enable_sleep(self) -> None:
        device = self.choose_device("Enable sleep for")
        if device is not None:
            self.report_operation(self.queue_sleep(device.id, True))

    def interactive_disable_sleep(self) -> None:
        device = self.choose_device("Disable sleep for")
        if device is not None:
            self.report_operation(self.queue_sleep(device.id, False))

    def interactive_set_sleep_interval(self) -> None:
        device = self.choose_device("Set sleep interval for")
        if device is None:
            return
        minutes = self.prompt_int("Sleep interval minutes", required=True, max_value=MAX_SLEEP_INTERVAL_MIN)
        if minutes is not None:
            operation_id = self.queue_sleep_interval(device.id, minutes, confirm_retry_duplicate=True)
            if operation_id is not None:
                self.report_operation(operation_id)

    def interactive_set_zero(self) -> None:
        device = self.choose_device("Set zero for")
        if device is not None:
            self.report_operation(self.queue_zero(device.id))

    def interactive_calibrate_scale(self) -> None:
        device = self.choose_device("Calibrate scale for")
        if device is None:
            return
        weight_g = self.prompt_float("Known weight g", required=True)
        if weight_g is not None:
            self.report_operation(self.queue_calibration(device.id, weight_g))

    def interactive_clear_device_queue(self) -> None:
        device = self.choose_device("Clear queue for")
        if device is not None:
            self.clear_device_queue(device.id)

    def interactive_show_operations(self) -> None:
        rows = self.operations.list_recent()
        print(self.format_operations(rows))
        if not rows:
            return
        operation_id = self.prompt("Operation id for details, empty to return", "")
        if operation_id:
            print(self.format_operation_detail(self.operations.get(operation_id), self.operations.events(operation_id)))

    def interactive_trace_operation(self) -> None:
        operation_id = self.prompt("Operation id")
        if not operation_id:
            print("cancelled")
            return
        trace = self.operation_trace(operation_id)
        if trace is None:
            print("operation: not found")
            return
        print(self.format_operation_trace(trace))

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
            [detector.scan_device(self.registry.get(device_name).id, start, end)]
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
        device_id = self.registry.get(device_name).id if device_name is not None else None
        deleted = self.plant_waterings.hard_drop(device_id)
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

    def run_interactive_submenu(
        self, title: str, items: list[tuple[str, str, Any]],
    ) -> None:
        actions = {key: action for key, _label, action in items}
        while True:
            self.clear_interactive_screen()
            print(self.format_submenu(title, items))
            choice = input("Choice: ").strip().lower()
            if choice in {"0", "b", "back", "q", "quit"}:
                return
            action = actions.get(choice)
            if action is None:
                self.show_interactive_message("error: unknown menu item")
                continue
            try:
                action()
                self.wait_for_interactive_continue()
            except SmartWateringError as exc:
                self.show_interactive_message(f"error: {exc}")

    def run_interactive(self) -> int:
        menus = {
            "1": ("Devices", [
                ("1", "Add backend device", self.interactive_register_device),
                ("2", "Discover device (read-only)", self.interactive_discover_device),
                ("3", "List devices", lambda: print(self.format_devices(self.registry.list()))),
                ("4", "Remove device", self.interactive_remove_device),
                ("5", "Configure device", self.interactive_configure_device),
                ("6", "Change MCU ID", self.interactive_change_controller_id),
            ]),
            "2": ("Read", [
                ("1", "Show device status", self.interactive_show_status),
                ("2", "Show device metrics", self.interactive_show_metrics),
                ("3", "Show device constants", self.interactive_show_constants),
            ]),
            "3": ("Watering", [
                ("1", "Start watering", self.interactive_fill_tank),
                ("2", "Stop watering", self.interactive_stop_watering),
            ]),
            "4": ("Device actions", [
                ("1", "Enable sleep", self.interactive_enable_sleep),
                ("2", "Disable sleep", self.interactive_disable_sleep),
                ("3", "Set sleep interval", self.interactive_set_sleep_interval),
                ("4", "Set zero", self.interactive_set_zero),
                ("5", "Calibrate scale", self.interactive_calibrate_scale),
            ]),
            "5": ("Operations", [
                ("1", "Show pending queue", lambda: print(self.queue.format_status())),
                ("2", "Clear device queue", self.interactive_clear_device_queue),
                ("3", "Show recent operations", self.interactive_show_operations),
                ("4", "Trace operation", self.interactive_trace_operation),
            ]),
            "6": ("Callback", [
                ("1", "Show callback URL", lambda: print(self.callback_url())),
                ("2", "Check callback", self.interactive_check_callback),
            ]),
            "7": ("Users", [
                ("1", "Add API user", self.interactive_add_user),
                ("2", "List API users", lambda: print(self.format_users(self.auth.list_users()))),
                ("3", "Drop API user", self.interactive_drop_user),
            ]),
            "8": ("Statistics", [
                ("1", "Sync detected watering history", self.interactive_sync_detected_watering_history),
                ("2", "Hard drop detected watering history", self.interactive_hard_drop_detected_watering_history),
            ]),
        }
        while True:
            self.clear_interactive_screen()
            print(self.format_main_menu())
            choice = input("Choice: ").strip().lower()
            if choice in {"0", "q", "quit", "exit"}:
                return 0
            menu = menus.get(choice)
            if menu is None:
                self.show_interactive_message("error: unknown menu item")
                continue
            self.run_interactive_submenu(*menu)

    def run(self, argv: list[str] | None = None) -> int:
        args = self.build_parser().parse_args(argv)
        try:
            if args.command is None:
                return self.run_interactive()
            if args.command == "devices":
                if args.device_command == "add":
                    return 0 if self.register_device(args.ip, args.type, args.name) else 1
                if args.device_command == "discover":
                    self.discover_device(args.ip)
                    return 0
                if args.device_command == "list":
                    print(self.format_devices(self.registry.list()))
                    return 0
                if args.device_command == "remove":
                    self.registry.remove(self.registry.get(args.name).id)
                    print(f"removed: {args.name}")
                    return 0
                if args.device_command == "config":
                    config = parse_config_assignments(args.values)
                    device = self.registry.get(args.name)
                    desired_device = self.registry.validate_config_update(device.id, config)
                    operation_id = self.queue_device_config(
                        device,
                        config,
                        f"configure {desired_device.name}",
                        confirm_retry_duplicate=True,
                    )
                    if operation_id is None:
                        return 0
                    return 0 if self.report_operation(operation_id, wait=not args.no_wait) else 1
                if args.device_command == "controller-id":
                    operation_id = self.queue_controller_name(
                        self.registry.get(args.name).id, args.controller_id
                    )
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
                return 0 if self.report_operation(self.queue_fill(self.registry.get(args.device).id, args.grams), wait=not args.no_wait) else 1
            if args.command == "stop":
                return 0 if self.report_operation(self.queue_stop(self.registry.get(args.device).id), wait=not args.no_wait) else 1
            if args.command == "sleep":
                if args.sleep_command == "enable":
                    return 0 if self.report_operation(self.queue_sleep(self.registry.get(args.device).id, True), wait=not args.no_wait) else 1
                if args.sleep_command == "disable":
                    return 0 if self.report_operation(self.queue_sleep(self.registry.get(args.device).id, False), wait=not args.no_wait) else 1
                if args.sleep_command == "interval":
                    operation_id = self.queue_sleep_interval(self.registry.get(args.device).id, args.minutes, confirm_retry_duplicate=True)
                    if operation_id is None:
                        return 0
                    return 0 if self.report_operation(operation_id, wait=not args.no_wait) else 1
            if args.command == "zero":
                return 0 if self.report_operation(self.queue_zero(self.registry.get(args.device).id), wait=not args.no_wait) else 1
            if args.command == "calibration":
                return 0 if self.report_operation(self.queue_calibration(self.registry.get(args.device).id, args.weight_g), wait=not args.no_wait) else 1
            if args.command == "queue":
                if args.queue_command == "clear":
                    self.clear_device_queue(self.registry.get(args.device).id)
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
            if args.command == "trace":
                return 0 if self.trace_operation(args.operation_id, args.follow, args.json) else 1
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
