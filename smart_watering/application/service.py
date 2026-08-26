from smart_watering.domain import (
    CONFIG_FLOAT_KEYS,
    DISCOVERY_DEVICE_PREFIX,
    OP_CANCELLED,
    OP_SUCCESS,
    REQUEST_TIMEOUT_SEC,
    AuthStore,
    CommandQueue,
    DeviceApiClient,
    DeviceRegistry,
    DeviceSnapshotStore,
    OperationLog,
    PlantWateringEventStore,
    SQLiteStore,
    Device,
    DeviceType,
    SmartWateringError,
    build_callback_url,
)

MAX_SLEEP_INTERVAL_MIN = 50


class SmartWateringService:
    """Shared application layer used by every delivery adapter."""

    def __init__(
        self,
        db_path: str | None = None,
        reuse_connections: bool = False,
    ) -> None:
        self.store = SQLiteStore(db_path, reuse_connections=reuse_connections)
        self.store.init_schema()
        self.registry = DeviceRegistry(self.store)
        self.auth = AuthStore(self.store)
        self.operations = OperationLog(self.store)
        self.snapshots = DeviceSnapshotStore(self.store)
        self.plant_waterings = PlantWateringEventStore(self.store)
        self.queue = CommandQueue(self.store)
        self.api = DeviceApiClient(REQUEST_TIMEOUT_SEC)

    def callback_url(self) -> str:
        return build_callback_url()

    def queue_device_discovery(self, ip_or_url: str) -> str:
        _ip, base_url = self.registry.normalize_base_url(ip_or_url)
        pending_id = self.queue.find_duplicate(base_url, "/watering", "GET", None)
        if pending_id is not None:
            return pending_id
        placeholder = f"{DISCOVERY_DEVICE_PREFIX}{base_url}"
        operation_id = self.operations.create(
            None, "device_discovery", {"base_url": base_url}, target_label=placeholder
        )
        return self.queue.enqueue(
            operation_id,
            None,
            base_url,
            "/watering",
            "GET",
            None,
            f"discover device at {base_url}",
            target_label=placeholder,
        )

    def cancel_device_discovery(self, ip_or_url: str) -> list[str]:
        _ip, base_url = self.registry.normalize_base_url(ip_or_url)
        operation_ids: list[str] = []
        for command in self.queue.list():
            if (
                command.base_url == base_url
                and command.device_name.startswith(DISCOVERY_DEVICE_PREFIX)
                and command.method == "GET"
                and command.path == "/watering"
            ):
                self.operations.event(
                    command.operation_id,
                    OP_CANCELLED,
                    "superseded by explicit device configuration",
                )
                self.queue.pop(command.id)
                operation_ids.append(command.operation_id)
        return operation_ids

    def build_operation_payload(self, operation_id: str, payload: dict | None = None) -> dict:
        return {**(payload or {}), "operation_id": operation_id, "callback_url": self.callback_url()}

    def _enqueue(
        self, device: Device, operation_type: str, path: str,
        payload: dict | None, description: str, method: str = "POST",
        correlation_id: str | None = None, causation_id: str | None = None,
    ) -> str:
        duplicate = self.queue.find_duplicate(device.base_url, path, method, payload)
        if duplicate is not None:
            return duplicate
        operation_id = self.operations.create(
            device.id, operation_type, payload or {}, correlation_id, causation_id,
        )
        command_payload = self.build_operation_payload(operation_id, payload)
        self.operations.update_payload(operation_id, command_payload)
        queued_id = self.queue.enqueue(
            operation_id, device.id, device.base_url, path, method,
            None if method == "GET" else command_payload, description,
        )
        if queued_id != operation_id:
            self.operations.event(operation_id, OP_CANCELLED, f"duplicate command already queued: {queued_id}")
        return queued_id

    def clear_device_queue(self, device_id: str) -> int:
        self.registry.get_by_id(device_id)
        self.queue.drop_device(device_id)
        operation_ids = self.operations.cancel_non_terminal(
            device_id,
            "cancelled by device queue clear",
        )
        return len(operation_ids)

    def queue_device_config(
        self, device: Device, config: dict, description: str,
        confirm_retry_duplicate: bool = False,
    ) -> str | None:
        self.registry.validate_config_update(device.id, config)
        if "backend_name" in config:
            device = self.registry.rename_backend(device.id, str(config["backend_name"]))
        payload: dict = {}
        if "device_type" in config:
            payload["device_type"] = config["device_type"]
        payload.update({key: config[key] for key in CONFIG_FLOAT_KEYS if key in config})
        if not payload:
            return None
        duplicate = self.queue.find_duplicate(device.base_url, "/config", "POST", payload)
        if duplicate is not None:
            return duplicate
        conflict = self._pending_command(device.base_url, "POST", "/config")
        if (
            conflict is not None
            and confirm_retry_duplicate
            and not self.confirm_retryable_command_conflict(conflict, payload)
        ):
            print(
                "cancelled: new command was not queued; "
                f"existing operation kept: {conflict.operation_id}"
            )
            return None
        return self._enqueue(device, "config", "/config", payload, description)

    def queue_controller_name(self, device_id: str, controller_name: str) -> str:
        device = self.registry.get_by_id(device_id)
        payload = {"device_type": device.device_type, "name": controller_name}
        return self._enqueue(
            device, "controller_name", "/config", payload,
            f"change controller id for {device.name}",
        )

    def queue_device_config_if_not_confirmed(self, device: Device) -> None:
        latest = self.operations.latest_for_device(device.id, "config")
        if latest is not None and latest["status"] != OP_SUCCESS:
            self.queue_device_config(
                device, {"device_type": device.device_type},
                f"confirm {device.name} config",
            )

    def queue_fill(self, device_id: str, grams: float) -> str:
        if grams <= 0:
            raise SmartWateringError("fill grams must be > 0")
        device = self.registry.get_by_id(device_id)
        if device.device_type != DeviceType.TANK:
            raise SmartWateringError(
                f"watering can only be started on tank devices, got {device.name} ({device.device_type})"
            )
        existing = self.operations.latest_non_terminal_watering_start(device.id)
        if existing is not None:
            return existing["operation_id"]
        self.queue_device_config_if_not_confirmed(device)
        return self._enqueue(
            device, "watering_start", "/watering/start", {"target_g": grams},
            f"fill {device.name} {grams:.1f} g",
        )

    def queue_stop(self, device_id: str) -> str:
        device = self.registry.get_by_id(device_id)
        if device.device_type != DeviceType.TANK:
            raise SmartWateringError(
                f"watering can only be stopped on tank devices, got {device.name} ({device.device_type})"
            )
        active = self.operations.latest_non_terminal_watering_start(device.id)
        cause_id = active["operation_id"] if active else None
        correlation_id = active.get("correlation_id") if active else None
        operation_id = self._enqueue(
            device, "watering_stop", "/watering/stop", {}, f"stop {device.name}",
            correlation_id=correlation_id, causation_id=cause_id,
        )
        cancelled = self.operations.cancel_active_watering_starts(
            device.id, "cancelled by watering stop"
        )
        for cancelled_id in cancelled:
            self.operations.trace_event(
                cancelled_id, "backend", "operation.related", "cancelled by watering stop",
                {"related_operation_id": operation_id, "relation": "cancelled_by"},
            )
        if cancelled:
            self.operations.trace_event(
                operation_id, "backend", "operation.related", "watering starts cancelled",
                {"related_operation_ids": cancelled, "relation": "cancels"},
            )
        self.queue.drop_pending_watering_start(device.id)
        return operation_id

    def queue_sleep(self, device_id: str, enabled: bool) -> str:
        device = self.registry.get_by_id(device_id)
        action = "enable" if enabled else "disable"
        return self._enqueue(
            device, f"sleep_{action}", f"/sleep/{action}", {},
            f"{action} sleep {device.name}",
        )

    def queue_sleep_interval(
        self, device_id: str, minutes: int, confirm_retry_duplicate: bool = False,
    ) -> str | None:
        if minutes <= 0:
            raise SmartWateringError("sleep interval minutes must be > 0")
        if minutes > MAX_SLEEP_INTERVAL_MIN:
            raise SmartWateringError(f"sleep interval minutes must be <= {MAX_SLEEP_INTERVAL_MIN}")
        device = self.registry.get_by_id(device_id)
        payload = {"minutes": minutes}
        duplicate = self.queue.find_duplicate(
            device.base_url, "/sleep/interval", "POST", payload
        )
        if duplicate is not None:
            return duplicate
        conflict = self._pending_command(device.base_url, "POST", "/sleep/interval")
        if (
            conflict is not None
            and confirm_retry_duplicate
            and not self.confirm_retryable_command_conflict(conflict, payload)
        ):
            print(
                "cancelled: new command was not queued; "
                f"existing operation kept: {conflict.operation_id}"
            )
            return None
        return self._enqueue(
            device, "sleep_interval", "/sleep/interval", payload,
            f"set sleep interval {device.name} {minutes} min",
        )

    def _pending_command(self, base_url: str, method: str, path: str):
        for command in self.queue.list():
            if command.base_url == base_url and command.method == method and command.path == path:
                return command
        return None

    def confirm_retryable_command_conflict(self, _command, _payload: dict) -> bool:
        return False

    def queue_zero(self, device_id: str) -> str:
        device = self.registry.get_by_id(device_id)
        return self._enqueue(device, "zero_capture", "/zero", {}, f"set zero {device.name}")

    def queue_calibration(self, device_id: str, weight_g: float) -> str:
        if weight_g <= 0:
            raise SmartWateringError("calibration weight must be > 0")
        device = self.registry.get_by_id(device_id)
        return self._enqueue(
            device, "scale_calibration", "/calibration", {"weight_g": weight_g},
            f"calibrate {device.name} {weight_g:.1f} g",
        )

    def request_device_status_snapshot(self, device_id: str) -> float:
        """Fetch and persist a status snapshot immediately, without the command queue."""
        device = self.registry.get_by_id(device_id)
        result = self.api.request_json(device.base_url, "/watering", "GET")
        received_at = self.snapshots.save(device.id, result)
        config = result.get("config")
        if isinstance(config, dict):
            self.registry.confirm_watering_settings(device.id, config, received_at)
        return received_at

    def latest_mcu_name(self, device_id: str) -> str | None:
        snapshot = self.snapshots.latest(device_id)
        result = snapshot.get("result") if snapshot is not None else None
        reported_device = result.get("device") if isinstance(result, dict) else None
        name = reported_device.get("name") if isinstance(reported_device, dict) else None
        return name if isinstance(name, str) and name else None
