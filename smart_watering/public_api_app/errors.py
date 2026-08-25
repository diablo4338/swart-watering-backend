from smart_watering.domain import SmartWateringError


ERROR_TITLES = {
    "active_operation_not_found": "Operation not found",
    "bad_request": "Invalid request",
    "card_action_not_found": "Action not found",
    "card_block_not_found": "Card section not found",
    "command_not_queued": "Command could not be queued",
    "database_unavailable": "Service temporarily unavailable",
    "detected_watering_not_found": "Watering record not found",
    "device_name_conflict": "Device name is already in use",
    "device_status_snapshot_not_found": "Device data is not available yet",
    "device_status_unavailable": "Device data is unavailable",
    "http_error": "Request failed",
    "google_account_not_allowed": "Google account is not allowed",
    "google_oauth_allowlist_missing": "Google sign-in is not configured",
    "google_oauth_not_configured": "Google sign-in is not configured",
    "invalid_credentials": "Invalid credentials",
    "invalid_device_type": "Invalid device type",
    "invalid_enabled": "Invalid switch value",
    "invalid_fertilized": "Invalid fertilizing value",
    "invalid_request": "Invalid request",
    "invalid_google_token": "Invalid Google sign-in",
    "invalid_prometheus_response": "Statistics response is invalid",
    "invalid_release": "App release metadata is invalid",
    "invalid_session": "Session has expired",
    "invalid_session_ttl": "Session configuration is invalid",
    "invalid_token": "Session is invalid",
    "jwt_secret_missing": "Authentication is not configured",
    "missing_token": "Sign-in is required",
    "not_a_plant": "Action is unavailable for this device",
    "not_found": "Not found",
    "prometheus_query_failed": "Statistics query failed",
    "prometheus_unavailable": "Statistics are unavailable",
    "release_not_found": "App release is not available",
    "smart_watering_error": "Unable to complete the action",
    "token_expired": "Session has expired",
}


def error_title(code: str) -> str:
    if code in ERROR_TITLES:
        return ERROR_TITLES[code]
    if code.startswith("invalid_"):
        return "Invalid value"
    return "Request failed"


def error_payload(
    code: str,
    detail: str,
    *,
    retryable: bool = False,
    title: str | None = None,
) -> dict[str, dict[str, object]]:
    """Stable v1 error envelope consumed by all native clients."""
    return {
        "error": {
            "code": code,
            "title": title or error_title(code),
            "detail": detail,
            "retryable": retryable,
        }
    }


class PublicApiError(SmartWateringError):
    def __init__(
        self,
        message: str,
        status_code: int = 400,
        code: str = "bad_request",
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
