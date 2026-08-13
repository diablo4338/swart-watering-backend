import json
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from smart_watering.application.service import SmartWateringService
from smart_watering.public_api_app import create_app
from smart_watering.public_api_app.runtime import ApiRuntime, ApiSettings


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
        unauthorized = client.get("/api/v2/devices")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"] == "missing_token"

        login = client.post(
            "/api/v2/auth/login",
            json={"username": "client", "password": "secret-password"},
        )
        assert login.status_code == 200
        token = login.json()["token"]
        devices = client.get(
            "/api/v2/devices",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert devices.status_code == 200
        assert devices.json() == {"devices": []}


def test_fastapi_registers_documented_v2_routes() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        client, _cli = make_client(temp_dir)
        paths = client.get("/openapi.json").json()["paths"]

        assert "/api/v2/devices/{device_name}/water-consumption" in paths
        assert "/api/v2/devices/{device_name}/watering/start" in paths
        assert "/api/v2/devices/{device_name}/operations" in paths
        assert "/api/v2/operations" not in paths
        assert "/api/v2/operations/{operation_id}/events" in paths
        assert "/api/v2/devices/{device_name}/detected-waterings" in paths
        assert (
            "/api/v2/devices/{device_name}/detected-waterings/{event_id}" in paths
        )


def test_mutating_request_with_idempotency_key_replays_original_response() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        client, cli = make_client(temp_dir)
        cli.auth.add_user("client", "secret-password")
        cli.registry.add("10.0.0.1", "plant", "plant_1")
        token = client.post(
            "/api/v2/auth/login",
            json={"username": "client", "password": "secret-password"},
        ).json()["token"]
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "018f927c-5c9a-7c14-a8e0-88d00bca0177",
        }
        cli.queue_device_status("plant_1")

        first = client.post(
            "/api/v2/devices/plant_1/queue/clear", json={}, headers=headers
        )
        replay = client.post(
            "/api/v2/devices/plant_1/queue/clear", json={}, headers=headers
        )

        assert first.status_code == 200
        assert first.json() == {"cleared": 1}
        assert replay.status_code == 200
        assert replay.json() == first.json()
        assert replay.headers["Idempotency-Replayed"] == "true"


def test_idempotency_key_cannot_be_reused_for_another_request() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        client, cli = make_client(temp_dir)
        cli.auth.add_user("client", "secret-password")
        cli.registry.add("10.0.0.1", "plant", "plant_1")
        token = client.post(
            "/api/v2/auth/login",
            json={"username": "client", "password": "secret-password"},
        ).json()["token"]
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "018f927c-5c9a-7c14-a8e0-88d00bca0178",
        }

        first = client.post(
            "/api/v2/devices/plant_1/queue/clear", json={}, headers=headers
        )
        conflict = client.post(
            "/api/v2/devices/plant_1/status", json={}, headers=headers
        )

        assert first.status_code == 200
        assert conflict.status_code == 409
        assert conflict.json()["error"] == "idempotency_key_conflict"


