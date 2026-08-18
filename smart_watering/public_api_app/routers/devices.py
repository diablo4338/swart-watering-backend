from typing import Any

from fastapi import APIRouter, Body, Query, status

from smart_watering.domain import DEVICE_TYPES

from ..dependencies import RuntimeDep, SessionDep
from ..domain import build_watering_status
from ..errors import PublicApiError
from ..service import CONTROL_OPERATION_TYPES


router = APIRouter(prefix="/api/v2", tags=["devices"])


@router.get("/devices")
def devices(api: RuntimeDep, _session: SessionDep) -> dict[str, Any]:
    pending = api.business.operations.devices_with_non_terminal(CONTROL_OPERATION_TYPES)
    return {
        "devices": [
            api.service.device_to_json(device, pending)
            for device in api.business.registry.list()
        ]
    }


@router.get("/device-types")
def device_types(_session: SessionDep) -> dict[str, list[str]]:
    return {"types": sorted(DEVICE_TYPES)}


@router.get("/device-name-availability")
def device_name_availability(
    name: str,
    api: RuntimeDep,
    _session: SessionDep,
    current_name: str | None = None,
) -> dict[str, Any]:
    candidate = name.strip()
    return {
        "name": candidate,
        "available": api.business.registry.is_name_available(candidate, current_name),
    }


@router.get("/devices/{device_name}/watering-parameters")
def watering_parameters(device_name: str, api: RuntimeDep, _session: SessionDep) -> dict[str, Any]:
    return {"device": device_name, **api.business.registry.watering_settings(device_name)}


@router.put("/devices/{device_name}/watering-parameters")
def update_watering_parameters(
    device_name: str,
    payload: dict[str, Any],
    api: RuntimeDep,
    _session: SessionDep,
) -> dict[str, Any]:
    allowed = {"dry_weight_g", "wet_weight_g", "watering_loss_threshold_percent"}
    if not any(key in payload for key in allowed):
        raise PublicApiError("at least one watering parameter is required", 400, "invalid_watering_parameters")
    values: dict[str, int] = {}
    for key in allowed:
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise PublicApiError(f"{key} must be a non-negative integer", 400, "invalid_watering_parameters")
        values[key] = value
    threshold = values.get("watering_loss_threshold_percent")
    if threshold is not None and threshold > 100:
        raise PublicApiError("watering_loss_threshold_percent must be <= 100", 400, "invalid_watering_parameters")

    device = api.business.registry.get(device_name)
    operation_id = api.business.queue_device_config(
        device, values, f"configure {device_name} watering parameters"
    )
    return {
        "device": device_name,
        "operation_id": operation_id,
        **api.business.registry.watering_settings(device_name),
    }


@router.get("/devices/{device_name}/water-consumption")
def water_consumption(device_name: str, api: RuntimeDep, _session: SessionDep) -> dict[str, Any]:
    return api.service.water_consumption_response(device_name)


