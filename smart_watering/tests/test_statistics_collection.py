import json
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from smart_watering.application.service import SmartWateringService
from smart_watering.jobs.worker import StatisticsCollectionScheduler
from smart_watering.domain import BackgroundWorker, WorkerState
from smart_watering.public_api_app.card_service import DeviceCardService
from smart_watering.public_api_app.errors import PublicApiError
from smart_watering.public_api_app.statistics import PrometheusClient


@pytest.fixture
def setup(tmp_path):
    app = SmartWateringService(str(tmp_path / "test.db"))
    device = app.registry.add("192.0.2.1", "plant", "Fern")
    api = Mock()
    worker = BackgroundWorker(api, app.queue, app.operations, WorkerState(str(tmp_path / "worker.pid")), 1, 120)
    state = SimpleNamespace(project_watering_history=lambda *_: {"waterings": [], "next_offset": None})
    cards = DeviceCardService(SimpleNamespace(business=app, device_state=state))
    return app, device, worker, cards


def test_history_refresh_uses_ordinary_operation_queue(setup):
    app, device, _, _ = setup
    ids = [app.queue_statistics_collection(device.id) for _ in range(2)]
    assert len(set(ids)) == 2
    app.registry.rename_backend(device.id, "Renamed fern")
    assert app.queue_statistics_collection(device.id) not in ids
    other = app.registry.add("192.0.2.1", "plant", "Other fern")
    assert app.queue_statistics_collection(other.id) != ids[0]
    payload = json.loads(app.operations.get(ids[0])["payload_json"])
    assert payload["device_id"] == device.id
    assert "callback_url" not in payload
    assert [command.operation_id for command in app.queue.list()][:2] == ids
    assert any(event["event_type"] == "command.queued" for event in app.operations.events(ids[0]))
    assert (datetime.fromisoformat(payload["end"]) - datetime.fromisoformat(payload["start"])).total_seconds() == 30 * 24 * 3600


def test_worker_collects_persists_and_history_button_stays_stateless(setup):
    app, device, worker, cards = setup
    before = cards.project_block(device.id, "watering_history")["block"]
    control = before["schema"]["controls"][0]
    assert control["control_type"] == "button.v1"
    assert "operation" not in control
    assert "operation_type" not in control
    assert control["request"]["body"] == {"binding": "none"}
    operation_id = app.queue_statistics_collection(device.id)
    command = app.queue.peek(device.id)
    assert command.method == "POST"
    assert app.operations.get(operation_id)["operation_type"] == "statistics_collection"
    with patch.object(app.operations, "latest_for_device", side_effect=AssertionError("history must not read operations")):
        assert cards.project_block(device.id, "watering_history")["block"]["schema"]["controls"][0] == control
    # A restarted worker finds the same durable command and resumes it.
    app.queue.mark_started(command.id)
    app.operations.event(operation_id, "running", "interrupted worker")
    assert app.operations.timeout_stale_controller_results(-1) == 0
    now = time.time() - 60
    with patch.object(PrometheusClient, "range_samples", return_value=[(now - 120, 100), (now, 150)]):
        worker.run_until_empty(device.id)
    worker.api.request_json.assert_not_called()
    detail = app.operations.detail(operation_id)
    assert detail["status"] == "success"
    assert detail["result"]["device_id"] == device.id
    assert detail["result"]["created"] == 1
    assert app.queue.peek(device.id) is None
    block = cards.project_block(device.id, "watering_history")["block"]
    assert block["schema"]["controls"][0] == control
    assert block["refresh"]["mode"] == "on_open"
    recreated = DeviceCardService(cards.runtime)
    assert recreated.project_block(device.id, "watering_history")["block"]["schema"]["controls"][0] == control
    assert app.queue_statistics_collection(device.id) != operation_id


def test_failure_is_terminal_and_exposes_reason(setup):
    app, device, worker, cards = setup
    operation_id = app.queue_statistics_collection(device.id)
    with patch.object(PrometheusClient, "range_samples", side_effect=RuntimeError("Prometheus unavailable")):
        worker.run_until_empty(device.id)
    state = app.operations.detail(operation_id)
    assert state["status"] == "error"
    assert state["error"]["detail"] == "Prometheus unavailable"
    assert app.queue.peek(device.id) is None
    assert app.queue_statistics_collection(device.id) != operation_id


