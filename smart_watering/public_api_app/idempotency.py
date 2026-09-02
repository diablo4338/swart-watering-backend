import hashlib
import time
from threading import Lock

from fastapi import Request
from sqlalchemy import delete
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from smart_watering.infrastructure.database import DatabaseStore, IdempotencyRecord


IDEMPOTENT_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
MIN_KEY_LENGTH = 8
MAX_KEY_LENGTH = 128
RECORD_TTL_SEC = 24 * 60 * 60


class IdempotencyMiddleware:
    def __init__(self, app: ASGIApp, store: DatabaseStore) -> None:
        self.app = app
        self.store = store
        self.lock = Lock()

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] not in IDEMPOTENT_METHODS
            or not scope["path"].startswith("/api/v3/devices/")
        ):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        key = request.headers.get("Idempotency-Key")
        if key is None:
            await self.app(scope, receive, send)
            return
        if not MIN_KEY_LENGTH <= len(key) <= MAX_KEY_LENGTH:
            await self._send_json(
                scope,
                receive,
                send,
                400,
                "invalid_idempotency_key",
                f"Idempotency-Key must contain {MIN_KEY_LENGTH} to {MAX_KEY_LENGTH} characters",
            )
            return

        body = await request.body()
        scope_key = hashlib.sha256(
            request.headers.get("Authorization", "anonymous").encode("utf-8")
        ).hexdigest()
        request_hash = hashlib.sha256(
            b"\0".join((scope["method"].encode(), scope["raw_path"], scope.get("query_string", b""), body))
        ).hexdigest()

        with self.lock:
            with self.store.session() as session:
                session.execute(
                    delete(IdempotencyRecord).where(
                        IdempotencyRecord.updated_at < time.time() - RECORD_TTL_SEC
                    )
                )
                record = session.get(IdempotencyRecord, (scope_key, key))
                if record is None:
                    now = time.time()
                    session.add(
                        IdempotencyRecord(
                            scope_key=scope_key,
                            idempotency_key=key,
                            request_hash=request_hash,
                            status_code=None,
                            response_body=None,
                            content_type=None,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    state = "new"
                elif record.request_hash != request_hash:
                    state = "conflict"
                elif record.status_code is None:
                    state = "pending"
                else:
                    state = "replay"
                    cached_status = record.status_code
                    cached_body = (record.response_body or "").encode("utf-8")
                    cached_content_type = record.content_type

        if state == "conflict":
            await self._send_json(scope, receive, send, 409, "idempotency_key_conflict", "Idempotency-Key was already used for another request")
            return
        if state == "pending":
            await self._send_json(scope, receive, send, 409, "idempotency_request_in_progress", "A request with this Idempotency-Key is still in progress")
            return
        if state == "replay":
            response = Response(cached_body, status_code=cached_status, media_type=cached_content_type)
            response.headers["Idempotency-Replayed"] = "true"
            await response(scope, receive, send)
            return

        messages = []

        async def replay_receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def capture_send(message):
            messages.append(message)

        try:
            await self.app(scope, replay_receive, capture_send)
        except Exception:
            self._delete(scope_key, key)
            raise

        start = next(message for message in messages if message["type"] == "http.response.start")
        response_body = b"".join(
            message.get("body", b"") for message in messages if message["type"] == "http.response.body"
        )
        status_code = start["status"]
        if status_code < 500:
            content_type = next(
                (value.decode("latin-1") for name, value in start.get("headers", []) if name.lower() == b"content-type"),
                None,
            )
            with self.store.session() as session:
                record = session.get(IdempotencyRecord, (scope_key, key))
                if record is not None:
                    record.status_code = status_code
                    record.response_body = response_body.decode("utf-8")
                    record.content_type = content_type
                    record.updated_at = time.time()
        else:
            self._delete(scope_key, key)

        for message in messages:
            await send(message)

    def _delete(self, scope_key: str, key: str) -> None:
        with self.store.session() as session:
            record = session.get(IdempotencyRecord, (scope_key, key))
            if record is not None:
                session.delete(record)

    @staticmethod
    async def _send_json(scope, receive, send, status_code: int, code: str, message: str) -> None:
        response = JSONResponse(status_code=status_code, content={"error": code, "message": message})
        await response(scope, receive, send)