@router.get("/devices/{device_name}/detected-waterings")
def detected_waterings(
    device_name: str,
    api: RuntimeDep,
    _session: SessionDep,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    return api.service.detected_waterings_response(device_name, limit, offset)


@router.delete("/devices/{device_name}/detected-waterings/{event_id}")
def invalidate_detected_watering(
    device_name: str,
    event_id: int,
    api: RuntimeDep,
    _session: SessionDep,
) -> dict[str, Any]:
    return api.service.invalidate_detected_watering(device_name, event_id)


@router.put("/devices/{device_name}/detected-waterings/{event_id}/fertilized")
def set_detected_watering_fertilized(
    device_name: str,
    event_id: int,
    api: RuntimeDep,
    _session: SessionDep,
    fertilized: bool = Body(embed=True),
) -> dict[str, Any]:
    return api.service.set_detected_watering_fertilized(
        device_name, event_id, fertilized
    )


@router.get("/devices/{device_name}/status/latest")
def latest_status(device_name: str, api: RuntimeDep, _session: SessionDep) -> dict[str, Any]:
    return api.service.latest_device_status_response(device_name)


@router.get("/devices/{device_name}/status/live")
def live_status(device_name: str, api: RuntimeDep, _session: SessionDep) -> dict[str, Any]:
    return api.service.live_device_status_response(device_name)


@router.post("/devices/{device_name}/status", status_code=status.HTTP_202_ACCEPTED)
def queue_status(
    device_name: str,
    _payload: dict[str, Any],
    api: RuntimeDep,
    _session: SessionDep,
) -> dict[str, Any]:
    operation_id = api.business.queue_device_status(device_name)
    return api.service.operation_response(operation_id)


@router.get("/devices/{device_name}/watering/status")
def watering_status(device_name: str, api: RuntimeDep, _session: SessionDep) -> dict[str, Any]:
    latest = api.service.live_device_status_response(device_name)
    result = latest["result"]
    if result is None:
        error = latest["error"] or {}
        raise PublicApiError(
            str(
                error.get("message")
                or f"live status is unavailable for device '{device_name}'"
            ),
            503,
            str(error.get("code") or "device_status_unavailable"),
        )
    response = build_watering_status(result)
    for key in (
        "source",
        "available",
        "result_received_at",
        "operation_id",
        "pending_operation_id",
        "pending_operation_status",
    ):
        response[key] = latest[key]
    response["planned_watering"] = api.service.planned_watering_response(device_name)
    response["result"] = result
    return response


@router.get("/devices/{device_name}/watering/last")
def last_watering(device_name: str, api: RuntimeDep, _session: SessionDep) -> dict[str, Any]:
    api.business.registry.get(device_name)
    return api.service.last_watering_response(device_name)


@router.post("/devices/{device_name}/watering/start", status_code=status.HTTP_202_ACCEPTED)
def start_watering(
    device_name: str,
    payload: dict[str, Any],
    api: RuntimeDep,
    _session: SessionDep,
) -> dict[str, Any]:
    target_g = payload.get("target_g")
    if not isinstance(target_g, (int, float)):
        raise PublicApiError("target_g must be a number", 400, "invalid_target_g")
    if target_g <= 0:
        raise PublicApiError("target_g must be > 0", 400, "invalid_target_g")
    return api.service.operation_response(api.business.queue_fill(device_name, float(target_g)))


@router.post("/devices/{device_name}/watering/stop", status_code=status.HTTP_202_ACCEPTED)
def stop_watering(
    device_name: str,
    _payload: dict[str, Any],
    api: RuntimeDep,
    _session: SessionDep,
) -> dict[str, Any]:
    return api.service.operation_response(api.business.queue_stop(device_name))


@router.post("/devices/{device_name}/sleep/enable", status_code=status.HTTP_202_ACCEPTED)
def enable_sleep(
    device_name: str,
    _payload: dict[str, Any],
    api: RuntimeDep,
    _session: SessionDep,
) -> dict[str, Any]:
    return api.service.operation_response(api.business.queue_sleep(device_name, True))


@router.post("/devices/{device_name}/sleep/disable", status_code=status.HTTP_202_ACCEPTED)
def disable_sleep(
    device_name: str,
    _payload: dict[str, Any],
    api: RuntimeDep,
    _session: SessionDep,
) -> dict[str, Any]:
    return api.service.operation_response(api.business.queue_sleep(device_name, False))


@router.post("/devices/{device_name}/sleep/interval", status_code=status.HTTP_202_ACCEPTED)
def sleep_interval(
    device_name: str,
    payload: dict[str, Any],
    api: RuntimeDep,
    _session: SessionDep,
) -> dict[str, Any]:
    minutes = payload.get("minutes")
    if not isinstance(minutes, int):
        raise PublicApiError("minutes must be an integer", 400, "invalid_sleep_interval")
    operation_id = api.business.queue_sleep_interval(device_name, minutes)
    if operation_id is None:
        raise PublicApiError(
            f"sleep interval command was not queued for device '{device_name}'",
            409,
            "command_not_queued",
        )
    return api.service.operation_response(operation_id)


@router.post("/devices/{device_name}/zero", status_code=status.HTTP_202_ACCEPTED)
def zero(
    device_name: str,
    _payload: dict[str, Any],
    api: RuntimeDep,
    _session: SessionDep,
) -> dict[str, Any]:
    return api.service.operation_response(api.business.queue_zero(device_name))


@router.post("/devices/{device_name}/calibration", status_code=status.HTTP_202_ACCEPTED)
def calibrate(
    device_name: str,
    payload: dict[str, Any],
    api: RuntimeDep,
    _session: SessionDep,
) -> dict[str, Any]:
    weight_g = payload.get("weight_g")
    if not isinstance(weight_g, (int, float)):
        raise PublicApiError("weight_g must be a number", 400, "invalid_calibration_weight_g")
    return api.service.operation_response(api.business.queue_calibration(device_name, float(weight_g)))


@router.post("/devices/{device_name}/config", status_code=status.HTTP_202_ACCEPTED)
def configure(
    device_name: str,
    payload: dict[str, Any],
    api: RuntimeDep,
    _session: SessionDep,
) -> dict[str, Any]:
    allowed = {
        "device_type", "name", "dry_weight_g", "tare_weight_g",
        "wet_weight_g", "watering_loss_threshold_percent",
    }
    config = {key: value for key, value in payload.items() if key in allowed}
    if not config:
        raise PublicApiError("at least one config field is required", 400, "invalid_config")
    device = api.business.registry.get(device_name)
    operation_id = api.business.queue_device_config(device, config, f"configure {device_name}")
    if operation_id is None:
        raise PublicApiError(
            f"configuration command was not queued for device '{device_name}'",
            409,
            "command_not_queued",
        )
    return api.service.operation_response(operation_id)


@router.post("/devices/{device_name}/queue/clear")
def clear_queue(
    device_name: str,
    _payload: dict[str, Any],
    api: RuntimeDep,
    _session: SessionDep,
) -> dict[str, int]:
    return {"cleared": api.business.clear_device_queue(device_name)}
