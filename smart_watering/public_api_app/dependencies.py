from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from smart_watering.domain import SmartWateringError

from .errors import PublicApiError
from .runtime import ApiRuntime
from .security import verify_jwt


bearer_scheme = HTTPBearer(auto_error=False)


def runtime(request: Request) -> ApiRuntime:
    return request.app.state.runtime


def authenticated_session(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    api: Annotated[ApiRuntime, Depends(runtime)],
) -> dict:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise PublicApiError("missing bearer token", 401, "missing_token")
    payload = verify_jwt(credentials.credentials, api.settings.jwt_secret)
    session_id = payload.get("sid")
    if not isinstance(session_id, str) or not session_id:
        raise PublicApiError("JWT session is missing", 401, "invalid_token")
    try:
        api.business.auth.require_active_session(session_id)
    except SmartWateringError as exc:
        raise PublicApiError(str(exc), 401, "invalid_session") from exc
    return payload


RuntimeDep = Annotated[ApiRuntime, Depends(runtime)]
SessionDep = Annotated[dict, Depends(authenticated_session)]
