import pytest
import json
from pathlib import Path

from smart_watering.public_api_app.statistics import adaptive_weight_change_per_hour


def test_recorded_prometheus_day_preserves_consumption_after_noise():
    fixture = Path(__file__).parent / "fixtures" / "consumption_2026_09_12.json"
    values = json.loads(fixture.read_text(encoding="utf-8-sig"))["values"]
    samples = [(float(t), float(w)) for t, w in values]
    # 08:00-09:54: baseline falls 6981 -> 6955 (26 g).
    # 14:25-20:00: baseline falls 6892 -> 6832 (60 g).
    # Last reading is 6836: a small rise must not erase prior consumption.
    # The 271-minute missing interval contributes neither its 63 g nor time.
    assert adaptive_weight_change_per_hour(samples) == pytest.approx(-86 / (449 / 60))


def test_noise_reversal_preserves_new_baseline_minimum():
    # Every 5 minutes the baseline loses one gram; intervening positive noise
    # makes the raw downward transition look much larger than the real loss.
    samples = [
        (minute * 60, 1000 - minute // 5 + (4 if minute % 5 == 4 else 0))
        for minute in range(61)
    ]
    assert adaptive_weight_change_per_hour(samples) == pytest.approx(-12)


@pytest.mark.parametrize("rate", [15, 25, 25.001, 60])
def test_hourly_rate_limit(rate):
    samples = [(minute * 60, 1000 - minute * rate / 60) for minute in range(61)]
    result = adaptive_weight_change_per_hour(samples)
    if rate <= 25:
        assert result == pytest.approx(-rate)
    else:
        assert result is None


@pytest.mark.parametrize("rate", [1, 5, 15, 20, 25])
@pytest.mark.parametrize("phase", [0, 0.3, 0.8])
def test_integer_gram_staircase_matches_firmware_metric(rate, phase):
    samples = [(minute * 60, round(1000 + phase - minute * rate / 60)) for minute in range(721)]
    assert adaptive_weight_change_per_hour(samples) == pytest.approx(-rate)


def test_weight_updated_every_twenty_minutes():
    samples = [(minute * 60, 1000 - (minute // 20) * 5) for minute in range(181)]
    assert adaptive_weight_change_per_hour(samples) == pytest.approx(-15)


def test_integer_weights_with_data_gap():
    first = [(minute * 60, 1000 - minute // 4) for minute in range(61)]
    second = [((minute + 180) * 60, 900 - minute // 4) for minute in range(61)]
    assert adaptive_weight_change_per_hour(first + second) == pytest.approx(-15)


def test_bad_hour_does_not_zero_good_hour():
    first = [(minute * 60, 1000 - minute // 4) for minute in range(61)]
    second = [((minute + 60) * 60, 985 - minute) for minute in range(1, 61)]
    assert adaptive_weight_change_per_hour(first + second) == pytest.approx(-15)


def test_exact_limit_is_accepted_over_full_hour():
    samples = [(minute * 60, 1000 - minute * 25 / 60) for minute in range(61)]
    assert adaptive_weight_change_per_hour(samples) == pytest.approx(-25)


def test_glitch_is_rejected_without_clipping_and_normal_consumption_resumes():
    samples = [(0, 1000), (60, 999.75), (120, 900), (180, 899.75)]
    # Only the two valid 0.25 g drops count, over three observed minutes.
    assert adaptive_weight_change_per_hour(samples) == pytest.approx(-10)


def test_glitch_recovery_does_not_create_extra_consumption():
    samples = [(0, 1000), (60, 900), (120, 1000), (180, 999.75)]
    assert adaptive_weight_change_per_hour(samples) == pytest.approx(-5)


def test_gap_does_not_relax_rate_limit_for_following_measurement():
    samples = [(0, 1000), (60, 999.75), (7200, 900), (7260, 899)]
    # The incomplete post-gap window exceeds the rate limit and is excluded.
    assert adaptive_weight_change_per_hour(samples) == pytest.approx(-15)


@pytest.mark.parametrize("rate, expected", [(15, -15), (25, -25), (60, None)])
def test_api_projection_applies_rate_limit(rate, expected):
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    from smart_watering.public_api_app.service import DeviceStateProjectionService

    device = SimpleNamespace(id="plant-id", name="Plant", device_type="plant", base_url="http://192.0.2.1")
    business = SimpleNamespace(registry=SimpleNamespace(get_by_id=lambda device_id: device))
    service = DeviceStateProjectionService(business, "http://prometheus.invalid", ZoneInfo("UTC"))
    service.prometheus.range_samples = lambda *_args: [
        (minute * 60, round(1000 - minute * rate / 60)) for minute in range(61)
    ]

    result = service.project_water_consumption(device.id)

    assert result["days"][1]["day"] == expected
    assert result["days"][1]["night"] == expected
    assert result["latest_full_period_rate_g_per_hour"] == expected
