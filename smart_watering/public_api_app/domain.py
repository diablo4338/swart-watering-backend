from enum import Enum
from typing import Any


class DeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"


class DeviceStatusSource(str, Enum):
    LIVE = "live"
    SNAPSHOT = "snapshot"
    NONE = "none"


def number_or_none(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
