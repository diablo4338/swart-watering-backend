import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from smart_watering.infrastructure.database import (
    DatabaseError,
    DatabaseStore,
)


QUEUE_DIR = os.path.join(tempfile.gettempdir(), "smart_watering_cli")
DEFAULT_DB_PATH = os.path.join(QUEUE_DIR, "smart_watering.db")
DB_PATH_ENV = "SMART_WATERING_DB_PATH"
WORKER_PID_PATH = os.path.join(QUEUE_DIR, "worker.pid")
NODE_URL_ENV = "SMART_WATERING_NODE_URL"
REQUEST_TIMEOUT_SEC = 5
WORKER_RETRY_INTERVAL_SEC = 5
WORKER_MAX_WAIT_SEC = 900
WORKER_RETRY_INTERVAL_SEC_ENV = "SMART_WATERING_WORKER_RETRY_INTERVAL_SEC"
WORKER_MAX_WAIT_SEC_ENV = "SMART_WATERING_WORKER_MAX_WAIT_SEC"
DEFAULT_NODE_PORT = 8080
NODE_WORKER_IDLE_INTERVAL_SEC_ENV = "SMART_WATERING_WORKER_IDLE_INTERVAL_SEC"
DEFAULT_NODE_WORKER_IDLE_INTERVAL_SEC = 1
NODE_WORKER_IDLE_INTERVAL_SEC = DEFAULT_NODE_WORKER_IDLE_INTERVAL_SEC
WORKER_STALE_CHECK_INTERVAL_SEC = 60
RETRYABLE_COMMANDS = frozenset({
    ("POST", "/config"),
    ("POST", "/watering/start"),
    ("POST", "/sleep/disable"),
    ("POST", "/sleep/interval"),
})
DISCOVERY_DEVICE_PREFIX = "discovery:"


def discovered_device_config(status: dict[str, Any]) -> tuple[str, str, dict[str, int]]:
    device = status.get("device")
    if not isinstance(device, dict):
        raise SmartWateringError("invalid /watering response: missing device object")
    name = device.get("name")
    device_type = device.get("type")
    if not isinstance(name, str) or not name:
        raise SmartWateringError("invalid /watering response: missing device.name")
    if device_type not in DEVICE_TYPES:
        raise SmartWateringError("invalid /watering response: unsupported device.type")

    settings: dict[str, int] = {}
    config = status.get("config")
    if isinstance(config, dict):
        for key in ("dry_weight_g", "wet_weight_g", "watering_loss_threshold_percent"):
            value = config.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                continue
            if key == "watering_loss_threshold_percent" and value > 100:
                continue
            settings[key] = int(value)
    return name, device_type, settings

class DeviceType(StrEnum):
    PLANT = "plant"
    TANK = "tank"


DEVICE_TYPES = frozenset(device_type.value for device_type in DeviceType)
CONFIG_FLOAT_KEYS = {
    "tare_weight_g", "dry_weight_g", "wet_weight_g",
    "watering_loss_threshold_percent",
}
CONFIG_STRING_KEYS = {"device_type", "name"}
CONFIG_KEYS = CONFIG_FLOAT_KEYS | CONFIG_STRING_KEYS
OP_QUEUED = "queued"
OP_SENDING = "sending"
OP_ACCEPTED = "accepted"
OP_RUNNING = "running"
OP_SUCCESS = "success"
OP_ERROR = "error"
OP_TIMEOUT = "timeout"
OP_CANCELLED = "cancelled"
OP_TERMINAL_STATUSES = {OP_SUCCESS, OP_ERROR, OP_TIMEOUT, OP_CANCELLED}
OP_STATUS_RANK = {
    OP_QUEUED: 0,
    OP_SENDING: 1,
    OP_ACCEPTED: 2,
    OP_RUNNING: 3,
    OP_SUCCESS: 4,
    OP_ERROR: 4,
    OP_TIMEOUT: 4,
    OP_CANCELLED: 4,
}
PASSWORD_HASH_ITERATIONS = 210_000


class SmartWateringError(RuntimeError):
    pass


class DeviceNameConflictError(SmartWateringError):
    pass


def parse_positive_int(raw_value: str, name: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise SmartWateringError(f"{name} must be an integer") from exc
    if value <= 0:
        raise SmartWateringError(f"{name} must be > 0")
    return value


def resolve_node_worker_idle_interval_sec() -> int:
    raw_value = os.environ.get(NODE_WORKER_IDLE_INTERVAL_SEC_ENV)
    if raw_value is None:
        return DEFAULT_NODE_WORKER_IDLE_INTERVAL_SEC
    return parse_positive_int(raw_value, NODE_WORKER_IDLE_INTERVAL_SEC_ENV)


def resolve_worker_retry_interval_sec() -> int:
    raw_value = os.environ.get(WORKER_RETRY_INTERVAL_SEC_ENV)
    if raw_value is None:
        return WORKER_RETRY_INTERVAL_SEC
    return parse_positive_int(raw_value, WORKER_RETRY_INTERVAL_SEC_ENV)


def resolve_worker_max_wait_sec() -> int:
    raw_value = os.environ.get(WORKER_MAX_WAIT_SEC_ENV)
    if raw_value is None:
        return WORKER_MAX_WAIT_SEC
    return parse_positive_int(raw_value, WORKER_MAX_WAIT_SEC_ENV)


class RetryableDeviceApiError(SmartWateringError):
    pass


class DeviceHttpError(RetryableDeviceApiError):
    def __init__(self, status_code: int, path: str, body: str) -> None:
        message = f"HTTP {status_code} for {path}"
        if body:
            message = f"{message}: {body}"
        super().__init__(message)
        self.status_code = status_code
        self.path = path
        self.body = body


def resolve_db_path() -> str:
    return os.environ.get(DB_PATH_ENV, DEFAULT_DB_PATH)


@dataclass
class Device:
    id: str
    name: str
    ip: str
    base_url: str
    device_type: str
    created_at: float
    updated_at: float


@dataclass
class QueuedCommand:
    id: int
    operation_id: str
    device_id: str | None
    device_name: str
    base_url: str
    path: str
    method: str
    payload: dict[str, Any] | None
    description: str
    created_at: float
    started_at: float | None

    def age_seconds(self) -> int:
        return max(0, int(time.time() - self.created_at))


@dataclass
class User:
    username: str
    created_at: float
    updated_at: float


@dataclass
class UserSession:
    session_id: str
    username: str
    created_at: float
    expires_at: float
    revoked_at: float | None


class SQLiteStore(DatabaseStore):
    def __init__(self, db_path: str | None = None, reuse_connections: bool = False) -> None:
        super().__init__(db_path or resolve_db_path(), reuse_connections=reuse_connections)

    @contextmanager
    def session(self) -> Iterator[Session]:
        try:
            with super().session() as session:
                yield session
        except DatabaseError as exc:
            raise SmartWateringError(str(exc)) from exc

    def init_schema(self) -> None:
        try:
            self.migrate()
        except DatabaseError as exc:
            raise SmartWateringError(str(exc)) from exc
