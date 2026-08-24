import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import yaml

from smart_watering.application.service import SmartWateringService
from smart_watering.infrastructure.database import DatabaseError
from smart_watering.public_api_app import create_app
from smart_watering.public_api_app.runtime import ApiRuntime, ApiSettings
from smart_watering.public_api_app.service import DeviceStateProjectionService


def make_client(temp_dir: str) -> tuple[TestClient, SmartWateringService]:
    cli = SmartWateringService(str(Path(temp_dir) / "test.db"))
    settings = ApiSettings(
        jwt_secret="secret",
        session_ttl_sec=3600,
        google_web_client_id="",
        google_allowed_emails=set(),
        google_allowed_domains=set(),
        prometheus_url="http://127.0.0.1:9090",
        statistics_timezone=ZoneInfo("Europe/Berlin"),
        android_releases_dir=Path(temp_dir) / "releases",
    )
    return TestClient(create_app(ApiRuntime(cli, settings))), cli


def test_android_release_metadata_and_download_are_public() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        releases_dir = Path(temp_dir) / "releases"
        version_dir = releases_dir / "1001"
        version_dir.mkdir(parents=True)
        manifest = {
            "version_name": "1.0.0",
            "version_code": 1001,
            "filename": "smart-watering-1.0.0-1001.apk",
            "sha256": "abc",
            "size": 3,
            "published_at": "2026-08-04T00:00:00Z",
            "git_commit": "deadbeef",
        }
        (version_dir / manifest["filename"]).write_bytes(b"apk")
        (version_dir / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (releases_dir / "latest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        client, _cli = make_client(temp_dir)

        latest = client.get("/api/v2/app/latest")
        download = client.get("/api/v2/app/releases/1001/download")

        assert latest.status_code == 200
        assert latest.json()["version_code"] == 1001
        assert latest.json()["download_url"].endswith(
            "/api/v2/app/releases/1001/download"
        )
        assert download.status_code == 200
        assert download.content == b"apk"


def test_android_release_returns_not_found_before_first_publication() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        client, _cli = make_client(temp_dir)

        response = client.get("/api/v2/app/latest")

        assert response.status_code == 404
        assert response.json()["error"] == "release_not_found"


def test_fastapi_health_and_auth_contract() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        client, cli = make_client(temp_dir)
        cli.auth.add_user("client", "secret-password")

        assert client.get("/healthz").json() == {"status": "ok"}
        unauthorized = client.get("/api/v3/devices")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"] == "missing_token"

        login = client.post(
            "/api/v3/auth/login",
            json={"username": "client", "password": "secret-password"},
        )
        assert login.status_code == 200
        token = login.json()["token"]
        devices = client.get(
            "/api/v3/devices",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert devices.status_code == 200
        assert devices.json() == {"devices": []}


def test_database_errors_return_safe_service_unavailable_response() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        client, cli = make_client(temp_dir)
        cli.auth.add_user("client", "secret-password")
        token = client.post(
            "/api/v3/auth/login",
            json={"username": "client", "password": "secret-password"},
        ).json()["token"]

        def fail_session_lookup(_session_id: str) -> None:
            raise DatabaseError("(sqlite3.OperationalError) database is locked; SELECT secret")

        cli.auth.require_active_session = fail_session_lookup
        response = client.get(
            "/api/v3/devices",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 503
        assert response.json() == {
            "error": "database_unavailable",
            "message": "database is temporarily unavailable",
        }
        assert "SELECT secret" not in response.text


def test_only_android_release_routes_remain_on_v2() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        client, _cli = make_client(temp_dir)
        paths = client.get("/openapi.json").json()["paths"]

        assert {path for path in paths if path.startswith("/api/v2/")} == {
            "/api/v2/app/latest",
            "/api/v2/app/releases/{version_code}/download",
        }
        assert "/api/v3/auth/login" in paths
        assert "/api/v3/auth/google" in paths
        assert "/api/v3/auth/logout" in paths
        assert client.post("/api/v2/auth/login", json={}).status_code == 404
        assert client.get("/api/v2/devices").status_code == 404


def test_checked_in_openapi_matches_registered_routes() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        client, _cli = make_client(temp_dir)
        checked_in = yaml.safe_load(
            (Path(__file__).parents[1] / "public_api.openapi.yaml").read_text(
                encoding="utf-8"
            )
        )

        assert checked_in == client.get("/openapi.json").json()


def test_v3_device_card_exposes_server_driven_blocks_and_actions() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        client, cli = make_client(temp_dir)
        cli.auth.add_user("client", "secret-password")
        cli.registry.add("10.0.0.1", "plant", "avocado")
        token = client.post(
            "/api/v3/auth/login",
            json={"username": "client", "password": "secret-password"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        devices = client.get("/api/v3/devices", headers=headers)
        with (
            patch.object(
                DeviceStateProjectionService,
                "project_watering_history",
                side_effect=AssertionError("initial card must not load history"),
            ),
        ):
            card = client.get("/api/v3/devices/avocado/card", headers=headers)

        assert devices.status_code == 200
        assert devices.json()["devices"] == [{
            "id": "avocado",
            "name": "avocado",
            "device_type": "plant",
            "card_profile": "plant.v1",
            "card_href": "/api/v3/devices/avocado/card",
        }]
        assert card.status_code == 200
        blocks = {block["id"]: block for block in card.json()["blocks"]}
        assert list(blocks) == [
            "overview", "control", "watering_parameters", "watering_history",
            "operation_queue",
        ]
        controls = {
            control["id"]: control
            for control in blocks["control"]["schema"]["controls"]
        }
        assert controls["sleep_enabled"]["control_type"] == "action_toggle.v1"
        assert controls["capture_zero"]["preset"] == "zero_capture_hold.v1"
        assert blocks["control"]["data"] == {}
        assert blocks["watering_parameters"]["data"] == {}
        assert {
            control["id"]
            for control in blocks["watering_parameters"]["schema"]["controls"]
        } >= {"save_watering_parameters"}
        assert blocks["watering_history"]["data"] == {}
        assert blocks["operation_queue"]["data"] == {"items": []}

        history = client.get(
            "/api/v3/devices/avocado/card/blocks/watering_history",
            headers=headers,
        )
        assert history.status_code == 200
        assert history.json()["block"]["data"] == {"items": [], "next_offset": None}

        runtime = client.app.state.runtime
        runtime.presence.mark_offline("avocado", "test offline")
        overview = client.get(
            "/api/v3/devices/avocado/card/blocks/overview",
            headers=headers,
        )
        assert overview.status_code == 200
        assert isinstance(overview.json()["card_revision"], int)
        assert "card_calculate_revision" not in overview.json()
        assert overview.json()["block"]["data"]["status"]["code"] == "offline"
        assert overview.json()["block"]["data"]["source"] == "none"

        runtime.presence.mark_online("avocado")
        with patch.object(
            DeviceStateProjectionService,
            "project_live_device_state",
            return_value={
                "available": True,
                "source": "live",
                "result": {
                    "weight": {"gross_weight_g": 1234},
                    "config": {
                        "dry_weight_g": 1000,
                        "wet_weight_g": 1500,
                        "watering_loss_threshold_percent": 10,
                    },
                },
                "result_received_at": 123.0,
            },
        ):
            live_overview = client.get(
                "/api/v3/devices/avocado/card/blocks/overview",
                headers=headers,
            )
        live_data = live_overview.json()["block"]["data"]
        assert live_data["status"]["code"] == "online"
        assert live_data["source"] == "live"
        assert live_data["primary_value"]["value"] == 184
        runtime.presence.mark_offline("avocado", "test offline")

        action = client.post(
            "/api/v3/devices/avocado/actions/set-sleep",
            json={"enabled": True},
            headers=headers,
        )

        assert action.status_code == 200
        assert action.json()["accepted"] is True
        assert cli.operations.latest_non_terminal("avocado", "sleep_enable") is not None
        interval_action = client.post(
            "/api/v3/devices/avocado/actions/set-sleep-interval",
            json={"minutes": 17},
            headers=headers,
        )
        assert interval_action.status_code == 200
        queue = client.get(
            "/api/v3/devices/avocado/card/blocks/operation_queue",
            headers=headers,
        )
        assert queue.status_code == 200
        queued_item = queue.json()["block"]["data"]["items"][0]
        assert queued_item["type"] == "sleep_interval"
        assert queued_item["payload"] == "17 min"
        assert queued_item["actions"][0]["label"] == "Delete"

        cleared = client.post(
            "/api/v3/devices/avocado/actions/cancel-operation",
            json={"operation_id": queued_item["id"]},
            headers=headers,
        )
        assert cleared.status_code == 200
        remaining = cli.operations.details_from_operations(
            cli.operations.list_non_terminal("avocado")
        )
        assert [operation["type"] for operation in remaining] == ["sleep_enable"]
        updated_control = next(
            block for block in action.json()["card"]["blocks"]
            if block["id"] == "control"
        )
        sleep = next(
            control for control in updated_control["schema"]["controls"]
            if control["id"] == "sleep_enabled"
        )
        assert sleep["enabled"] is False



