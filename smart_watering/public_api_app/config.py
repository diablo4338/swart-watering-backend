import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from smart_watering.domain import SmartWateringError

from .errors import PublicApiError


PUBLIC_API_JWT_SECRET_ENV = "SMART_WATERING_PUBLIC_API_JWT_SECRET"
PUBLIC_API_SESSION_TTL_SEC_ENV = "SMART_WATERING_PUBLIC_API_SESSION_TTL_SEC"
GOOGLE_WEB_CLIENT_ID_ENV = "SMART_WATERING_GOOGLE_WEB_CLIENT_ID"
GOOGLE_ALLOWED_EMAILS_ENV = "SMART_WATERING_GOOGLE_ALLOWED_EMAILS"
GOOGLE_ALLOWED_DOMAINS_ENV = "SMART_WATERING_GOOGLE_ALLOWED_DOMAINS"
PROMETHEUS_URL_ENV = "SMART_WATERING_PROMETHEUS_URL"
STATISTICS_TIMEZONE_ENV = "SMART_WATERING_STATISTICS_TIMEZONE"
CONSUMPTION_DROP_THRESHOLD_PERCENT_ENV = "SMART_WATERING_CONSUMPTION_DROP_THRESHOLD_PERCENT"
CONSUMPTION_MEDIAN_DAYS_ENV = "SMART_WATERING_CONSUMPTION_MEDIAN_DAYS"
DEFAULT_TOKEN_TTL_SEC = 3600
DEFAULT_PROMETHEUS_URL = "http://127.0.0.1:9090"
DEFAULT_STATISTICS_TIMEZONE = "Europe/Berlin"
DEFAULT_CONSUMPTION_DROP_THRESHOLD_PERCENT = 30
DEFAULT_CONSUMPTION_MEDIAN_DAYS = 5


def parse_csv_env(raw_value: str) -> set[str]:
    return {item.strip().lower() for item in raw_value.split(",") if item.strip()}


def public_api_session_ttl_sec() -> int:
    raw_value = os.environ.get(PUBLIC_API_SESSION_TTL_SEC_ENV)
    if raw_value is None or raw_value.strip() == "":
        return DEFAULT_TOKEN_TTL_SEC
    try:
        ttl_sec = int(raw_value)
    except ValueError as exc:
        raise PublicApiError(
            f"{PUBLIC_API_SESSION_TTL_SEC_ENV} must be an integer",
            500,
            "invalid_session_ttl",
        ) from exc
    if ttl_sec <= 0:
        raise PublicApiError(
            f"{PUBLIC_API_SESSION_TTL_SEC_ENV} must be > 0",
            500,
            "invalid_session_ttl",
        )
    return ttl_sec


def consumption_drop_threshold_percent() -> int:
    raw_value = os.environ.get(CONSUMPTION_DROP_THRESHOLD_PERCENT_ENV)
    if raw_value is None or raw_value.strip() == "":
        return DEFAULT_CONSUMPTION_DROP_THRESHOLD_PERCENT
    try:
        threshold = int(raw_value)
    except ValueError as exc:
        raise SmartWateringError(
            f"{CONSUMPTION_DROP_THRESHOLD_PERCENT_ENV} must be an integer"
        ) from exc
    if not 0 <= threshold <= 100:
        raise SmartWateringError(
            f"{CONSUMPTION_DROP_THRESHOLD_PERCENT_ENV} must be between 0 and 100"
        )
    return threshold


def consumption_median_days() -> int:
    raw_value = os.environ.get(CONSUMPTION_MEDIAN_DAYS_ENV)
    if raw_value is None or raw_value.strip() == "":
        return DEFAULT_CONSUMPTION_MEDIAN_DAYS
    try:
        days = int(raw_value)
    except ValueError as exc:
        raise SmartWateringError(
            f"{CONSUMPTION_MEDIAN_DAYS_ENV} must be an integer"
        ) from exc
    if days <= 0:
        raise SmartWateringError(f"{CONSUMPTION_MEDIAN_DAYS_ENV} must be > 0")
    return days


@dataclass(frozen=True)
class ApiSettings:
    jwt_secret: str
    session_ttl_sec: int
    google_web_client_id: str
    google_allowed_emails: set[str]
    google_allowed_domains: set[str]
    prometheus_url: str
    statistics_timezone: ZoneInfo
    consumption_drop_threshold_percent: int = DEFAULT_CONSUMPTION_DROP_THRESHOLD_PERCENT
    consumption_median_days: int = DEFAULT_CONSUMPTION_MEDIAN_DAYS

    @classmethod
    def from_env(cls) -> "ApiSettings":
        jwt_secret = os.environ.get(PUBLIC_API_JWT_SECRET_ENV, "")
        if not jwt_secret:
            raise SmartWateringError(f"{PUBLIC_API_JWT_SECRET_ENV} is required")
        timezone_name = os.environ.get(STATISTICS_TIMEZONE_ENV, DEFAULT_STATISTICS_TIMEZONE)
        try:
            statistics_timezone = ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise SmartWateringError(f"invalid {STATISTICS_TIMEZONE_ENV}: {timezone_name}") from exc
        return cls(
            jwt_secret=jwt_secret,
            session_ttl_sec=public_api_session_ttl_sec(),
            google_web_client_id=os.environ.get(GOOGLE_WEB_CLIENT_ID_ENV, ""),
            google_allowed_emails=parse_csv_env(os.environ.get(GOOGLE_ALLOWED_EMAILS_ENV, "")),
            google_allowed_domains=parse_csv_env(os.environ.get(GOOGLE_ALLOWED_DOMAINS_ENV, "")),
            prometheus_url=os.environ.get(PROMETHEUS_URL_ENV, DEFAULT_PROMETHEUS_URL),
            statistics_timezone=statistics_timezone,
            consumption_drop_threshold_percent=consumption_drop_threshold_percent(),
            consumption_median_days=consumption_median_days(),
        )
