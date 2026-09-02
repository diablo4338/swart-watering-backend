from contextlib import asynccontextmanager
from functools import partial
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from smart_watering.domain import DeviceNameConflictError, SmartWateringError
from smart_watering.infrastructure.database import DatabaseError

from .errors import PublicApiError, error_payload
from .idempotency import IdempotencyMiddleware
from .routers import app_releases, auth, cards
from .runtime import ApiRuntime


def create_app(runtime: ApiRuntime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runtime.presence_monitor.start()
        try:
            yield
        finally:
            await runtime.presence_monitor.stop()

    app = FastAPI(
        title="Smart Watering Public API",
        version="3.0.0",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.runtime = runtime

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started_at = perf_counter()
        response = await call_next(request)
        elapsed_ms = (perf_counter() - started_at) * 1000
        client_address = request.client
        client = client_address.host if client_address is not None else "-"
        print(
            f'{client} "{request.method} {request.url.path}" '
            f"{response.status_code} {elapsed_ms:.1f}ms",
            flush=True,
        )
        return response

    @app.exception_handler(PublicApiError)
    async def public_api_error(_request: Request, exc: PublicApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.code, str(exc), retryable=exc.retryable),
        )

    @app.exception_handler(DeviceNameConflictError)
    async def device_name_conflict(_request: Request, exc: DeviceNameConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=error_payload("device_name_conflict", str(exc)),
        )

    @app.exception_handler(DatabaseError)
    async def database_error(_request: Request, _exc: DatabaseError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content=error_payload(
                "database_unavailable",
                "database is temporarily unavailable",
                retryable=True,
            ),
        )

    @app.exception_handler(SmartWateringError)
    async def smart_watering_error(_request: Request, exc: SmartWateringError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=error_payload("smart_watering_error", str(exc)),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=error_payload("invalid_request", str(exc)),
        )

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return JSONResponse(
                status_code=404,
                content=error_payload("not_found", "not found"),
            )
        detail = exc.detail
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(
                "http_error",
                detail if isinstance(detail, str) else "HTTP error",
            ),
        )

    @app.get("/healthz", tags=["system"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(app_releases.router)
    app.include_router(app_releases.legacy_router)
    app.include_router(cards.router)
    idempotency_middleware = partial(
        IdempotencyMiddleware,
        store=runtime.business.store,
    )
    app.add_middleware(idempotency_middleware)
    return app
