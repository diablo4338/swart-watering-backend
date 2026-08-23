from typing import Any

from fastapi import APIRouter

from ..dependencies import RuntimeDep, SessionDep


router = APIRouter(prefix="/api/v2", tags=["operations"])


def operations_response(
    api: RuntimeDep,
    device: str | None = None,
    active: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    source = (
        api.business.operations.list_non_terminal(device_name=device)
        if active
        else api.business.operations.list_recent(device_name=device)
    )
    return {"operations": api.service.operations_response(source)}


@router.get("/devices/{device_name}/operations")
def list_device_operations(
    device_name: str,
    api: RuntimeDep,
    _session: SessionDep,
    active: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    return operations_response(api, device_name, active)


@router.get("/operations/{operation_id}")
def operation(operation_id: str, api: RuntimeDep, _session: SessionDep) -> dict[str, Any]:
    return api.service.operation_response(operation_id)


@router.get("/operations/{operation_id}/events")
def operation_events(operation_id: str, api: RuntimeDep, _session: SessionDep) -> dict[str, Any]:
    return api.service.operation_events_response(operation_id)


@router.get("/operations/{operation_id}/trace")
def operation_trace(operation_id: str, api: RuntimeDep, _session: SessionDep) -> dict[str, Any]:
    return api.service.operation_trace_response(operation_id)


@router.get("/watering/history")
def watering_history(
    api: RuntimeDep,
    _session: SessionDep,
    successful: bool = False,
) -> dict[str, Any]:
    return api.service.watering_history_response(successful)
