import time
from typing import Any

from smart_watering.domain import DEVICE_TYPES, OP_CANCELLED

from .errors import PublicApiError


class DeviceCardService:
    """Server-owned projection and command surface for the mobile device card."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._project_statistics_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    @property
    def business(self) -> Any:
        return self.runtime.business

    @property
    def device_state(self) -> Any:
        return self.runtime.device_state

    def project_device_catalog(self) -> dict[str, Any]:
        return {
            "devices": [
                {
                    "id": device.name,
                    "name": device.name,
                    "device_type": device.device_type,
                    "card_profile": f"{device.device_type}.v1",
                    "card_href": f"/api/v3/devices/{device.name}/card",
                }
                for device in self.business.registry.list()
            ]
        }

    def project_card(
        self, device_name: str, include_deferred_data: bool = False
    ) -> dict[str, Any]:
        device = self.business.registry.get(device_name)
        operations = self._load_active_operations(device.name)
        status = self.device_state.project_current_device_state(device.name)
        control = self._project_control_block(device, status, operations)
        blocks = [
            self._project_overview_block(device, status, operations, include_project_statistics=True),
            control if include_deferred_data else self._as_deferred_block_descriptor(control),
        ]
        if device.device_type == "plant":
            parameters = self._project_watering_parameters_block(device, status, operations)
            blocks.extend([
                parameters if include_deferred_data else self._as_deferred_block_descriptor(parameters),
                self._project_watering_history_block(device) if include_deferred_data
                else self._project_watering_history_descriptor(device),
            ])
        else:
            blocks.append(self._project_tank_watering_block(device, status, operations))
        blocks.append(self._project_operation_queue_block(device, operations))
        revision = self._calculate_revision(status, operations)
        return {
            "device_id": device.name,
            "profile": f"{device.device_type}.v1",
            "schema_version": 1,
            "revision": revision,
            "blocks": blocks,
        }

    def project_block(self, device_name: str, block_id: str) -> dict[str, Any]:
        device = self.business.registry.get(device_name)
        operations = self._load_active_operations(device.name)
        status = self.device_state.project_current_device_state(device.name)
        builders = {
            "overview": lambda: self._project_overview_block(
                device, status, operations, include_project_statistics=True
            ),
            "control": lambda: self._project_control_block(device, status, operations),
            "watering_parameters": lambda: self._project_watering_parameters_block(
                device, status, operations
            ),
            "watering_history": lambda: self._project_watering_history_block(device),
            "watering": lambda: self._project_tank_watering_block(device, status, operations),
            "operation_queue": lambda: self._project_operation_queue_block(
                device, operations
            ),
        }
        if block_id not in builders:
            raise PublicApiError(
                f"card block '{block_id}' does not exist for device '{device_name}'",
                404,
                "card_block_not_found",
            )
        block = builders[block_id]()
        return {
            "device_id": device.name,
            "card_revision": self._calculate_revision(status, operations),
            "block": block,
        }

    def execute_action(
        self, device_name: str, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        device = self.business.registry.get(device_name)
        accepted = True

        if action == "rename-backend":
            name = self._require_string(payload, "name")
            device = self.business.registry.rename_backend(device.name, name)
        elif action == "set-device-type":
            device_type = self._require_string(payload, "device_type")
            if device_type not in DEVICE_TYPES:
                raise PublicApiError("invalid device_type", 400, "invalid_device_type")
            self._require_queued_operation(self.business.queue_device_config(
                device, {"device_type": device_type}, f"configure {device.name} type"
            ))
        elif action == "set-tare-weight":
            tare_weight_g = self._require_number(payload, "tare_weight_g", minimum=0)
            self._require_queued_operation(self.business.queue_device_config(
                device, {"tare_weight_g": tare_weight_g}, f"configure {device.name} tare"
            ))
        elif action == "set-sleep":
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                raise PublicApiError("enabled must be a boolean", 400, "invalid_enabled")
            self.business.queue_sleep(device.name, enabled)
        elif action == "set-sleep-interval":
            minutes = self._require_integer(payload, "minutes", minimum=1)
            self._require_queued_operation(self.business.queue_sleep_interval(device.name, minutes))
        elif action == "capture-zero":
            self.business.queue_zero(device.name)
        elif action == "calibrate":
            self.business.queue_calibration(
                device.name, self._require_number(payload, "weight_g", minimum=0, exclusive=True)
            )
        elif action == "set-watering-parameters":
            values = self._validate_watering_parameters(payload)
            self._require_queued_operation(self.business.queue_device_config(
                device, values, f"configure {device.name} watering parameters"
            ))
        elif action == "start-watering":
            self.business.queue_fill(
                device.name, self._require_number(payload, "target_g", minimum=0, exclusive=True)
            )
        elif action == "stop-watering":
            self.business.queue_stop(device.name)
        elif action == "set-watering-fertilized":
            event_id = self._require_integer(payload, "event_id", minimum=1)
            fertilized = payload.get("fertilized")
            if not isinstance(fertilized, bool):
                raise PublicApiError(
                    "fertilized must be a boolean", 400, "invalid_fertilized"
                )
            self.device_state.set_watering_history_fertilized(
                device.name, event_id, fertilized
            )
        elif action == "delete-watering-history-item":
            self.device_state.delete_watering_history_item(
                device.name, self._require_integer(payload, "event_id", minimum=1)
            )
        elif action == "cancel-operation":
            operation_id = self._require_string(payload, "operation_id")
            if operation_id not in {
                operation["operation_id"] for operation in self._load_active_operations(
                    device.name
                )
            }:
                raise PublicApiError(
                    "active operation does not exist for this device",
                    404,
                    "active_operation_not_found",
                )
            self.business.operations.event(
                operation_id,
                OP_CANCELLED,
                "cancelled from device card",
                event_type="operation.cancelled",
            )
        else:
            raise PublicApiError(
                f"unknown device card action '{action}'", 404, "card_action_not_found"
            )

        return {
            "accepted": accepted,
            "card": self.project_card(device.name, include_deferred_data=True),
        }

    def _load_active_operations(self, device_name: str) -> list[dict[str, Any]]:
        return self.business.operations.details_from_operations(
            self.business.operations.list_non_terminal(device_name)
        )

    def _project_operation_queue_block(
        self, device: Any, operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        items = [
            {
                "id": operation["operation_id"],
                "type": operation["type"],
                "label": str(operation["type"]).replace("_", " ").title(),
                "status": operation["status"],
                "created_at": operation["created_at"],
                "payload": self._summarize_operation_payload(
                    operation["type"], operation.get("payload") or {}
                ),
                "actions": [{
                    "kind": "action",
                    "id": "cancel",
                    "label": "Delete",
                    "control_type": "button.v1",
                    "style": "danger",
                    "enabled": True,
                    "request": self._advertised_action_request(
                        f"/api/v3/devices/{device.name}/actions/cancel-operation",
                        "literal",
                        value={"operation_id": operation["operation_id"]},
                    ),
                }],
            }
            for operation in operations
        ]
        return {
            "id": "operation_queue",
            "kind": "operation_queue",
            "slot": "operations",
            "title": "Operation queue",
            "required": True,
            "data": {"items": items},
            "refresh": {
                "mode": "poll",
                "interval_ms": 3000,
                "href": (
                    f"/api/v3/devices/{device.name}/card/blocks/operation_queue"
                ),
            },
        }

    @staticmethod
    def _summarize_operation_payload(
        operation_type: str, payload: dict[str, Any]
    ) -> str | None:
        if operation_type == "sleep_enable":
            return "On"
        if operation_type == "sleep_disable":
            return "Off"
        formats = (
            ("minutes", " min"),
            ("target_g", " g"),
            ("weight_g", " g"),
            ("tare_weight_g", " g"),
            ("watering_loss_threshold_percent", "%"),
            ("wet_weight_g", " g"),
            ("dry_weight_g", " g"),
            ("backend_name", ""),
            ("device_type", ""),
            ("name", ""),
        )
        for key, suffix in formats:
            value = payload.get(key)
            if value is not None:
                return f"{value}{suffix}"
        return None

    @staticmethod
    def _find_active_operation(
        operations: list[dict[str, Any]], *operation_types: str
    ) -> dict[str, Any] | None:
        return next(
            (op for op in operations if op.get("type") in operation_types), None
        )

    @staticmethod
    def _as_deferred_block_descriptor(block: dict[str, Any]) -> dict[str, Any]:
        return {**block, "data": {}}

    @staticmethod
    def _project_watering_history_descriptor(device: Any) -> dict[str, Any]:
        return {
            "id": "watering_history",
            "kind": "history",
            "slot": "history",
            "title": "Watering history",
            "required": False,
            "data": {},
            "refresh": {
                "mode": "once",
                "href": (
                    f"/api/v3/devices/{device.name}/card/blocks/watering_history"
                ),
            },
        }

    @staticmethod
    def _advertised_action_request(action_href: str, binding: str = "none", **body: Any) -> dict[str, Any]:
        return {
            "method": "POST",
            "href": action_href,
            "body": {"binding": binding, **body},
        }

    def _project_overview_block(
        self,
        device: Any,
        status: dict[str, Any],
        operations: list[dict[str, Any]],
        include_project_statistics: bool,
    ) -> dict[str, Any]:
        result = status.get("result") or {}
        weight = result.get("weight") or {}
        config = result.get("config") or {}
        online = bool(status.get("online"))
        data_available = bool(status.get("available"))
        primary_value: dict[str, Any]
        statistics: list[dict[str, Any]] = []
        if device.device_type == "plant":
            threshold_weight = self._remaining_weight_above_threshold(weight, config, device.name)
            primary_value = {
                "value": threshold_weight,
                "unit": "g",
                "label": "Weight above watering threshold",
                "tone": "good" if threshold_weight is not None and threshold_weight > 50 else "danger",
            }
            if include_project_statistics:
                statistics = self._project_statistics(device.name)
        else:
            gross = weight.get("gross_weight_g")
            tare = config.get("tare_weight_g")
            water = gross - tare if isinstance(gross, (int, float)) and isinstance(tare, (int, float)) else None
            primary_value = {
                "value": round(water) if water is not None else None,
                "unit": "g",
                "label": "Water weight",
                "tone": "danger" if water is not None and water < 100 else "accent",
            }
        workflow = self._project_workflow_state(result, operations)
        return {
            "id": "overview",
            "kind": "device_overview",
            "slot": "primary",
            "required": True,
            "data": {
                "title": device.name,
                "subtitle": f"MCU: {device.controller_name}",
                "status": {
                    "code": workflow["code"] if workflow["code"] != "idle" else ("online" if online else "offline"),
                    "label": workflow["label"] if workflow["code"] != "idle" else ("Online" if online else "Offline"),
                    "severity": workflow["severity"] if workflow["code"] != "idle" else ("success" if online else "error"),
                },
                "source": status.get("source") if data_available else "none",
                "primary_value": primary_value,
                "snapshot_at": status.get("result_received_at"),
                "statistics": statistics,
            },
            "refresh": {
                "mode": "poll",
                "interval_ms": 5000,
                "href": f"/api/v3/devices/{device.name}/card/blocks/overview",
            },
        }

    def _project_control_block(
        self, device: Any, status: dict[str, Any], operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        result = status.get("result") or {}
        config = result.get("config") or {}
        snapshot_at = status.get("result_received_at") or 0
        confirmed_config = self._find_successful_operation_after(
            device.name, "config", snapshot_at
        )
        confirmed_interval = self._find_successful_operation_after(
            device.name, "sleep_interval", snapshot_at
        )
        confirmed_sleep = max(
            filter(
                None,
                (
                    self._find_successful_operation_after(device.name, "sleep_enable", snapshot_at),
                    self._find_successful_operation_after(device.name, "sleep_disable", snapshot_at),
                ),
            ),
            key=lambda operation: operation["updated_at"],
            default=None,
        )
        confirmed_config_payload = (confirmed_config or {}).get("payload") or {}
        applied_device_type = confirmed_config_payload.get("device_type", device.device_type)
        applied_tare = confirmed_config_payload.get("tare_weight_g", config.get("tare_weight_g"))
        config_pending = self._find_active_operation(operations, "device_config")
        applied_device_type = self._project_pending_value(
            config_pending, "device_type", applied_device_type
        )
        applied_tare = self._project_pending_value(config_pending, "tare_weight_g", applied_tare)
        applied_interval = (confirmed_interval or {}).get("payload", {}).get(
            "minutes", config.get("sleep_interval_min")
        )
        applied_sleep_disabled = config.get("sleep_disabled")
        if confirmed_sleep is not None:
            applied_sleep_disabled = confirmed_sleep["type"] == "sleep_disable"
        controls: list[dict[str, Any]] = [
            self._field_with_commit_action(
                "backend_name", "Name", "text_input.v1", "string", device.name,
                f"/api/v3/devices/{device.name}/actions/rename-backend", "name",
            ),
            {
                **self._field_with_commit_action(
                    "device_type", "Device type", "select.v1", "string", applied_device_type,
                    f"/api/v3/devices/{device.name}/actions/set-device-type", "device_type",
                ),
                "options": [
                    {"value": str(item), "label": str(item).replace("_", " ").title()}
                    for item in sorted(DEVICE_TYPES)
                ],
            },
        ]
        if device.device_type != "plant":
            controls.append(self._field_with_commit_action(
                "tare_weight_g", "Tare weight", "number_input.v1", "integer",
                applied_tare,
                f"/api/v3/devices/{device.name}/actions/set-tare-weight", "tare_weight_g",
                unit="g", constraints={"min": 0, "step": 1},
            ))
        sleep_pending = self._find_active_operation(operations, "sleep_enable", "sleep_disable")
        interval_pending = self._find_active_operation(operations, "sleep_interval")
        if sleep_pending is not None:
            applied_sleep_disabled = sleep_pending["type"] == "sleep_disable"
        controls.extend([
            {
                "kind": "action",
                "id": "sleep_enabled",
                "label": "Sleep mode",
                "control_type": "action_toggle.v1",
                "value_type": "boolean",
                "enabled": sleep_pending is None and config.get("sleep_disabled") is not None,
                "request": self._advertised_action_request(
                    f"/api/v3/devices/{device.name}/actions/set-sleep",
                    "control_value", property="enabled",
                ),
            },
            self._field_with_commit_action(
                "sleep_interval_minutes", "Sleep interval", "number_input.v1", "integer",
                self._project_pending_value(interval_pending, "minutes", applied_interval),
                f"/api/v3/devices/{device.name}/actions/set-sleep-interval", "minutes",
                unit="min", constraints={"min": 1, "max": 50, "step": 1},
                enabled=interval_pending is None,
            ),
            {
                "kind": "action",
                "id": "capture_zero",
                "label": "Set zero",
                "control_type": "hold_action.v1",
                "preset": "zero_capture_hold.v1",
                "enabled": self._find_active_operation(operations, "zero_capture") is None,
                "request": self._advertised_action_request(
                    f"/api/v3/devices/{device.name}/actions/capture-zero"
                ),
            },
            {
                "kind": "field",
                "id": "calibration_weight_g",
                "label": "Calibration weight",
                "control_type": "number_input.v1",
                "value_type": "decimal",
                "default": None,
                "unit": "g",
                "constraints": {"min_exclusive": 0},
            },
            {
                "kind": "action",
                "id": "calibrate",
                "label": "Calibrate",
                "control_type": "hold_action.v1",
                "preset": "calibration_hold.v1",
                "enabled": self._find_active_operation(operations, "scale_calibration") is None,
                "request": self._advertised_action_request(
                    f"/api/v3/devices/{device.name}/actions/calibrate",
                    "fields", fields=["calibration_weight_g"],
                    properties={"calibration_weight_g": "weight_g"},
                ),
            },
        ])
        values = {
            "backend_name": device.name,
            "device_type": applied_device_type,
            "sleep_enabled": None if applied_sleep_disabled is None else not applied_sleep_disabled,
            "sleep_interval_minutes": self._project_pending_value(interval_pending, "minutes", applied_interval),
            "tare_weight_g": applied_tare,
            "calibration_weight_g": None,
        }
        return {
            "id": "control",
            "kind": "dynamic_form",
            "slot": "control",
            "title": "Control",
            "required": True,
            "schema": {"controls": controls},
            "data": {"values": values},
            "refresh": {
                "mode": "poll" if operations else "on_open",
                "interval_ms": 3000 if operations else None,
                "href": f"/api/v3/devices/{device.name}/card/blocks/control",
            },
        }

    def _project_watering_parameters_block(
        self, device: Any, status: dict[str, Any], operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        settings = self.business.registry.watering_settings(device.name)
        result = status.get("result") or {}
        config = result.get("config") or {}
        weight = result.get("weight") or {}
        values = {
            "gross_weight_g": weight.get("gross_weight_g"),
            "dry_weight_g": self._prefer_stored_setting(
                settings, config, "dry_weight_g"
            ),
            "wet_weight_g": self._prefer_stored_setting(
                settings, config, "wet_weight_g"
            ),
            "watering_loss_threshold_percent": self._prefer_stored_setting(
                settings, config, "watering_loss_threshold_percent"
            ),
        }
        pending = self._find_active_operation(operations, "device_config")
        for key in (
            "dry_weight_g", "wet_weight_g", "watering_loss_threshold_percent"
        ):
            values[key] = self._project_pending_value(pending, key, values[key])
        controls = [
            {"kind": "field", "id": "gross_weight_g", "label": "Raw weight (gross)", "control_type": "readonly.v1", "value_type": "decimal", "unit": "g"},
            {"kind": "field", "id": "dry_weight_g", "label": "Dry weight", "control_type": "number_input.v1", "value_type": "integer", "unit": "g", "constraints": {"min": 0}},
            {"kind": "field", "id": "wet_weight_g", "label": "Wet weight", "control_type": "number_input.v1", "value_type": "integer", "unit": "g", "constraints": {"min": 0}},
            {"kind": "field", "id": "watering_loss_threshold_percent", "label": "Water loss threshold", "control_type": "number_input.v1", "value_type": "integer", "unit": "%", "constraints": {"min": 0, "max": 100}},
            {
                "kind": "action", "id": "save_watering_parameters", "label": "Save",
                "control_type": "button.v1", "enabled": pending is None,
                "request": self._advertised_action_request(
                    f"/api/v3/devices/{device.name}/actions/set-watering-parameters",
                    "fields",
                    fields=["dry_weight_g", "wet_weight_g", "watering_loss_threshold_percent"],
                ),
            },
        ]
        return {
            "id": "watering_parameters", "kind": "dynamic_form",
            "slot": "watering_parameters", "title": "Watering parameters",
            "required": True, "schema": {"controls": controls},
            "data": {"values": values},
            "refresh": {
                "mode": "poll" if pending else "on_open",
                "interval_ms": 3000 if pending else None,
                "href": f"/api/v3/devices/{device.name}/card/blocks/watering_parameters",
            },
        }

    def _project_watering_history_block(self, device: Any) -> dict[str, Any]:
        history = self.device_state.project_watering_history(device.name, 50, 0)
        items = []
        for item in history["waterings"]:
            event_id = item["id"]
            items.append({
                **item,
                "actions": [
                    {
                        "id": "fertilized", "label": "Fertilized",
                        "control_type": "action_toggle.v1", "value": item["fertilized"],
                        "request": self._advertised_action_request(
                            f"/api/v3/devices/{device.name}/actions/set-watering-fertilized",
                            "literal_and_control_value",
                            literal={"event_id": event_id}, property="fertilized",
                        ),
                    },
                    {
                        "id": "delete", "label": "Delete",
                        "control_type": "hold_action.v1", "preset": "history_delete_hold.v1",
                        "request": self._advertised_action_request(
                            f"/api/v3/devices/{device.name}/actions/delete-watering-history-item",
                            "literal", value={"event_id": event_id},
                        ),
                    },
                ],
            })
        return {
            "id": "watering_history", "kind": "history", "slot": "history",
            "title": "Watering history", "required": False,
            "data": {"items": items, "next_offset": history["next_offset"]},
            "refresh": {
                "mode": "once",
                "href": f"/api/v3/devices/{device.name}/card/blocks/watering_history",
            },
        }

    def _project_tank_watering_block(
        self, device: Any, status: dict[str, Any], operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        result = status.get("result") or {}
        watering = result.get("watering") or {}
        active = self._find_active_operation(operations, "fill", "watering_start")
        controller_active = bool(watering.get("active"))
        if active or controller_active:
            target = (active or {}).get("target_g")
            controls = [{
                "kind": "action", "id": "stop_watering", "label": "Stop",
                "control_type": "button.v1", "style": "danger", "enabled": True,
                "request": self._advertised_action_request(
                    f"/api/v3/devices/{device.name}/actions/stop-watering"
                ),
            }]
            data = {
                "state": "watering",
                "label": "Watering in progress",
                "target": target,
                "unit": "g",
                "controller_state": watering.get("state"),
            }
            kind = "progress"
        else:
            controls = [
                {"kind": "field", "id": "target_g", "label": "Water amount", "control_type": "number_input.v1", "value_type": "decimal", "default": 200, "unit": "g", "constraints": {"min_exclusive": 0}},
                {"kind": "action", "id": "start_watering", "label": "Start", "control_type": "button.v1", "enabled": True, "request": self._advertised_action_request(
                    f"/api/v3/devices/{device.name}/actions/start-watering",
                    "fields", fields=["target_g"],
                )},
            ]
            data = {"values": {"target_g": 200}}
            kind = "dynamic_form"
        return {
            "id": "watering", "kind": kind, "slot": "watering", "title": "Watering",
            "required": True, "schema": {"controls": controls}, "data": data,
            "refresh": {
                "mode": "poll", "interval_ms": 3000 if active or controller_active else 10000,
                "href": f"/api/v3/devices/{device.name}/card/blocks/watering",
            },
        }

    @staticmethod
    def _field_with_commit_action(
        control_id: str, label: str, control_type: str, value_type: str,
        default: Any, href: str, property_name: str, **extra: Any,
    ) -> dict[str, Any]:
        enabled = extra.pop("enabled", True)
        return {
            "kind": "field", "id": control_id, "label": label,
            "control_type": control_type, "value_type": value_type,
            "default": default, "enabled": enabled, **extra,
            "commit": {
                "mode": "button", "label": "Apply",
                "request": DeviceCardService._advertised_action_request(
                    href, "control_value", property=property_name
                ),
            },
        }

    def _remaining_weight_above_threshold(
        self, weight: dict[str, Any], config: dict[str, Any], device_name: str
    ) -> int | None:
        settings = self.business.registry.watering_settings(device_name)
        gross = weight.get("gross_weight_g")
        dry = self._prefer_stored_setting(settings, config, "dry_weight_g")
        wet = self._prefer_stored_setting(settings, config, "wet_weight_g")
        loss = self._prefer_stored_setting(
            settings, config, "watering_loss_threshold_percent"
        )
        if not all(isinstance(value, (int, float)) for value in (gross, dry, wet, loss)):
            return None
        gross_value, dry_value, wet_value, loss_value = map(
            float, (gross, dry, wet, loss)
        )
        if wet_value <= dry_value or not 0 <= loss_value <= 100:
            return None
        return round(
            gross_value - dry_value
            - (wet_value - dry_value) * loss_value / 100
        )

    @staticmethod
    def _prefer_stored_setting(
        settings: dict[str, Any], config: dict[str, Any], key: str
    ) -> Any:
        value = settings.get(key)
        return value if isinstance(value, (int, float)) else config.get(key)

    def _project_statistics(self, device_name: str) -> list[dict[str, Any]]:
        now = time.monotonic()
        cached = self._project_statistics_cache.get(device_name)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            value = [{
                "kind": "water_consumption",
                "days": self.device_state.project_water_consumption(device_name)["days"],
            }]
        except Exception:
            value = cached[1] if cached is not None else []
        self._project_statistics_cache[device_name] = (now + 300, value)
        return value

    def _find_successful_operation_after(
        self, device_name: str, operation_type: str, timestamp: float
    ) -> dict[str, Any] | None:
        operation = self.business.operations.latest_for_device(
            device_name, operation_type
        )
        if (
            operation is None
            or operation.get("status") != "success"
            or operation.get("updated_at", 0) <= timestamp
        ):
            return None
        return operation

    @staticmethod
    def _project_workflow_state(
        result: dict[str, Any], operations: list[dict[str, Any]]
    ) -> dict[str, str]:
        watering = result.get("watering") or {}
        active = next(
            (op for op in operations if op.get("type") in {"fill", "watering_start"}),
            None,
        )
        if active:
            status = active.get("status")
            if status in {"queued", "sending"}:
                return {"code": "watering_queued", "label": "Watering queued", "severity": "info"}
            return {"code": "watering", "label": "Watering", "severity": "info"}
        if watering.get("active"):
            return {"code": "watering", "label": "Watering", "severity": "info"}
        if operations:
            return {"code": "updating", "label": "Applying changes", "severity": "info"}
        return {"code": "idle", "label": "Idle", "severity": "success"}

    @staticmethod
    def _project_pending_value(operation: dict[str, Any] | None, key: str, fallback: Any) -> Any:
        if operation and isinstance(operation.get("payload"), dict):
            return operation["payload"].get(key, fallback)
        return fallback

    @staticmethod
    def _calculate_revision(status: dict[str, Any], operations: list[dict[str, Any]]) -> int:
        timestamps = [status.get("result_received_at") or 0]
        timestamps.extend(
            value for operation in operations
            if isinstance((value := operation.get("updated_at")), (int, float))
        )
        return int(max(timestamps) * 1000)

    @staticmethod
    def _require_queued_operation(operation_id: str | None) -> str:
        if operation_id is None:
            raise PublicApiError("command was not queued", 409, "command_not_queued")
        return operation_id

    @staticmethod
    def _require_string(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise PublicApiError(f"{key} must be a non-empty string", 400, f"invalid_{key}")
        return value.strip()

    @staticmethod
    def _require_integer(payload: dict[str, Any], key: str, minimum: int) -> int:
        value = payload.get(key)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not float(value).is_integer()
            or value < minimum
        ):
            raise PublicApiError(f"{key} must be an integer >= {minimum}", 400, f"invalid_{key}")
        return int(value)

    @staticmethod
    def _require_number(
        payload: dict[str, Any], key: str, minimum: float, exclusive: bool = False
    ) -> float:
        value = payload.get(key)
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        valid = valid and (value > minimum if exclusive else value >= minimum)
        if not valid:
            operator = ">" if exclusive else ">="
            raise PublicApiError(f"{key} must be a number {operator} {minimum}", 400, f"invalid_{key}")
        assert isinstance(value, (int, float)) and not isinstance(value, bool)
        return float(value)

    @staticmethod
    def _validate_watering_parameters(payload: dict[str, Any]) -> dict[str, int]:
        allowed = {
            "dry_weight_g": (0, None),
            "wet_weight_g": (0, None),
            "watering_loss_threshold_percent": (0, 100),
        }
        values: dict[str, int] = {}
        for key, (minimum, maximum) in allowed.items():
            if key not in payload:
                continue
            value = payload[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise PublicApiError(f"invalid {key}", 400, f"invalid_{key}")
            if maximum is not None and value > maximum:
                raise PublicApiError(f"invalid {key}", 400, f"invalid_{key}")
            values[key] = value
        if not values:
            raise PublicApiError("watering parameters are required", 400, "invalid_watering_parameters")
        return values


