from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from smart_watering.domain import SmartWateringError

from .errors import PublicApiError
from .routers import app_releases, auth, devices, operations
from .runtime import ApiRuntime


def create_app(runtime: ApiRuntime) -> FastAPI:
    app = FastAPI(
        title="Smart Watering Public API",
        version="2",
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.runtime = runtime

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started_at = perf_counter()
        response = await call_next(request)
        elapsed_ms = (perf_counter() - started_at) * 1000
        client = request.client.host if request.client else "-"
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
            content={"error": exc.code, "message": str(exc)},
        )

    @app.exception_handler(SmartWateringError)
    async def smart_watering_error(_request: Request, exc: SmartWateringError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": "smart_watering_error", "message": str(exc)},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "message": str(exc)},
        )

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "message": "not found"},
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": "http_error", "message": str(exc.detail)},
        )

    @app.get("/healthz", tags=["system"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(app_releases.router)
    app.include_router(devices.router)
    app.include_router(operations.router)
    return app
