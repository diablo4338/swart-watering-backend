import io
import json
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from smart_watering.public_api_app.card_service import DeviceCardService
from smart_watering.public_api_app.errors import PublicApiError
from smart_watering.public_api_app.service import DeviceStateProjectionService
from smart_watering.public_api_app.statistics import PrometheusClient
from smart_watering.tests.test_public_api_fastapi import make_client


def make_service():
    registry = SimpleNamespace(get_by_id=Mock(return_value=SimpleNamespace(
        id="stable-id", base_url="http://10.0.0.1",
    )))
    service = DeviceStateProjectionService(
        SimpleNamespace(registry=registry), "http://prometheus", ZoneInfo("UTC")
    )
    service.prometheus = Mock()
    service.prometheus.weight_at.side_effect = [(100, 1024.5), (200, 1000.0)]
    return service


@pytest.mark.parametrize("weights, expected", [((1024.5, 1000), -24.5), ((1000, 1010), 10), ((0, 0), 0)])
def test_difference_uses_device_id_and_utc_boundaries(weights, expected):
    service = make_service()
    service.prometheus.weight_at.side_effect = [(100, weights[0]), (200, weights[1])]
    result = service.project_weight_difference("stable-id", {
        "start": "2026-01-01T08:00:00+02:00", "end": "2026-01-02T08:00:00+02:00",
    })
    assert result["device_id"] == "stable-id"
    assert result["difference_g"] == expected
    assert result["start_sample_at"] == 100
    assert service.prometheus.weight_at.call_args_list[0].args == (
        "10.0.0.1:80", datetime(2026, 1, 1, 6, tzinfo=timezone.utc)
    )


@pytest.mark.parametrize("period", [
    None, {}, {"start": 1, "end": 2},
    {"start": "2026-01-01T08:00:00", "end": "2026-01-01T09:00:00"},
    {"start": "2026-01-01T08:00:00Z", "end": "2026-01-01T08:00:00Z"},
    {"start": "2026-01-02T08:00:00Z", "end": "2026-01-01T08:00:00Z"},
    {"start": "2026-01-01T08:00:00Z", "end": "2999-01-01T08:00:00Z"},
])
def test_invalid_period_never_queries_prometheus(period):
    service = make_service()
    with pytest.raises(PublicApiError) as error:
        service.project_weight_difference("stable-id", period)
    assert error.value.code == "invalid_weight_period"
    service.prometheus.weight_at.assert_not_called()


@pytest.mark.parametrize("samples", [[None, (200, 1000)], [(100, 1000), None]])
def test_missing_endpoint_is_not_zero(samples):
    service = make_service()
    service.prometheus.weight_at.side_effect = samples
    with pytest.raises(PublicApiError) as error:
        service.project_weight_difference("stable-id", {
            "start": "2026-01-01T08:00:00Z", "end": "2026-01-01T09:00:00Z",
        })
    assert error.value.code == "weight_measurement_missing"


def response_for(series):
    return io.BytesIO(json.dumps({
        "status": "success", "data": {"resultType": "matrix", "result": series},
    }).encode())


def test_prometheus_reads_raw_endpoint_with_escaped_instance_without_rounding():
    at = datetime(2026, 1, 1, 8, 0, 45, tzinfo=timezone.utc)
    timestamp = at.timestamp()
    with patch("urllib.request.urlopen", return_value=response_for([{
        "values": [[timestamp - 299, "1200"], [timestamp - 4, "1199.5"]],
    }])) as request:
        result = PrometheusClient("http://prometheus").weight_at('id"1', at)
    assert result == (timestamp - 4, 1199.5)
    params = parse_qs(urlsplit(request.call_args.args[0].full_url).query)
    assert params["query"] == ['gross_weight_g{instance="id\\"1"}[7201s]']
    assert float(params["time"][0]) == timestamp + 3600
    assert request.call_args.kwargs["timeout"] == 10


