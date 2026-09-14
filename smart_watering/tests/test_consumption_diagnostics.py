from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from smart_watering.public_api_app import service as service_module
from smart_watering.public_api_app.card_service import DeviceCardService
from smart_watering.public_api_app.service import DeviceStateProjectionService
from smart_watering.public_api_app.consumption_average import calculate_average_consumption
from smart_watering.public_api_app.consumption_diagnostics import consumption_diagnostics
from smart_watering.public_api_app.consumption_result import AverageConsumptionResult


def report(samples, start=0, end=7200):
    return consumption_diagnostics(
        samples, calculate_average_consumption(samples),
        datetime.fromtimestamp(start, timezone.utc),
        datetime.fromtimestamp(end, timezone.utc),
    )


def test_evaluator_uses_supplied_rate_without_recomputing_or_mutating_it(monkeypatch):
    from copy import deepcopy
    from smart_watering.public_api_app.consumption_result import ConsumptionInterval

    def forbidden(*_args):
        raise AssertionError("evaluation must not run the average algorithm")

    monkeypatch.setattr(service_module.consumption_average, "calculate_average_consumption", forbidden)
    result = AverageConsumptionResult(
        counted_seconds=3600, change_g=-15, rate=-7.5,
        intervals=[ConsumptionInterval(0, 3600, True, "new_algorithm", -15, "New estimator")],
    )
    original = deepcopy(result)
    detail = consumption_diagnostics(
        [(0, 1000), (3600, 985)], result,
        datetime.fromtimestamp(0, timezone.utc), datetime.fromtimestamp(3600, timezone.utc),
    )
    assert detail["average_rate_g_per_hour"] == 7.5
    assert detail["endpoint_rate_g_per_hour"] == 15
    assert detail["agreement_percent"] == 200
    assert detail["intervals"][0]["reason_label"] == "New estimator"
    assert result == original


def test_concurrent_views_share_one_snapshot_fetch():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    fetching, release, second_started, duplicate = Event(), Event(), Event(), Event()

    def fetch(device_id):
        assert device_id == "plant-id"
        if fetching.is_set():
            duplicate.set()
        fetching.set()
        assert release.wait(5)
        return {"days": [], "snapshot_at": 123.0}

    cards = DeviceCardService(SimpleNamespace(
        device_state=SimpleNamespace(project_water_consumption=fetch),
    ))

    def second_request():
        second_started.set()
        return cards._project_statistics("plant-id")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cards._project_statistics, "plant-id")
        try:
            assert fetching.wait(5)
            second = pool.submit(second_request)
            assert second_started.wait(5)
            assert not duplicate.wait(0.05)
        finally:
            release.set()
        assert first.result(timeout=5) is second.result(timeout=5)
        assert not duplicate.is_set()


def assert_accounting(result):
    intervals = result["intervals"]
    assert sum(i["end_at"] - i["start_at"] for i in intervals) == pytest.approx(result["total_seconds"])
    assert sum(i["end_at"] - i["start_at"] for i in intervals if i["included"]) == pytest.approx(result["counted_seconds"])
    assert sum(i["consumed_g"] for i in intervals) == pytest.approx(result["consumed_g"] or 0)
    assert all(a["end_at"] == b["start_at"] for a, b in zip(intervals, intervals[1:]))


def test_linear_consumption_has_full_agreement_and_compact_timeline():
    result = report([(minute * 60, 1000 - minute / 4) for minute in range(121)])
    assert result["average_rate_g_per_hour"] == pytest.approx(15)
    assert result["consumed_g"] == pytest.approx(30)
    assert result["endpoint_consumed_rounded_g"] == 30
    assert result["agreement_percent"] == pytest.approx(100)
    assert len(result["intervals"]) == 1
    assert_accounting(result)


def test_diagnostics_expose_gap_and_use_actual_endpoint_time_for_comparison():
    result = report([(600, 1000), (1200, 997.5), (6000, 990), (6600, 987.5)])
    assert result["counted_seconds"] == pytest.approx(1200)
    assert result["sample_span_seconds"] == 6000
    assert result["consumed_g"] == pytest.approx(5)
    assert result["endpoint_consumed_g"] == 12.5
    assert result["agreement_percent"] == pytest.approx(50)
    assert [i["reason"] for i in result["intervals"]] == ["no_data", "consumption", "gap", "consumption", "no_data"]
    assert_accounting(result)


def test_rejected_hour_contributes_neither_grams_nor_time():
    samples = [(minute * 60, 1000 - minute / 4) for minute in range(61)]
    samples += [(minute * 60, 985 - (minute - 60)) for minute in range(61, 121)]
    result = report(samples)
    assert result["counted_seconds"] == pytest.approx(3600)
    assert result["consumed_g"] == pytest.approx(15)
    assert result["agreement_percent"] == pytest.approx(250)
    assert [i["reason"] for i in result["intervals"]] == ["consumption", "rate_limit"]
    assert_accounting(result)


@pytest.mark.parametrize("noise", [-15, 15])
def test_smoothed_excursions_are_visible_without_changing_consumption(noise):
    samples = [(minute * 60, 1000 - minute / 4 + (noise if minute % 10 == 5 else 0)) for minute in range(121)]
    result = report(samples)
    assert result["filtered_samples"] == 12
    assert result["agreement_percent"] == pytest.approx(100)
    assert_accounting(result)


