from enum import Enum
from typing import Any


class DeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class DeviceStatusSource(str, Enum):
    LIVE = "live"
    SNAPSHOT = "snapshot"
    NONE = "none"


def number_or_none(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def build_watering_status(payload: dict[str, Any]) -> dict[str, Any]:
    device = payload.get("device", {})
    watering = payload.get("watering", {})
    config = payload.get("config", {})
    weight = payload.get("weight", {})
    target_g = number_or_none(config.get("target_g"))
    water_used_g = number_or_none(weight.get("water_used_g"))
    gap_g = None
    percent_complete = None
    if target_g is not None and water_used_g is not None:
        gap_g = max(0.0, target_g - water_used_g)
        if target_g > 0:
            percent_complete = min(100.0, max(0.0, water_used_g / target_g * 100.0))
    return {
        "device": {"name": device.get("name"), "type": device.get("type")},
        "active": bool(watering.get("active", False)),
        "state": watering.get("state"),
        "gap_g": round(gap_g, 2) if gap_g is not None else None,
        "percent_complete": round(percent_complete, 2) if percent_complete is not None else None,
        "last_operation": {
            "type": watering.get("last_operation_type"),
            "status": watering.get("last_operation_status"),
        },
    }
