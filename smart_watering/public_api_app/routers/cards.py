from typing import Any

from fastapi import APIRouter

from ..dependencies import RuntimeDep, SessionDep


router = APIRouter(prefix="/api/v3", tags=["device-cards"])


@router.get("/devices")
def project_device_catalog(api: RuntimeDep, _session: SessionDep) -> dict[str, Any]:
    return api.cards.project_device_catalog()


@router.get("/devices/{device_id}/card")
def project_card(device_id: str, api: RuntimeDep, _session: SessionDep) -> dict[str, Any]:
    return api.cards.project_card(device_id)


@router.get("/devices/{device_id}/card/blocks/{block_id}")
def project_block(
    device_id: str,
    block_id: str,
    api: RuntimeDep,
    _session: SessionDep,
) -> dict[str, Any]:
    return api.cards.project_block(device_id, block_id)


@router.post("/devices/{device_id}/actions/{action}")
def execute_device_action(
    device_id: str,
    action: str,
    payload: dict[str, Any],
    api: RuntimeDep,
    _session: SessionDep,
) -> dict[str, Any]:
    return api.cards.execute_action(device_id, action, payload)


