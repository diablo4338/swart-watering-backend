from typing import Any

from fastapi import APIRouter

from smart_watering.domain import SmartWateringError

from ..dependencies import RuntimeDep, SessionDep
from ..errors import PublicApiError
from .. import security


router = APIRouter(prefix="/api/v3/auth", tags=["auth"])


@router.post("/login")
def login(payload: dict[str, Any], api: RuntimeDep) -> dict[str, Any]:
    username = payload.get("username")
    password = payload.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise PublicApiError("username and password are required", 400, "invalid_credentials")
    try:
        session = api.business.auth.create_session(username, password, api.settings.session_ttl_sec)
    except SmartWateringError as exc:
        raise PublicApiError(str(exc), 401, "invalid_credentials") from exc
    return {
        "token": security.create_session_jwt(api.settings.jwt_secret, session),
        "expires_at": session.expires_at,
    }


@router.post("/google")
def google_login(payload: dict[str, Any], api: RuntimeDep) -> dict[str, Any]:
    token = payload.get("id_token")
    if not isinstance(token, str) or not token:
        raise PublicApiError("id_token is required", 400, "invalid_google_token")
    google_payload = security.verify_google_id_token(token, api.settings.google_web_client_id)
    security.require_allowed_google_identity(
        google_payload,
        api.settings.google_allowed_emails,
        api.settings.google_allowed_domains,
    )
    session = api.business.auth.create_external_session(
        "google",
        google_payload["sub"],
        api.settings.session_ttl_sec,
    )
    return {
        "token": security.create_session_jwt(api.settings.jwt_secret, session),
        "expires_at": session.expires_at,
    }


@router.post("/logout")
def logout(session: SessionDep, api: RuntimeDep) -> dict[str, str]:
    session_id = session.get("sid")
    if isinstance(session_id, str):
        api.business.auth.revoke_session(session_id)
    return {"status": "logged_out"}
