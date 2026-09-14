"""Production average-consumption algorithm; independent of quality evaluation."""

from math import isclose, isfinite

from .consumption_result import AverageConsumptionResult, ConsumptionInterval


WEIGHT_INCREASE_RESET_G = 10.0
MAX_VALID_CONSUMPTION_RATE_G_PER_HOUR = 25.0
MAX_FILTER_SAMPLE_INTERVAL_SEC = 60
CONSUMPTION_RATE_WINDOW_SEC = 3600
MAX_VALID_CONSUMPTION_GAP_SEC = 3600
TRANSIENT_WEIGHT_WINDOW_SEC = 5 * 60
TRANSIENT_WEIGHT_MAX_G = 30.0
TRANSIENT_WEIGHT_RETURN_TOLERANCE_G = 2.0


def _filter_transient_weights(
    ordered: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Interpolate short excursions only after a return to the prior trend.

    A scale can temporarily deviate by +/-15 g (30 g peak to peak).
    Persistent shifts remain available to the watering/drop guards. Never
    interpolate across a missing minute or extrapolate an unconfirmed return.
    """
    filtered = ordered.copy()
    index = 1
    while index < len(ordered) - 1:
        before_t, before_w = filtered[index - 1]
        timestamp, weight = ordered[index]
        difference = abs(weight - before_w)
        if not (
            0 < timestamp - before_t <= MAX_FILTER_SAMPLE_INTERVAL_SEC
            and WEIGHT_INCREASE_RESET_G < difference <= TRANSIENT_WEIGHT_MAX_G
        ):
            index += 1
            continue
        stop = index + 1
        while stop < len(ordered):
            end_t, end_w = ordered[stop]
            if (
                end_t - timestamp > TRANSIENT_WEIGHT_WINDOW_SEC
                or not 0 < end_t - ordered[stop - 1][0] <= MAX_FILTER_SAMPLE_INTERVAL_SEC
            ):
                break
            allowed_loss = MAX_VALID_CONSUMPTION_RATE_G_PER_HOUR * (end_t - before_t) / 3600
            if -allowed_loss - TRANSIENT_WEIGHT_RETURN_TOLERANCE_G <= end_w - before_w <= TRANSIENT_WEIGHT_RETURN_TOLERANCE_G:
                for offset in range(index, stop):
                    sample_t = ordered[offset][0]
                    fraction = (sample_t - before_t) / (end_t - before_t)
                    filtered[offset] = (sample_t, before_w + fraction * (end_w - before_w))
                index = stop
                break
            if abs(end_w - before_w) > TRANSIENT_WEIGHT_MAX_G + allowed_loss:
                break
            stop += 1
        index += 1
    return filtered


def adaptive_weight_change_per_hour(samples: list[tuple[float, float]]) -> float | None:
    return calculate_average_consumption(samples).rate


def _baseline_rise_kind(
    samples: list[tuple[float, float]], index: int, baseline: float,
) -> str:
    """Check a moderate rise against the estimator's actual consumption base."""
    timestamp, weight = samples[index]
    if (
        weight - baseline > TRANSIENT_WEIGHT_MAX_G
        or timestamp - samples[index - 1][0] > MAX_FILTER_SAMPLE_INTERVAL_SEC
    ):
        return "persistent"
    previous_t = timestamp
    for next_t, next_weight in samples[index + 1:]:
        if (
            next_t - timestamp > TRANSIENT_WEIGHT_WINDOW_SEC
            or not 0 < next_t - previous_t <= MAX_FILTER_SAMPLE_INTERVAL_SEC
            or next_weight - baseline > TRANSIENT_WEIGHT_MAX_G
        ):
            return "persistent"
        allowed_loss = MAX_VALID_CONSUMPTION_RATE_G_PER_HOUR * (next_t - timestamp) / 3600
        if -allowed_loss - TRANSIENT_WEIGHT_RETURN_TOLERANCE_G <= next_weight - baseline <= TRANSIENT_WEIGHT_RETURN_TOLERANCE_G:
            return "transient"
        if next_t - timestamp >= TRANSIENT_WEIGHT_WINDOW_SEC:
            return "persistent"
        previous_t = next_t
    return "pending"


def calculate_average_consumption(samples: list[tuple[float, float]]) -> AverageConsumptionResult:
    """Compute the rate and its audit trail in the same pass."""
    ordered = sorted(
        sample for sample in samples
        if isfinite(sample[0]) and isfinite(sample[1]) and sample[1] > 0
    )
    result = AverageConsumptionResult()
    if len(ordered) < 2:
        return result

    filtered = _filter_transient_weights(ordered)
    result.filtered_samples = sum(a[1] != b[1] for a, b in zip(ordered, filtered))
    ordered = filtered

    elapsed_hours = 0.0
    change = 0.0
    window_hours = 0.0
    window_change = 0.0
    window_intervals: list[ConsumptionInterval] = []

    def finish_window() -> None:
        nonlocal elapsed_hours, change, window_hours, window_change
        allowed = MAX_VALID_CONSUMPTION_RATE_G_PER_HOUR * window_hours
        included = -window_change <= allowed or isclose(
            -window_change, allowed, rel_tol=1e-9, abs_tol=1e-9
        )
        if included:
            elapsed_hours += window_hours
            change += window_change
        for interval in window_intervals:
            if not included:
                interval.included = False
                interval.reason = "rate_limit"
                interval.reason_label = f"Excluded: rate above {MAX_VALID_CONSUMPTION_RATE_G_PER_HOUR:g} g/h"
                interval.change_g = 0.0
            result.intervals.append(interval)
        window_intervals.clear()
        window_hours = 0.0
        window_change = 0.0

    baseline_weight = ordered[0][1]
    previous = ordered[0]
    for index, (timestamp, weight) in enumerate(ordered[1:], start=1):
        interval_seconds = timestamp - previous[0]
        if interval_seconds <= 0 or interval_seconds > MAX_VALID_CONSUMPTION_GAP_SEC:
            finish_window()
            if interval_seconds > 0:
                result.intervals.append(ConsumptionInterval(previous[0], timestamp, False, "gap", reason_label="Excluded: data gap over 1 hour"))
            baseline_weight = weight
            previous = (timestamp, weight)
            continue
        difference = weight - baseline_weight
        rise_kind = (
            _baseline_rise_kind(ordered, index, baseline_weight)
            if difference > WEIGHT_INCREASE_RESET_G else None
        )
        if rise_kind == "pending":
            # An unfinished excursion must not increase the base or dilute the
            # rate with unconfirmed time. Re-evaluate it on the next snapshot.
            finish_window()
            result.intervals.append(ConsumptionInterval(
                previous[0], ordered[-1][0], False, "pending_weight_increase",
                reason_label="Excluded: weight increase awaiting confirmation",
            ))
            break
        interval_hours = interval_seconds / 3600.0
        window_hours += interval_hours
        interval = ConsumptionInterval(previous[0], timestamp, True, "consumption")
        window_intervals.append(interval)
        # The MCU exports whole grams. Do not extrapolate each one-gram
        # staircase step into a minute rate; validate the accumulated hour.
        allowed_drop = MAX_VALID_CONSUMPTION_RATE_G_PER_HOUR * max(1.0, interval_hours)
        # Compare with the consumption baseline, not the previous noisy point.
        # Returning from a positive spike must not discard a new weight minimum.
        # Subtracting large weights can introduce rounding error at the limit.
        if difference < -allowed_drop and not isclose(
            -difference, allowed_drop, rel_tol=1e-9, abs_tol=1e-9
        ):
            interval.reason = "abrupt_drop"
            interval.reason_label = "Time counted; abrupt weight loss excluded"
            baseline_weight = weight
            previous = (timestamp, weight)
            if window_hours * 3600 >= CONSUMPTION_RATE_WINDOW_SEC - 1e-9:
                finish_window()
            continue

        if difference < 0:
            window_change += difference
            interval.change_g = difference
            baseline_weight = weight
        elif difference > 0:
            # A real upward baseline shift is watering. Small positive noise
            # does not move the baseline, so its reversal is not counted twice.
            if difference > WEIGHT_INCREASE_RESET_G:
                if rise_kind == "transient":
                    interval.reason = "transient_weight_increase"
                    interval.reason_label = "Time counted; temporary weight increase ignored"
                    result.filtered_samples += 1
                else:
                    interval.reason = "weight_increase"
                    interval.reason_label = "Time counted; weight increase excluded"
                    baseline_weight = weight
        previous = (timestamp, weight)
        if window_hours * 3600 >= CONSUMPTION_RATE_WINDOW_SEC - 1e-9:
            finish_window()

    finish_window()
    result.counted_seconds = elapsed_hours * 3600
    result.change_g = change
    result.rate = change / elapsed_hours if elapsed_hours > 0 else None
    return result


def combine_average_results(results: list[AverageConsumptionResult]) -> AverageConsumptionResult:
    """Weight day/night estimates by the time they represent, including gaps.

    A missing daytime stretch must not give the slower nighttime estimate more
    weight. Represent internal data gaps by the estimate of that same period;
    never use their unknown weight loss as measured consumption. Rejected and
    pending intervals remain excluded, as do periods without a usable estimate.
    """
    usable = [result for result in results if result.rate is not None and result.counted_seconds > 0]
    seconds = sum(result.counted_seconds for result in usable)
    weights = [
        result.counted_seconds + sum(
            interval.end_at - interval.start_at
            for interval in result.intervals
            if not interval.included and interval.reason == "gap"
        )
        for result in usable
    ]
    represented_seconds = sum(weights)
    return AverageConsumptionResult(
        rate=(
            sum(result.rate * weight for result, weight in zip(usable, weights)) / represented_seconds
            if represented_seconds > 0 else None
        ),
        counted_seconds=seconds,
        change_g=sum(result.change_g for result in usable),
        intervals=[interval for result in results for interval in result.intervals],
        filtered_samples=sum(result.filtered_samples for result in results),
    )