@pytest.mark.parametrize("values", [[], [[1767254400, "NaN"]], [[1767254400, "+Inf"]]])
def test_prometheus_missing_or_invalid_measurement(values):
    with patch("urllib.request.urlopen", side_effect=lambda *a, **kw: response_for([{"values": values}])):
        assert PrometheusClient("http://prometheus").weight_at(
            "stable-id", datetime(2026, 1, 1, 8, tzinfo=timezone.utc)
        ) is None


@pytest.mark.parametrize("offsets, expected", [
    ([-900, 600], 600), ([-600, 900], -600), ([-600, 600], -600),
    ([-90000, 100000], -90000), ([0, 900], 0),
])
def test_nearest_measurement_on_either_side_without_age_limit(offsets, expected):
    at = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)
    target = at.timestamp()
    def respond(request, **kwargs):
        params = parse_qs(urlsplit(request.full_url).query)
        end = float(params["time"][0])
        duration = int(params["query"][0].rsplit("[", 1)[1][:-2])
        return response_for([{"values": [
            [target + offset, "1000"] for offset in offsets
            if end - duration < target + offset <= end
        ]}])
    with patch("urllib.request.urlopen", side_effect=respond):
        assert PrometheusClient("http://prometheus").weight_at("stable-id", at) == (target + expected, 1000)


@pytest.mark.parametrize("payload, code", [
    ({"status": "error"}, "prometheus_query_failed"),
    ({"status": "success", "data": {}}, "invalid_prometheus_response"),
    ({"status": "success", "data": {"resultType": "matrix", "result": [{}, {}]}}, "ambiguous_weight_series"),
])
def test_prometheus_errors(payload, code):
    with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
        with pytest.raises(PublicApiError) as error:
            PrometheusClient("http://prometheus").weight_at("stable-id", datetime.now(timezone.utc))
    assert error.value.code == code


def test_advertised_control_only_binds_period_and_returns_result():
    state = make_service()
    runtime = SimpleNamespace(business=state.business, device_state=state)
    cards = DeviceCardService(runtime)
    device = SimpleNamespace(id="stable-id", name="Display", device_type="plant")
    block = cards._project_control_block(device, {})
    assert all(c["id"] != "weight_difference" for c in block["schema"]["controls"])
    control = cards._project_watering_history_descriptor(device)["actions"][0]
    assert control["control_type"] == "date_time_range.v1"
    assert control["request"]["body"] == {"binding": "control_value", "property": "period"}
    cards.project_card = Mock(return_value={"device_id": device.id})
    response = cards.execute_action(device.id, "weight-difference", {"period": {
        "start": "2026-01-01T08:00:00Z", "end": "2026-01-01T09:00:00Z",
    }})
    assert response["accepted"] is True
    assert response["result"]["difference_g"] == -24.5
    with pytest.raises(PublicApiError):
        cards.execute_action(device.id, "weight-difference", {"period": {}, "other": 1})


def test_http_action_returns_result_without_device_commands():
    with tempfile.TemporaryDirectory() as temp_dir:
        client, business = make_client(temp_dir)
        business.auth.add_user("client", "secret-password")
        device = business.registry.add("10.0.0.1", "plant", "Display name")
        token = client.post("/api/v3/auth/login", json={
            "username": "client", "password": "secret-password",
        }).json()["token"]
        href = f"/api/v3/devices/{device.id}/actions/weight-difference"
        payload = {"period": {"start": "2026-01-01T08:00:00Z", "end": "2026-01-01T09:00:00Z"}}
        assert client.post(href, json=payload).status_code == 401
        with (
            patch.object(PrometheusClient, "weight_at", side_effect=[(100, 1000), (200, 980)]) as query,
            patch.object(PrometheusClient, "range_samples", return_value=[]),
        ):
            response = client.post(href, json=payload, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["card"]["device_id"] == device.id
        assert response.json()["result"]["difference_g"] == -20
        assert [call.args[0] for call in query.call_args_list] == ["10.0.0.1:80", "10.0.0.1:80"]
        assert business.operations.list_non_terminal(device.id) == []


def test_prometheus_transport_failure_is_reported():
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timeout")):
        with pytest.raises(PublicApiError) as error:
            PrometheusClient("http://prometheus").weight_at("stable-id", datetime.now(timezone.utc))
    assert error.value.code == "prometheus_unavailable"
