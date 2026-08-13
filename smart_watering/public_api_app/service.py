import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from smart_watering.application.service import SmartWateringService
from smart_watering.domain import SmartWateringError

from .domain import DeviceStatus, DeviceStatusSource, number_or_none
from .errors import PublicApiError
from .statistics import (
    WATER_WEIGHT_METRIC,
    PrometheusClient,
    adaptive_weight_change_per_hour,
    consumption_is_below_median,
    prometheus_instance,
    prometheus_string,
    water_consumption_periods,
    water_consumption_query_end,
)


LATEST_STATUS_LIVE_TIMEOUT_SEC = 3
CONTROL_OPERATION_TYPES = {
    "config", "sleep_enable", "sleep_disable", "sleep_interval",
    "zero_capture", "scale_calibration",
}


class PublicApiService:
    def __init__(
        self,
        app: SmartWateringService,
        prometheus_url: str,
        statistics_timezone: ZoneInfo,
        consumption_drop_threshold_percent: int = 30,
        consumption_median_days: int = 5,
    ) -> None:
        self.app = app
        self.prometheus = PrometheusClient(prometheus_url)
        self.statistics_timezone = statistics_timezone
        self.consumption_drop_threshold_percent = consumption_drop_threshold_percent
        self.consumption_median_days = consumption_median_days

    def operation_response(self, operation_id: str) -> dict[str, Any]:
        operation = self.app.operations.detail(operation_id)
        if operation is None:
            raise PublicApiError(
                f"operation '{operation_id}' does not exist",
                404,
                "operation_not_found",
            )
        return self.operation_response_from_detail(operation)

    @staticmethod
    def operation_response_from_detail(operation: dict[str, Any]) -> dict[str, Any]:
        payload = operation.get("payload")
        response = {
            key: operation.get(key)
            for key in ("operation_id", "device", "type", "status", "updated_at", "finished_at")
        }
        target_g = operation.get("target_g")
        if not isinstance(target_g, (int, float)) and isinstance(payload, dict):
            target_g = payload.get("target_g")
        if isinstance(target_g, (int, float)):
            response["target_g"] = target_g
        if isinstance(payload, dict):
            for key in (
                "minutes", "weight_g", "device_type", "name", "dry_weight_g",
                "tare_weight_g", "wet_weight_g", "watering_loss_threshold_percent",
            ):
                if key in payload:
                    response[key] = payload[key]
        if operation.get("error") is not None:
            response["error"] = operation["error"]
        return response

    def operations_response(
        self,
        operations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            self.operation_response_from_detail(operation)
            for operation in self.app.operations.details_from_operations(operations)
        ]

    def operation_events_response(self, operation_id: str) -> dict[str, Any]:
        if self.app.operations.get(operation_id) is None:
            raise PublicApiError(
                f"operation '{operation_id}' does not exist",
                404,
                "operation_not_found",
            )
        return {
            "operation_id": operation_id,
            "events": [
                {"status": event["status"], "message": event["detail"]}
                for event in self.app.operations.events(operation_id)
            ],
        }

    def planned_watering_response(self, device_name: str) -> dict[str, Any] | None:
        operation = self.app.operations.latest_non_terminal_watering_start(device_name)
        if operation is None:
            return None
        return {
            "operation_id": operation["operation_id"],
            "target_g": operation["target_g"],
            "status": operation["status"],
        }

    def last_watering_response(self, device_name: str) -> dict[str, Any]:
        operation = self.app.operations.latest_terminal_watering_start(device_name)
        return (
            {"operation": None}
            if operation is None
            else {"operation": self.operation_response(operation["operation_id"])}
        )

    def watering_history_response(self, successful_only: bool = False) -> dict[str, Any]:
        return {
            "operations": [
                self.operation_response(operation["operation_id"])
                for operation in self.app.operations.list_recent_watering_starts(
                    limit=10, successful_only=successful_only
                )
            ]
        }

    @staticmethod
    def raw_status_response(payload: dict[str, Any]) -> dict[str, Any]:
        device = payload.get("device") or {}
        watering = payload.get("watering") or {}
        config = payload.get("config") or {}
        weight = payload.get("weight") or {}
        return {
            "device": {"name": device.get("name"), "type": device.get("type")},
            "watering": {
                "active": bool(watering.get("active", False)),
                "state": watering.get("state"),
                "last_operation_type": watering.get("last_operation_type"),
                "last_operation_status": watering.get("last_operation_status"),
            },
            "config": {
                "target_g": number_or_none(config.get("target_g")),
                "dry_weight_g": number_or_none(config.get("dry_weight_g")),
                "wet_weight_g": number_or_none(config.get("wet_weight_g")),
                "watering_loss_threshold_percent": number_or_none(
                    config.get("watering_loss_threshold_percent")
                ),
                "tare_weight_g": number_or_none(config.get("tare_weight_g")),
                "zero_raw": number_or_none(config.get("zero_raw")),
                "raw_per_gram": number_or_none(config.get("raw_per_gram")),
                "sleep_disabled": config.get("sleep_disabled"),
                "sleep_interval_min": config.get("sleep_interval_min"),
            },
            "weight": {
                "gross_weight_g": number_or_none(weight.get("gross_weight_g")),
                "useful_weight_g": number_or_none(weight.get("useful_weight_g")),
                "water_used_g": number_or_none(weight.get("water_used_g")),
            },
        }

    def latest_device_status_response(self, device_name: str) -> dict[str, Any]:
        device = self.app.registry.get(device_name)
        pending = self.pending_status_operation(device.name)
        latest = self.app.operations.latest_successful_result(device.name, "device_status")
        if latest is not None and latest["result"] is not None:
            return self.available_status_response(
                device_name=device.name,
                source=DeviceStatusSource.SNAPSHOT,
                result=self.raw_status_response(latest["result"]),
                result_received_at=latest["result_received_at"],
                operation_id=latest["operation_id"],
                pending=pending,
            )
        return self.unavailable_status_response(
            device_name=device.name,
            pending=pending,
            exc=SmartWateringError(
                f"no stored status snapshot exists for device '{device.name}'"
            ),
            error_code="device_status_snapshot_not_found",
        )

    def live_device_status_response(self, device_name: str) -> dict[str, Any]:
        device = self.app.registry.get(device_name)
        pending = self.pending_status_operation(device.name)
        try:
            return self._live_device_status_response(device, pending)
        except SmartWateringError as exc:
            return self.unavailable_status_response(device.name, pending, exc)

    def _live_device_status_response(
        self,
        device: Any,
        pending: dict[str, Any] | None,
    ) -> dict[str, Any]:
        previous_timeout = getattr(self.app.api, "timeout_sec", None)
        try:
            if previous_timeout is not None:
                self.app.api.timeout_sec = LATEST_STATUS_LIVE_TIMEOUT_SEC
            result = self.app.api.request_json(device.base_url, "/watering", "GET")
            return self.available_status_response(
                device_name=device.name,
                source=DeviceStatusSource.LIVE,
                result=self.raw_status_response(result),
                result_received_at=time.time(),
                operation_id=None,
                pending=pending,
            )
        finally:
            if previous_timeout is not None:
                self.app.api.timeout_sec = previous_timeout

    @staticmethod
    def pending_status_operation_fields(
        pending: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "pending_operation_id": pending["operation_id"] if pending else None,
            "pending_operation_status": pending["status"] if pending else None,
        }

    def pending_status_operation(self, device_name: str) -> dict[str, Any] | None:
        return self.app.operations.latest_non_terminal(device_name, "device_status")

    @classmethod
    def available_status_response(
        cls,
        device_name: str,
        source: DeviceStatusSource,
        result: dict[str, Any],
        result_received_at: float,
        operation_id: str | None,
        pending: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "device": device_name,
            "status": (
                DeviceStatus.ONLINE
                if source is DeviceStatusSource.LIVE
                else DeviceStatus.UNKNOWN
            ),
            "source": source, "available": True,
            "result": result, "result_received_at": result_received_at,
            "operation_id": operation_id,
            **cls.pending_status_operation_fields(pending),
            "error": None,
        }

    @classmethod
    def unavailable_status_response(
        cls,
        device_name: str,
        pending: dict[str, Any] | None,
        exc: SmartWateringError,
        error_code: str = "device_status_unavailable",
    ) -> dict[str, Any]:
        return {
            "device": device_name,
            "status": DeviceStatus.OFFLINE,
            "source": DeviceStatusSource.NONE,
            "available": False,
            "result": None,
            "result_received_at": None,
            "operation_id": None,
            **cls.pending_status_operation_fields(pending),
            "error": {
                "code": error_code,
                "message": str(exc),
                "retryable": True,
            },
        }

    @staticmethod
    def device_to_json(device: Any, pending_devices: set[str] | None = None) -> dict[str, Any]:
        return {
            "name": device.name,
            "type": device.device_type,
            "has_pending_operations": device.name in (pending_devices or set()),
        }

    def water_consumption_response(self, device_name: str) -> dict[str, Any]:
        device = self.app.registry.get(device_name)
        if device.device_type != "plant":
            raise PublicApiError(
                f"device '{device.name}' is not a plant; water consumption "
                "statistics are only available for plant devices",
                404,
                "not_a_plant",
            )
        instance = prometheus_instance(device.base_url)
        selector = f'{WATER_WEIGHT_METRIC}{{instance="{prometheus_string(instance)}"}}'
        now = datetime.now(self.statistics_timezone)
        rows: dict[Any, dict[str, Any]] = {}
        completed_periods: list[tuple[datetime, str, float | None]] = []
        history_days = max(7, self.consumption_median_days + 2)
        for period_date, period_name, start, end in water_consumption_periods(
            now, history_days
        ):
            row = rows.setdefault(
                period_date,
                {
                    "date": period_date.isoformat(),
                    "day": None,
                    "night": None,
                    "day_below_weekly_median": False,
                    "night_below_weekly_median": False,
                },
            )
            query_end = water_consumption_query_end(start, end, now)
            if query_end is None:
                continue
            samples = self.prometheus.range_samples(selector, start, query_end)
            value = None
            if samples:
                value = round(adaptive_weight_change_per_hour(samples), 2)
                row[period_name] = value
            if end <= now:
                completed_periods.append((end, period_name, value))

        if completed_periods:
            latest_end, latest_name, latest_value = max(
                completed_periods, key=lambda period: period[0]
            )
            previous_values = [
                value
                for end, name, value in completed_periods
                if (
                    name == latest_name
                    and latest_end - timedelta(days=self.consumption_median_days)
                    <= end < latest_end
                    and value is not None
                )
            ]
            latest_date = (
                latest_end.date()
                if latest_name == "day"
                else (latest_end - timedelta(days=1)).date()
            )
            if latest_value is not None:
                rows[latest_date][f"{latest_name}_below_weekly_median"] = (
                    consumption_is_below_median(
                        latest_value,
                        previous_values,
                        self.consumption_drop_threshold_percent,
                    )
                )
        return {"device": device_name, "days": list(rows.values())[:7]}

    def detected_waterings_response(
        self, device_name: str, limit: int, offset: int
    ) -> dict[str, Any]:
        device = self.app.registry.get(device_name)
        if device.device_type != "plant":
            raise PublicApiError(
                f"device '{device.name}' is not a plant",
                404,
                "not_a_plant",
            )
        events, has_more = self.app.plant_waterings.list_valid_page(
            device.name, limit, offset
        )
        return {
            "device": device.name,
            "waterings": [
                {
                    key: event[key]
                    for key in (
                        "id", "occurred_at", "weight_before_g",
                        "weight_after_g", "amount_g", "source",
                        "fertilized",
                    )
                }
                for event in events
            ],
            "next_offset": offset + len(events) if has_more else None,
        }

    def invalidate_detected_watering(
        self, device_name: str, event_id: int
    ) -> dict[str, Any]:
        device = self.app.registry.get(device_name)
        if not self.app.plant_waterings.invalidate(device.name, event_id):
            raise PublicApiError(
                f"detected watering '{event_id}' does not exist",
                404,
                "detected_watering_not_found",
            )
        return {"id": event_id, "invalid": True}

    def set_detected_watering_fertilized(
        self, device_name: str, event_id: int, fertilized: bool
    ) -> dict[str, Any]:
        device = self.app.registry.get(device_name)
        if device.device_type != "plant":
            raise PublicApiError(
                f"device '{device.name}' is not a plant", 404, "not_a_plant"
            )
        event = self.app.plant_waterings.set_fertilized(
            device.name, event_id, fertilized
        )
        if event is None:
            raise PublicApiError(
                f"detected watering '{event_id}' does not exist",
                404,
                "detected_watering_not_found",
            )
        return {"id": event_id, "fertilized": event["fertilized"]}
