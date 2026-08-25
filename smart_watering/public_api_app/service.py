import time
from copy import deepcopy
from datetime import datetime, timedelta
from threading import RLock
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
from .presence import DevicePresenceRegistry


LIVE_STATE_REQUEST_TIMEOUT_SEC = 3


class DeviceRuntimeState:
    """Validated, process-local owner of the latest observed state per device."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._states: dict[str, tuple[tuple[Any, Any], dict[str, Any]]] = {}

    def update_from_snapshot(
        self, device_id: str, revision: tuple[Any, Any], state: dict[str, Any]
    ) -> dict[str, Any]:
        serialized = self.serialize(state)
        with self._lock:
            self._states[device_id] = (revision, serialized)
        return deepcopy(serialized)

    def read(
        self, device_id: str, revision: tuple[Any, Any], recover: Any
    ) -> dict[str, Any]:
        with self._lock:
            cached = self._states.get(device_id)
        if cached is not None and cached[0] == revision:
            try:
                return deepcopy(self.serialize(cached[1]))
            except (TypeError, ValueError):
                pass
        return self.update_from_snapshot(device_id, revision, recover())

    @classmethod
    def apply_callback_patches(
        cls, state: dict[str, Any], patches: list[dict[str, Any]]
    ) -> dict[str, Any]:
        result = deepcopy(state)

        def merge(target: dict[str, Any], delta: dict[str, Any]) -> None:
            for key, value in delta.items():
                if isinstance(target.get(key), dict) and isinstance(value, dict):
                    merge(target[key], value)
                else:
                    target[key] = value

        for item in patches:
            merge(result, item["patch"])
        return cls.serialize(result)

    @staticmethod
    def serialize(state: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(state, dict):
            raise TypeError("device state must be an object")
        required = {"device", "watering", "config", "weight"}
        if not required.issubset(state):
            raise ValueError("device state is incomplete")
        if not all(isinstance(state[key], dict) for key in required):
            raise TypeError("device state sections must be objects")
        return deepcopy(state)


class DeviceStateProjectionService:
    def __init__(
        self,
        business: SmartWateringService,
        prometheus_url: str,
        statistics_timezone: ZoneInfo,
        consumption_drop_threshold_percent: int = 30,
        consumption_median_days: int = 5,
        presence: DevicePresenceRegistry | None = None,
    ) -> None:
        self.business = business
        self.prometheus = PrometheusClient(prometheus_url)
        self.statistics_timezone = statistics_timezone
        self.consumption_drop_threshold_percent = consumption_drop_threshold_percent
        self.consumption_median_days = consumption_median_days
        self.presence = presence or DevicePresenceRegistry()
        self.runtime_state = DeviceRuntimeState()

    @staticmethod
    def project_device_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
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

    def project_snapshot_device_state(self, device_id: str) -> dict[str, Any]:
        device = self.business.registry.get_by_id(device_id)
        latest = self.business.operations.latest_successful_result(device.id, "device_status")
        if latest is not None and latest["result"] is not None:
            snapshot_updated_at = latest.get("updated_at") or latest["result_received_at"]
            patches = self.business.operations.confirmed_snapshot_patches_since(
                device.id, snapshot_updated_at
            )
            revision = (
                latest["operation_id"],
                patches[-1]["operation_id"] if patches else snapshot_updated_at,
            )
            result = self.runtime_state.read(
                device.id,
                revision,
                lambda: self.runtime_state.apply_callback_patches(
                    self.project_device_snapshot(latest["result"]), patches
                ),
            )
            return self.project_available_device_state(
                device_id=device.id,
                device_name=device.name,
                source=DeviceStatusSource.SNAPSHOT,
                result=result,
                result_received_at=latest["result_received_at"],
                operation_id=latest["operation_id"],
            )
        return self.project_unavailable_device_state(
            device_id=device.id,
            device_name=device.name,
            exc=SmartWateringError(
                f"no stored status snapshot exists for device '{device.name}'"
            ),
            error_code="device_status_snapshot_not_found",
        )

    def project_current_device_state(self, device_id: str) -> dict[str, Any]:
        """Project the latest stored MCU state with independently monitored presence."""
        presence = self.presence.get(device_id)
        return {
            **self.project_snapshot_device_state(device_id),
            "status": DeviceStatus(presence.state),
            "online": presence.online,
            "presence_checked_at": presence.checked_at,
        }

    def project_live_device_state(self, device_id: str) -> dict[str, Any]:
        device = self.business.registry.get_by_id(device_id)
        try:
            return self._request_live_device_state(device)
        except SmartWateringError as exc:
            return self.project_unavailable_device_state(device.id, device.name, exc)

    def _request_live_device_state(
        self,
        device: Any,
    ) -> dict[str, Any]:
        previous_timeout = getattr(self.business.api, "timeout_sec", None)
        try:
            if previous_timeout is not None:
                self.business.api.timeout_sec = LIVE_STATE_REQUEST_TIMEOUT_SEC
            result = self.business.api.request_json(device.base_url, "/watering", "GET")
            config = result.get("config") if isinstance(result, dict) else None
            if isinstance(config, dict):
                self.business.registry.confirm_watering_settings(
                    device.id, config, time.time()
                )
            return self.project_available_device_state(
                device_id=device.id,
                device_name=device.name,
                source=DeviceStatusSource.LIVE,
                result=self.project_device_snapshot(result),
                result_received_at=time.time(),
                operation_id=None,
            )
        finally:
            if previous_timeout is not None:
                self.business.api.timeout_sec = previous_timeout

    @staticmethod
    def project_available_device_state(
        device_id: str,
        device_name: str,
        source: DeviceStatusSource,
        result: dict[str, Any],
        result_received_at: float,
        operation_id: str | None,
    ) -> dict[str, Any]:
        return {
            "device_id": device_id,
            "device_name": device_name,
            "status": (
                DeviceStatus.ONLINE
                if source is DeviceStatusSource.LIVE
                else DeviceStatus.OFFLINE
            ),
            "source": source, "available": True,
            "result": result, "result_received_at": result_received_at,
            "operation_id": operation_id,
            "error": None,
        }

    @staticmethod
    def project_unavailable_device_state(
        device_id: str,
        device_name: str,
        exc: SmartWateringError,
        error_code: str = "device_status_unavailable",
    ) -> dict[str, Any]:
        return {
            "device_id": device_id,
            "device_name": device_name,
            "status": DeviceStatus.OFFLINE,
            "source": DeviceStatusSource.NONE,
            "available": False,
            "result": None,
            "result_received_at": None,
            "operation_id": None,
            "error": {
                "code": error_code,
                "message": str(exc),
                "retryable": True,
            },
        }

    def project_water_consumption(self, device_id: str) -> dict[str, Any]:
        device = self.business.registry.get_by_id(device_id)
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
        return {"device_id": device.id, "device_name": device.name, "days": list(rows.values())[:7]}

    def project_watering_history(
        self, device_id: str, limit: int, offset: int
    ) -> dict[str, Any]:
        device = self.business.registry.get_by_id(device_id)
        if device.device_type != "plant":
            raise PublicApiError(
                f"device '{device.name}' is not a plant",
                404,
                "not_a_plant",
            )
        events, has_more = self.business.plant_waterings.list_valid_page(
            device.id, limit, offset
        )
        return {
            "device_id": device.id,
            "device_name": device.name,
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

    def delete_watering_history_item(
        self, device_id: str, event_id: int
    ) -> dict[str, Any]:
        device = self.business.registry.get_by_id(device_id)
        if not self.business.plant_waterings.invalidate(device.id, event_id):
            raise PublicApiError(
                f"detected watering '{event_id}' does not exist",
                404,
                "detected_watering_not_found",
            )
        return {"id": event_id, "invalid": True}

    def set_watering_history_fertilized(
        self, device_id: str, event_id: int, fertilized: bool
    ) -> dict[str, Any]:
        device = self.business.registry.get_by_id(device_id)
        if device.device_type != "plant":
            raise PublicApiError(
                f"device '{device.name}' is not a plant", 404, "not_a_plant"
            )
        event = self.business.plant_waterings.set_fertilized(
            device.id, event_id, fertilized
        )
        if event is None:
            raise PublicApiError(
                f"detected watering '{event_id}' does not exist",
                404,
                "detected_watering_not_found",
            )
        return {"id": event_id, "fertilized": event["fertilized"]}

