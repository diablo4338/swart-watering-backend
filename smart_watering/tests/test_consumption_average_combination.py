from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest

from smart_watering.public_api_app.consumption_average import (
    calculate_average_consumption,
    combine_average_results,
)
from smart_watering.public_api_app.consumption_diagnostics import consumption_diagnostics


def samples_at_rate(start, minutes, rate, initial=1000):
    return [(start + minute * 60, initial - minute * rate / 60) for minute in range(minutes + 1)]


@pytest.mark.parametrize("missing_period", ["day", "night"])
def test_missing_part_of_day_or_night_does_not_change_their_relative_weight(missing_period):
    day = samples_at_rate(0, 720, 12)
    night = samples_at_rate(43200, 720, 6)
    if missing_period == "day":
        day = [sample for sample in day if not 3600 < sample[0] < 21600]
    else:
        night = [sample for sample in night if not 46800 < sample[0] < 64800]
    results = [calculate_average_consumption(part) for part in (day, night)]
    original = deepcopy(results)

    combined = combine_average_results(results)

    assert combined.rate == pytest.approx(-9)
    assert combined.counted_seconds == pytest.approx(19 * 3600)
    assert combined.change_g == sum(result.change_g for result in results)
    assert results == original
    assert sum(i.end_at - i.start_at for i in combined.intervals if not i.included) == 5 * 3600


def test_active_night_uses_only_its_elapsed_duration():
    day = calculate_average_consumption(samples_at_rate(0, 720, 12))
    night = calculate_average_consumption(samples_at_rate(43200, 120, 6))
    assert combine_average_results([day, night]).rate == pytest.approx(-(12 * 12 + 6 * 2) / 14)


def test_missing_period_does_not_become_zero_consumption():
    day = calculate_average_consumption(samples_at_rate(0, 720, 12))
    missing = calculate_average_consumption([(43200, 1000), (86400, 900)])
    assert combine_average_results([day, missing]).rate == pytest.approx(-12)
    assert combine_average_results([missing]).rate is None


def test_observed_zero_consumption_still_contributes_its_duration():
    day = calculate_average_consumption(samples_at_rate(0, 720, 12))
    night = calculate_average_consumption(samples_at_rate(43200, 720, 0))
    assert combine_average_results([day, night]).rate == pytest.approx(-6)


def test_rejected_hour_is_not_treated_as_a_missing_interval():
    day = calculate_average_consumption(
        samples_at_rate(0, 60, 12) + samples_at_rate(3600, 60, 60, 988)[1:]
    )
    night = calculate_average_consumption(samples_at_rate(7200, 60, 6))
    assert combine_average_results([day, night]).rate == pytest.approx(-9)


def test_pending_rise_is_not_extrapolated():
    day = calculate_average_consumption(samples_at_rate(0, 60, 12) + [(3660, 999)])
    assert day.intervals[-1].reason == "pending_weight_increase"
    night = calculate_average_consumption(samples_at_rate(43200, 60, 6))
    assert combine_average_results([day, night]).rate == pytest.approx(-9)


def test_september_12_real_snapshot_reduces_day_night_sampling_bias():
    recorded = json.loads((Path(__file__).parent / "fixtures" / "consumption_2026_09_12_test7.json").read_text())
    start = datetime.fromisoformat(recorded["start"])
    middle, end = start + timedelta(hours=12), start + timedelta(days=1)
    samples = [tuple(sample) for sample in recorded["samples"]]
    results = [calculate_average_consumption([
        sample for sample in samples if a.timestamp() <= sample[0] <= b.timestamp()
    ]) for a, b in [(start, middle), (middle, end)]]
    combined = combine_average_results(results)

    assert combined.rate == pytest.approx(-8.364286978967664)
    assert combined.change_g == -148
    assert combined.counted_seconds == pytest.approx(69780)
    report = consumption_diagnostics(samples, combined, start, end)
    assert report["endpoint_consumed_g"] == 207
    assert report["agreement_percent"] == pytest.approx(103.54843022348994)
    assert sum(i["end_at"] - i["start_at"] for i in report["intervals"] if i["reason"] == "gap") == 16260