@pytest.mark.parametrize("samples", [[], [(3600, 1000)], [(0, 1000), (7200, 1000)]])
def test_missing_or_unusable_data_has_no_quality_percentage(samples):
    result = report(samples)
    assert result["average_rate_g_per_hour"] is None
    assert result["agreement_percent"] is None
    assert_accounting(result)


def test_zero_consumption_is_not_reported_as_missing_or_perfect_quality():
    result = report([(0, 1000), (3600, 1000), (7200, 1000)])
    assert result["average_rate_g_per_hour"] == 0
    assert result["endpoint_consumed_g"] == 0
    assert result["agreement_percent"] is None
    assert_accounting(result)


def test_abrupt_drop_and_watering_explain_time_only_intervals():
    result = report([(0, 1000), (60, 900), (120, 1000), (180, 999.75)], end=180)
    assert [i["reason"] for i in result["intervals"]] == ["abrupt_drop", "weight_increase", "consumption"]
    assert result["counted_seconds"] == pytest.approx(180)
    assert result["consumed_g"] == pytest.approx(0.25)
    assert_accounting(result)


def test_weight_gain_retains_signed_endpoint_difference():
    samples = [(minute * 60, 1000 - minute / 4 + (100 if minute >= 60 else 0)) for minute in range(121)]
    result = report(samples)
    assert result["endpoint_consumed_g"] == -70
    assert result["agreement_percent"] < 0
    assert_accounting(result)


@pytest.mark.parametrize("median_days", [5, 9])
@pytest.mark.parametrize("estimate_scale", [1, 0.5])
def test_card_projects_all_days_from_one_history_snapshot_and_caps_today(monkeypatch, median_days, estimate_scale):
    from dataclasses import replace

    estimates = []

    def alternate_average(samples):
        result = calculate_average_consumption(samples)
        estimates.append(result)
        return replace(result, rate=result.rate * estimate_scale if result.rate is not None else None)

    monkeypatch.setattr(service_module.consumption_average, "calculate_average_consumption", alternate_average)
    now = datetime(2026, 9, 14, 14, 30, tzinfo=ZoneInfo("Europe/Berlin"))

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(service_module, "datetime", FixedDatetime)
    device = SimpleNamespace(id="plant-id", name="Plant", device_type="plant", base_url="http://192.0.2.1")
    business = SimpleNamespace(registry=SimpleNamespace(get_by_id=lambda device_id: device))
    projection = DeviceStateProjectionService(
        business, "http://prometheus.invalid", now.tzinfo, consumption_median_days=median_days,
    )
    queries = []

    def samples(_selector, start, end):
        queries.append((start, end))
        assert len(queries) == 1, "all values must use one Prometheus response"
        return [(start.timestamp() + minute * 60, 1000 - (start.timestamp() + minute * 60 - now.timestamp()) / 240) for minute in range(int((end - start).total_seconds() / 60) + 1)]

    projection.prometheus.range_samples = samples
    cards = DeviceCardService(SimpleNamespace(business=business, device_state=projection))
    statistics = cards._project_statistics(device.id)[0]
    today, yesterday = statistics["days"][:2]
    assert today["day"] == -15 * estimate_scale
    assert today["night"] is None
    assert today["analysis"]["end_at"] == now.timestamp()
    assert today["analysis"]["total_seconds"] == 6.5 * 3600
    assert today["analysis"]["endpoint_consumed_rounded_g"] == 98
    assert yesterday["analysis"]["total_seconds"] == 24 * 3600
    assert yesterday["analysis"]["average_rate_g_per_hour"] == pytest.approx(15 * estimate_scale)
    assert yesterday["analysis"]["agreement_percent"] == pytest.approx(100 / estimate_scale)
    assert yesterday["day_analysis"]["average_rate_g_per_hour"] == pytest.approx(-yesterday["day"])
    assert yesterday["night_analysis"]["average_rate_g_per_hour"] == pytest.approx(-yesterday["night"])
    assert len(queries) == 1
    assert len(estimates) == 2 * max(7, median_days + 2) - 1
    history_start = now.replace(hour=8, minute=0) - timedelta(days=max(7, median_days + 2) - 1)
    assert queries[0] == (history_start, now)
    assert all(end <= now for _, end in queries)
    assert_accounting(today["analysis"])
    assert_accounting(yesterday["analysis"])
    overview = cards._project_overview_block(device, {}, include_project_statistics=True)
    summary = overview["data"]["statistics"][0]["days"]
    assert set(summary[0]) == {
        "date", "day", "night", "day_below_weekly_median", "night_below_weekly_median",
    }
    assert summary[0]["day"] == today["day"]
    descriptor = cards._consumption_analysis_descriptor(device)
    assert descriptor["data"] == {}
    assert descriptor["refresh"]["mode"] == "on_open"
    detail = cards.project_block(device.id, descriptor["id"])
    assert detail["device_id"] == device.id
    assert detail["block"]["data"]["days"] == statistics["days"]
    assert len(detail["block"]["data"]["days"]) == 7
    assert all("analysis" in row for row in detail["block"]["data"]["days"])
    assert detail["block"]["data"]["snapshot_at"] == now.timestamp()
    assert overview["data"]["statistics"][0]["snapshot_at"] == now.timestamp()
    # Serializing the summary must not strip the shared cached analysis.
    assert "analysis" in detail["block"]["data"]["days"][0]
    assert len(queries) == 1
