import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from ..errors import PublicApiError
from ..runtime import ApiRuntime


router = APIRouter(prefix="/api/v3/app", tags=["app"])
legacy_router = APIRouter(prefix="/api/v2/app", tags=["app"])


def runtime(request: Request) -> ApiRuntime:
    return request.app.state.runtime


def _manifest(releases_dir: Path, version_code: int | None = None) -> dict[str, Any]:
    path = releases_dir / (
        "latest.json" if version_code is None else f"{version_code}/manifest.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PublicApiError(
            "no Android release has been published", 404, "release_not_found"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicApiError(
            "published Android release metadata is invalid", 500, "invalid_release"
        ) from exc

    version_name = payload.get("version_name")
    version_code = payload.get("version_code")
    filename = payload.get("filename")
    if (
        not isinstance(version_name, str)
        or not isinstance(version_code, int)
        or not isinstance(filename, str)
        or Path(filename).name != filename
        or not filename.endswith(".apk")
    ):
        raise PublicApiError(
            "published Android release metadata is invalid", 500, "invalid_release"
        )
    return payload


def _latest_release(
    request: Request,
    app_runtime: ApiRuntime,
    download_route_name: str,
) -> dict[str, Any]:
    payload = _manifest(app_runtime.settings.android_releases_dir)
    version_code = payload["version_code"]
    return {
        **payload,
        "download_url": str(request.url_for(download_route_name, version_code=version_code)),
    }


@router.get("/latest")
def latest_release(
    request: Request,
    app_runtime: Annotated[ApiRuntime, Depends(runtime)],
) -> dict[str, Any]:
    return _latest_release(request, app_runtime, "download_release")


@legacy_router.get("/latest", deprecated=True)
def legacy_latest_release(
    request: Request,
    app_runtime: Annotated[ApiRuntime, Depends(runtime)],
) -> dict[str, Any]:
    return _latest_release(request, app_runtime, "legacy_download_release")


def _download_release(
    version_code: int,
    app_runtime: ApiRuntime,
) -> FileResponse:
    payload = _manifest(app_runtime.settings.android_releases_dir, version_code)
    if version_code != payload["version_code"]:
        raise PublicApiError("Android release does not exist", 404, "release_not_found")
    apk = app_runtime.settings.android_releases_dir / str(version_code) / payload["filename"]
    if not apk.is_file():
        raise PublicApiError("Android release file does not exist", 404, "release_not_found")
    return FileResponse(
        apk,
        media_type="application/vnd.android.package-archive",
        filename=payload["filename"],
    )


@router.get("/releases/{version_code}/download", name="download_release")
def download_release(
    version_code: int,
    app_runtime: Annotated[ApiRuntime, Depends(runtime)],
) -> FileResponse:
    return _download_release(version_code, app_runtime)


@legacy_router.get(
    "/releases/{version_code}/download",
    name="legacy_download_release",
    deprecated=True,
)
def legacy_download_release(
    version_code: int,
    app_runtime: Annotated[ApiRuntime, Depends(runtime)],
) -> FileResponse:
    return _download_release(version_code, app_runtime)