def test_statistics_dispatch_uses_operation_type_not_transport_fields(setup):
    app, device, worker, _ = setup
    operation_id = app.operations.create(device.id, "statistics_collection", {})
    payload = {
        "device_id": device.id,
        "start": "2026-08-01T00:00:00+00:00",
        "end": "2026-08-02T00:00:00+00:00",
    }
    app.queue.enqueue(operation_id, device.id, device.base_url, "/watering", "GET", payload, "history refresh")
    with patch.object(PrometheusClient, "range_samples", return_value=[]) as query:
        worker.run_until_empty(device.id)
    query.assert_called()
    worker.api.request_json.assert_not_called()
    assert app.operations.get(operation_id)["status"] == "success"


@pytest.mark.parametrize("status", ["cancelled", "success", "timeout", "error"])
def test_terminal_command_left_in_queue_is_not_executed_again(setup, status):
    app, device, worker, _ = setup
    operation_id = app.queue_statistics_collection(device.id)
    app.operations.event(operation_id, status, "already finished")
    with patch.object(PrometheusClient, "range_samples") as query:
        worker.run_until_empty(device.id)
    query.assert_not_called()
    assert app.operations.get(operation_id)["status"] == status
    assert app.queue.peek(device.id) is None


def test_expired_backend_command_times_out_without_query(setup):
    app, device, worker, _ = setup
    operation_id = app.queue_statistics_collection(device.id)
    worker.max_wait_sec = -1
    with patch.object(PrometheusClient, "range_samples") as query:
        worker.run_until_empty(device.id)
    query.assert_not_called()
    assert app.operations.get(operation_id)["status"] == "timeout"


def test_action_uses_existing_surface_and_rejects_extra_fields(setup):
    app, device, _, cards = setup
    with patch.object(cards, "project_card", return_value={"device_id": device.id}):
        assert cards.execute_action(device.id, "collect-statistics", {})["accepted"]
        assert cards.execute_action(device.id, "collect-statistics", {})["accepted"]
        assert len(app.operations.list_non_terminal(device.id)) == 2
        with pytest.raises(PublicApiError):
            cards.execute_action(device.id, "collect-statistics", {"start": "anything"})
        tank = app.registry.register_discovered("192.0.2.2", "tank", "Tank")
        with pytest.raises(PublicApiError):
            cards.execute_action(tank.id, "collect-statistics", {})


def test_scheduler_enqueues_only_plants_at_configured_interval(setup):
    app, device, _, _ = setup
    tank = app.registry.register_discovered("192.0.2.2", "tank", "Tank")
    scheduler = StatisticsCollectionScheduler(app, interval_sec=3600, lookback_hours=3)
    with patch("smart_watering.jobs.worker.time.monotonic", return_value=100):
        scheduler.enqueue_due()
    first = app.queue.peek(device.id)
    assert first is not None
    assert app.queue.peek(tank.id) is None
    assert (datetime.fromisoformat(first.payload["end"]) - datetime.fromisoformat(first.payload["start"])).total_seconds() == 3 * 3600
    app.operations.event(first.operation_id, "success", "finished")
    app.queue.pop(first.id)
    with patch("smart_watering.jobs.worker.time.monotonic", return_value=3699):
        scheduler.enqueue_due()
    assert app.queue.peek(device.id) is None
    with patch("smart_watering.jobs.worker.time.monotonic", return_value=3700):
        scheduler.enqueue_due()
    assert app.queue.peek(device.id).operation_id != first.operation_id


def test_scheduled_scans_join_queue_after_manual_collection(setup):
    app, device, worker, _ = setup
    manual_id = app.queue_statistics_collection(device.id)
    for _ in range(2):
        StatisticsCollectionScheduler(app).enqueue_due()
    assert len(app.operations.list_non_terminal(device.id)) == 3
    assert app.queue.peek(device.id).operation_id == manual_id
    worker.api.request_json.assert_not_called()


def test_manual_month_scan_is_not_swallowed_by_active_automatic_scan(setup):
    app, device, _, _ = setup
    automatic_id = app.queue_statistics_collection(device.id, lookback_hours=3)
    manual_id = app.queue_statistics_collection(device.id)
    assert manual_id != automatic_id
    assert app.queue.peek(device.id).operation_id == automatic_id
    assert len(app.operations.list_non_terminal(device.id)) == 2
    queued = [command for command in app.queue.list() if command.operation_id == manual_id][0]
    assert (datetime.fromisoformat(queued.payload["end"]) - datetime.fromisoformat(queued.payload["start"])).days == 30
