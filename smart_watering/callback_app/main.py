from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .service import CallbackService


def create_app(service: CallbackService) -> FastAPI:
    app = FastAPI(
        title="Smart Watering Callback API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started_at = perf_counter()
        response = await call_next(request)
        elapsed_ms = (perf_counter() - started_at) * 1000
        client = request.client.host if request.client else "-"
        detail = getattr(request.state, "log_detail", "")
        suffix = f" {detail}" if detail else ""
        print(
            f'{client} "{request.method} {request.url.path}" '
            f"{response.status_code} {elapsed_ms:.1f}ms{suffix}",
            flush=True,
        )
        return response

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, str]:
        request.state.log_detail = "healthz"
        return {"status": "ok"}

    @app.post("/operations/callback")
    async def callback(request: Request):
        try:
            payload = await request.json()
        except ValueError:
            request.state.log_detail = "invalid_json"
            return JSONResponse(400, {"error": "missing_operation_id"})
        if not isinstance(payload, dict):
            request.state.log_detail = "missing_operation_id"
            return JSONResponse(400, {"error": "missing_operation_id"})
        try:
            result = service.record(payload)
        except ValueError:
            request.state.log_detail = "missing_operation_id"
            return JSONResponse(400, {"error": "missing_operation_id"})
        request.state.log_detail = result.log_detail
        return {"status": "recorded"}

    return app
