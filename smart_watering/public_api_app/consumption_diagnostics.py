"""Independent quality evaluation of a supplied average and raw snapshot."""

from datetime import datetime
from math import isfinite

from .consumption_result import AverageConsumptionResult, ConsumptionInterval


def consumption_diagnostics(
    samples: list[tuple[float, float]], result: AverageConsumptionResult,
    start: datetime, end: datetime,
) -> dict:
    """Evaluate a supplied estimate against raw endpoints, without running the estimator."""
    counted_seconds = result.counted_seconds
    consumed_g = -result.change_g
    rate = -result.rate if result.rate is not None else None
    ordered = sorted(
        sample for sample in samples
        if start.timestamp() <= sample[0] <= end.timestamp()
        and isfinite(sample[0]) and isfinite(sample[1]) and sample[1] > 0
    )
    first = ordered[0] if ordered else None
    last = ordered[-1] if ordered else None
    span_seconds = last[0] - first[0] if first and last else 0.0
    raw_consumed_g = first[1] - last[1] if span_seconds > 0 else None
    raw_rate = raw_consumed_g / (span_seconds / 3600) if raw_consumed_g is not None else None
    agreement = raw_rate / rate * 100 if raw_rate is not None and rate is not None and rate > 0 else None
    intervals: list[ConsumptionInterval] = []

    def append(interval: ConsumptionInterval) -> None:
        if interval.end_at <= interval.start_at:
            return
        if intervals and (
            intervals[-1].end_at == interval.start_at
            and intervals[-1].included == interval.included
            and intervals[-1].reason == interval.reason
            and intervals[-1].reason_label == interval.reason_label
        ):
            intervals[-1].end_at = interval.end_at
            intervals[-1].change_g += interval.change_g
        else:
            intervals.append(ConsumptionInterval(
                interval.start_at, interval.end_at, interval.included,
                interval.reason, interval.change_g, interval.reason_label,
            ))

    cursor = start.timestamp()
    for interval in sorted(
        result.intervals,
        key=lambda interval: interval.start_at,
    ):
        if interval.start_at > cursor:
            append(ConsumptionInterval(cursor, interval.start_at, False, "no_data", reason_label="Excluded: no measurements"))
        append(interval)
        cursor = interval.end_at
    if cursor < end.timestamp():
        append(ConsumptionInterval(cursor, end.timestamp(), False, "no_data", reason_label="Excluded: no measurements"))

    def timestamp_label(timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, start.tzinfo).strftime("%d.%m %H:%M")

    def sample_data(sample: tuple[float, float] | None) -> dict | None:
        return {"at": sample[0], "label": timestamp_label(sample[0]), "weight_g": sample[1]} if sample else None

    return {
        "start_at": start.timestamp(), "end_at": end.timestamp(),
        "timezone": str(start.tzinfo),
        "period_label": f"{timestamp_label(start.timestamp())} – {timestamp_label(end.timestamp())}",
        "counted_seconds": counted_seconds,
        "total_seconds": max(0.0, end.timestamp() - start.timestamp()),
        "sample_span_seconds": span_seconds,
        "consumed_g": consumed_g if counted_seconds > 0 else None,
        "average_rate_g_per_hour": rate,
        "endpoint_consumed_g": raw_consumed_g,
        "endpoint_consumed_rounded_g": round(raw_consumed_g) if raw_consumed_g is not None else None,
        "endpoint_rate_g_per_hour": raw_rate,
        "agreement_percent": agreement,
        "first_sample": sample_data(first), "last_sample": sample_data(last),
        "filtered_samples": result.filtered_samples,
        "intervals": [{
            "start_at": interval.start_at, "end_at": interval.end_at,
            "label": f"{timestamp_label(interval.start_at)} – {timestamp_label(interval.end_at)}",
            "included": interval.included, "reason": interval.reason,
            "reason_label": interval.reason_label,
            "consumed_g": -interval.change_g,
        } for interval in intervals],
    }


