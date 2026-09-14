import pytest

from smart_watering.public_api_app.consumption_average import adaptive_weight_change_per_hour


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
