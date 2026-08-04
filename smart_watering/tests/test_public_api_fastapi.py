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
    )
    return TestClient(create_app(ApiRuntime(cli, settings))), cli


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