def test_watering_parameters_are_confirmed_and_timestamped_per_field() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        client, cli = make_client(temp_dir)
        cli.auth.add_user("client", "secret-password")
        cli.registry.add("10.0.0.1", "plant", "plant_1")
        token = client.post(
            "/api/v2/auth/login",
            json={"username": "client", "password": "secret-password"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        endpoint = "/api/v2/devices/plant_1/watering-parameters"

        initial = client.get(endpoint, headers=headers)
        queued = client.put(
            endpoint,
            json={"dry_weight_g": 120, "wet_weight_g": 500},
            headers=headers,
        )

        assert initial.status_code == 200
        assert initial.json()["dry_weight_g"] is None
        assert queued.status_code == 200
        assert queued.json()["dry_weight_g"] is None
        assert queued.json()["dry_weight_updated_at"] is None

        cli.operations.event(queued.json()["operation_id"], "success", "config_updated")
        confirmed = client.get(endpoint, headers=headers).json()
        dry_updated_at = confirmed["dry_weight_updated_at"]
        wet_updated_at = confirmed["wet_weight_updated_at"]

        assert confirmed["dry_weight_g"] == 120
        assert dry_updated_at is not None
        assert confirmed["wet_weight_g"] == 500
        assert wet_updated_at is not None

        wet_only = client.put(endpoint, json={"wet_weight_g": 510}, headers=headers)
        cli.operations.event(wet_only.json()["operation_id"], "success", "config_updated")
        updated = client.get(endpoint, headers=headers).json()

        assert updated["dry_weight_g"] == 120
        assert updated["dry_weight_updated_at"] == dry_updated_at
        assert updated["wet_weight_g"] == 510
        assert updated["wet_weight_updated_at"] >= wet_updated_at


def test_watering_parameters_reject_invalid_or_empty_updates() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        client, cli = make_client(temp_dir)
        cli.auth.add_user("client", "secret-password")
        cli.registry.add("10.0.0.1", "plant", "plant_1")
        token = client.post(
            "/api/v2/auth/login",
            json={"username": "client", "password": "secret-password"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        endpoint = "/api/v2/devices/plant_1/watering-parameters"

        assert client.put(endpoint, json={}, headers=headers).status_code == 400
        assert client.put(
            endpoint, json={"dry_weight_g": 1.5}, headers=headers
        ).status_code == 400
        assert client.put(
            endpoint,
            json={"watering_loss_threshold_percent": 101},
            headers=headers,
        ).status_code == 400


def test_detected_watering_history_soft_delete() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        client, cli = make_client(temp_dir)
        cli.auth.add_user("client", "secret-password")
        cli.registry.add("10.0.0.1", "plant", "plant_1")
        token = client.post(
            "/api/v2/auth/login",
            json={"username": "client", "password": "secret-password"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        event, _created = cli.plant_waterings.upsert_detected(
            "plant_1",
            {
                "event_start_at": 100.0,
                "occurred_at": 200.0,
                "weight_before_g": 100.0,
                "weight_after_g": 130.0,
                "amount_g": 30.0,
            },
        )

        history = client.get(
            "/api/v2/devices/plant_1/detected-waterings", headers=headers
        )
        removed = client.delete(
            f"/api/v2/devices/plant_1/detected-waterings/{event['id']}",
            headers=headers,
        )
        empty_history = client.get(
            "/api/v2/devices/plant_1/detected-waterings", headers=headers
        )

        assert history.status_code == 200
        assert history.json()["waterings"][0]["amount_g"] == 30.0
        assert removed.json() == {"id": event["id"], "invalid": True}
        assert empty_history.json()["waterings"] == []


def test_detected_watering_history_is_paginated() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        client, cli = make_client(temp_dir)
        cli.auth.add_user("client", "secret-password")
        cli.registry.add("10.0.0.1", "plant", "plant_1")
        token = client.post(
            "/api/v2/auth/login",
            json={"username": "client", "password": "secret-password"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        for index in range(3):
            cli.plant_waterings.upsert_detected(
                "plant_1",
                {
                    "event_start_at": 100.0 + index,
                    "occurred_at": 200.0 + index,
                    "weight_before_g": 100.0,
                    "weight_after_g": 130.0,
                    "amount_g": 30.0,
                },
            )

        first = client.get(
            "/api/v2/devices/plant_1/detected-waterings?limit=2",
            headers=headers,
        ).json()
        second = client.get(
            f"/api/v2/devices/plant_1/detected-waterings"
            f"?limit=2&offset={first['next_offset']}",
            headers=headers,
        ).json()

        assert [item["occurred_at"] for item in first["waterings"]] == [202.0, 201.0]
        assert first["next_offset"] == 2
        assert [item["occurred_at"] for item in second["waterings"]] == [200.0]
        assert second["next_offset"] is None


def test_detected_watering_can_be_marked_fertilized() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        client, cli = make_client(temp_dir)
        cli.auth.add_user("client", "secret-password")
        cli.registry.add("10.0.0.1", "plant", "plant_1")
        token = client.post(
            "/api/v2/auth/login",
            json={"username": "client", "password": "secret-password"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        event, _created = cli.plant_waterings.upsert_detected(
            "plant_1",
            {
                "event_start_at": 100.0,
                "occurred_at": 200.0,
                "weight_before_g": 100.0,
                "weight_after_g": 130.0,
                "amount_g": 30.0,
            },
        )

        before = client.get(
            "/api/v2/devices/plant_1/detected-waterings", headers=headers
        ).json()["waterings"][0]
        updated = client.put(
            f"/api/v2/devices/plant_1/detected-waterings/{event['id']}/fertilized",
            json={"fertilized": True},
            headers=headers,
        )
        after = client.get(
            "/api/v2/devices/plant_1/detected-waterings", headers=headers
        ).json()["waterings"][0]
        cleared = client.put(
            f"/api/v2/devices/plant_1/detected-waterings/{event['id']}/fertilized",
            json={"fertilized": False},
            headers=headers,
        )

        assert before["fertilized"] is False
        assert updated.status_code == 200
        assert updated.json() == {"id": event["id"], "fertilized": True}
        assert after["fertilized"] is True
        assert cleared.json() == {"id": event["id"], "fertilized": False}


def test_fastapi_operations_filter_by_device() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        client, cli = make_client(temp_dir)
        cli.auth.add_user("client", "secret-password")
        token = client.post(
            "/api/v2/auth/login",
            json={"username": "client", "password": "secret-password"},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        first_id = cli.operations.create("plant_1", "device_status", {})
        cli.operations.create("plant_2", "device_status", {})

        response = client.get("/api/v2/devices/plant_1/operations", headers=headers)

        assert response.status_code == 200
        assert response.json()["operations"] == [
            {
                "operation_id": first_id,
                "device": "plant_1",
                "type": "device_status",
                "status": "queued",
                "updated_at": response.json()["operations"][0]["updated_at"],
                "finished_at": None,
            }
        ]
