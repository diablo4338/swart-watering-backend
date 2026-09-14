from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from smart_watering.public_api_app.service import DeviceStateProjectionService
from smart_watering.public_api_app.consumption_average import adaptive_weight_change_per_hour
from smart_watering.public_api_app.statistics import water_consumption_periods


@pytest.mark.parametrize("gap_end", [3661, 7200, 36000])
@pytest.mark.parametrize("resumed_weight", [998, 900, 1100])
def test_gap_changes_neither_observed_consumption_nor_duration(gap_end, resumed_weight):
    samples = [
        (0, 1000), (60, 999.75),
        (gap_end, resumed_weight), (gap_end + 60, resumed_weight - 0.25),
    ]
    # Two observed minutes lose 0.5 g; the intervening gap is unknown.
    assert adaptive_weight_change_per_hour(samples) == pytest.approx(-15)


@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf")])
def test_invalid_weight_is_skipped_when_valid_points_are_within_an_hour(invalid):
    samples = [(0, 1000), (60, 999.75), (120, invalid), (180, 999.25), (240, 999)]
    assert adaptive_weight_change_per_hour(samples) == pytest.approx(-15)


@pytest.mark.parametrize("samples", [[], [(0, 1000)], [(0, 1000), (3601, 999)]])
def test_no_observed_interval_is_missing_data(samples):
    assert adaptive_weight_change_per_hour(samples) is None


def test_observed_constant_weight_is_zero_consumption():
    assert adaptive_weight_change_per_hour([(0, 1000), (60, 1000)]) == 0


@pytest.mark.parametrize("samples, expected", [
    ([(0, 1000), (60, 999.75), (7200, 900), (7260, 899.75)], -15),
    ([(0, 1000), (3601, 900)], None),
    ([(0, 1000), (3600, 985)], -15),
])
def test_projection_returns_gap_adjusted_rate_or_null(samples, expected):
    device = SimpleNamespace(id="plant-id", name="Plant", device_type="plant", base_url="http://192.0.2.1")
    business = SimpleNamespace(registry=SimpleNamespace(get_by_id=lambda device_id: device))
    service = DeviceStateProjectionService(business, "http://prometheus.invalid", ZoneInfo("UTC"))
    service.prometheus.range_samples = lambda _query, _start, end: [
        (start.timestamp() + offset, weight)
        for _, _, start, _ in water_consumption_periods(end)
        for offset, weight in samples if start.timestamp() + offset <= end.timestamp()
    ]

    result = service.project_water_consumption(device.id)

    # Yesterday's day and night are complete regardless of the current time.
    assert result["days"][1]["day"] == expected
    assert result["days"][1]["night"] == expected
    assert result["latest_full_period_rate_g_per_hour"] == expected


@pytest.mark.parametrize("gap_seconds", [120, 1800, 3599, 3600])
def test_short_gap_includes_elapsed_time_and_weight_loss(gap_seconds):
    samples = [(0, 1000), (gap_seconds, 1000 - 15 * gap_seconds / 3600)]
    assert adaptive_weight_change_per_hour(samples) == pytest.approx(-15)


def test_short_gap_and_long_gap_in_same_period():
    # Half an hour with 6 g lost, then a long gap, then an hour with 18 g lost.
    samples = [(0, 1000), (1800, 994), (7200, 900), (10800, 882)]
    assert adaptive_weight_change_per_hour(samples) == pytest.approx(-24 / 1.5)
