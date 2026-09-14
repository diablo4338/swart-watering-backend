import pytest

from smart_watering.public_api_app.consumption_average import adaptive_weight_change_per_hour, calculate_average_consumption


@pytest.mark.parametrize("rate", [0, 5, 15, 25])
@pytest.mark.parametrize("noise", [15, -15])
@pytest.mark.parametrize("duration", [1, 3, 5])
def test_temporary_scale_excursions_preserve_linear_consumption(rate, noise, duration):
    samples = [
        (minute * 60, 1000 - minute * rate / 60 + (noise if 1 <= minute % 10 <= duration else 0))
        for minute in range(121)
    ]
    assert adaptive_weight_change_per_hour(samples) == pytest.approx(-rate)


def test_alternating_positive_and_negative_excursions():
    samples = [
        (minute * 60, 1000 - minute / 4 + ({1: 15, 2: -15}.get(minute % 10, 0)))
        for minute in range(121)
    ]
    assert adaptive_weight_change_per_hour(samples) == pytest.approx(-15)


@pytest.mark.parametrize("increase", [15, 100])
def test_persistent_watering_shift_is_preserved(increase):
    samples = [
        (minute * 60, 1000 - minute / 4 + (increase if minute >= 30 else 0))
        for minute in range(121)
    ]
    # The watering interval contributes time but no weight loss.
    assert adaptive_weight_change_per_hour(samples) == pytest.approx(-29.75 / 2)


def test_return_after_missing_data_is_not_interpolated():
    samples = [(0, 1000), (60, 1015), (7260, 900), (7320, 899.75)]
    assert adaptive_weight_change_per_hour(samples) == pytest.approx(-7.5)


@pytest.mark.parametrize("shoulder, spike", [(1, 11), (5, 15), (10, 20)])
def test_return_to_consumption_base_does_not_count_a_second_loss(shoulder, spike):
    samples = [(minute * 60, 1000 + ({1: shoulder, 2: spike}.get(minute, 0))) for minute in range(61)]
    result = calculate_average_consumption(samples)
    assert result.rate == 0
    assert result.change_g == 0
    assert not any(interval.reason == "weight_increase" for interval in result.intervals)


def test_baseline_excursion_across_hour_boundary():
    samples = [(minute * 60, 1000 + ({59: 1, 60: 11}.get(minute, 0))) for minute in range(121)]
    assert adaptive_weight_change_per_hour(samples) == 0


def test_unconfirmed_tail_is_excluded_then_resolved_from_next_snapshot():
    samples = [(0, 1000), (60, 1001), (120, 1011)]
    pending = calculate_average_consumption(samples)
    assert pending.counted_seconds == 60
    assert pending.intervals[-1].reason == "pending_weight_increase"
    assert not pending.intervals[-1].included
    resolved = calculate_average_consumption(samples + [(180, 1000)])
    assert resolved.counted_seconds == 180
    assert resolved.change_g == 0


def test_sustained_rise_is_confirmed_after_five_minutes():
    samples = [(0, 1000), (60, 1001)] + [(minute * 60, 1011) for minute in range(2, 8)]
    result = calculate_average_consumption(samples)
    assert result.counted_seconds == pytest.approx(420)
    assert sum(interval.reason == "weight_increase" for interval in result.intervals) == 1
    assert result.change_g == 0


def test_recorded_september_14_snapshot_has_no_extra_eleven_grams():
    import json
    from datetime import datetime
    from pathlib import Path
    from smart_watering.public_api_app.consumption_diagnostics import consumption_diagnostics

    recorded = json.loads((Path(__file__).parent / "fixtures" / "consumption_2026_09_14.json").read_text())
    start = datetime.fromisoformat(recorded["start"])
    end = start.replace(hour=14, minute=54)
    samples = [tuple(sample) for sample in recorded["samples"] if sample[0] <= end.timestamp()]
    result = calculate_average_consumption(samples)
    assert result.change_g == -78  # Previously -89: the 10:53 return was counted twice.
    assert result.rate == pytest.approx(-78 / 6.9)
    detail = consumption_diagnostics(samples, result, start, end)
    assert detail["endpoint_consumed_g"] == 70
    assert detail["agreement_percent"] == pytest.approx(70 / 78 * 100)
