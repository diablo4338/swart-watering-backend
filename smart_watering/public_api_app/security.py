import base64
import hashlib
import hmac
import json
import time
from typing import Any

from smart_watering.domain import UserSession

from .config import (
    DEFAULT_TOKEN_TTL_SEC,
    GOOGLE_ALLOWED_DOMAINS_ENV,
    GOOGLE_ALLOWED_EMAILS_ENV,
    GOOGLE_WEB_CLIENT_ID_ENV,
)
from .errors import PublicApiError


def base64url_encode(raw_value: bytes) -> str:
    return base64.urlsafe_b64encode(raw_value).decode("ascii").rstrip("=")


def base64url_decode(raw_value: str) -> bytes:
    padding = "=" * (-len(raw_value) % 4)
    return base64.urlsafe_b64decode((raw_value + padding).encode("ascii"))


def create_jwt(
    secret: str,
    subject: str,
    ttl_sec: int = DEFAULT_TOKEN_TTL_SEC,
    session_id: str | None = None,
) -> str:
    if not secret:
        raise PublicApiError("JWT secret is required", 500, "jwt_secret_missing")
    now = int(time.time())
    payload = {"sub": subject, "iat": now, "exp": now + ttl_sec}
    if session_id is not None:
        payload["sid"] = session_id
    header = base64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":"), sort_keys=True).encode())
    body = base64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{header}.{body}"
    signature = hmac.new(secret.encode(), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{base64url_encode(signature)}"


def create_session_jwt(secret: str, user_session: UserSession) -> str:
    ttl_sec = max(1, int(user_session.expires_at - time.time()))
    return create_jwt(secret, user_session.username, ttl_sec, user_session.session_id)


def verify_jwt(token: str, secret: str) -> dict[str, Any]:
    if not secret:
        raise PublicApiError("JWT secret is not configured", 500, "jwt_secret_missing")
    parts = token.split(".")
    if len(parts) != 3:
        raise PublicApiError("invalid JWT", 401, "invalid_token")
    signing_input = f"{parts[0]}.{parts[1]}"
    expected = hmac.new(secret.encode(), signing_input.encode("ascii"), hashlib.sha256).digest()
    try:
        actual = base64url_decode(parts[2])
    except (ValueError, UnicodeError):
        raise PublicApiError("invalid JWT signature", 401, "invalid_token")
    if not hmac.compare_digest(actual, expected):
        raise PublicApiError("invalid JWT signature", 401, "invalid_token")
    try:
        header = json.loads(base64url_decode(parts[0]).decode())
        payload = json.loads(base64url_decode(parts[1]).decode())
    except (json.JSONDecodeError, UnicodeError, ValueError):
        raise PublicApiError("invalid JWT payload", 401, "invalid_token")
    if header.get("alg") != "HS256":
        raise PublicApiError("unsupported JWT algorithm", 401, "invalid_token")
    if not isinstance(payload.get("exp"), int) or payload["exp"] < int(time.time()):
        raise PublicApiError("JWT expired", 401, "token_expired")
    return payload


def verify_google_id_token(token: str, web_client_id: str) -> dict[str, Any]:
    if not web_client_id:
        raise PublicApiError(f"{GOOGLE_WEB_CLIENT_ID_ENV} is required", 500, "google_oauth_not_configured")
    from google.auth.transport import requests
    from google.oauth2 import id_token
    try:
        payload = id_token.verify_oauth2_token(token, requests.Request(), web_client_id)
    except ValueError as exc:
        raise PublicApiError("invalid Google ID token", 401, "invalid_google_token") from exc
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        raise PublicApiError("Google ID token subject is missing", 401, "invalid_google_token")
    return dict(payload)


def require_allowed_google_identity(
    google_payload: dict[str, Any],
    allowed_emails: set[str],
    allowed_domains: set[str],
) -> None:
    if not allowed_emails and not allowed_domains:
        raise PublicApiError(
            f"{GOOGLE_ALLOWED_EMAILS_ENV} or {GOOGLE_ALLOWED_DOMAINS_ENV} is required",
            500,
            "google_oauth_allowlist_missing",
        )
    email = google_payload.get("email")
    if not isinstance(email, str) or not email:
        raise PublicApiError("Google account email is missing", 401, "google_account_not_allowed")
    email = email.lower()
    if email in allowed_emails:
        return
    if google_payload.get("email_verified") is not True:
        raise PublicApiError("Google account email is not verified", 401, "google_account_not_allowed")
    hosted_domain = google_payload.get("hd")
    if isinstance(hosted_domain, str) and hosted_domain.lower() in allowed_domains:
        return
    if email.rsplit("@", 1)[-1] in allowed_domains:
        return
    raise PublicApiError("Google account is not allowed", 403, "google_account_not_allowed")
