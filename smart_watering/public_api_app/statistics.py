import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta
from math import isclose, isfinite
from statistics import median

from .errors import PublicApiError


WATER_WEIGHT_METRIC = "gross_weight_g"
WEIGHT_INCREASE_RESET_G = 10.0
MAX_VALID_CONSUMPTION_RATE_G_PER_HOUR = 25.0
WATERING_DETECTION_WINDOW_SEC = 5 * 60
WATER_CONSUMPTION_HISTORY_DAYS = 7
PROMETHEUS_SAMPLE_INTERVAL_SEC = 60
CONSUMPTION_RATE_WINDOW_SEC = 3600
MAX_VALID_CONSUMPTION_GAP_SEC = 3600


def adaptive_weight_change_per_hour(samples: list[tuple[float, float]]) -> float | None:
    """Average consumption, bridging gaps up to one hour and excluding longer ones."""
    ordered = sorted(
        sample for sample in samples
        if isfinite(sample[0]) and isfinite(sample[1]) and sample[1] > 0
    )
    if len(ordered) < 2:
        return None

    elapsed_hours = 0.0
    change = 0.0
    window_hours = 0.0
    window_change = 0.0

    def finish_window() -> None:
        nonlocal elapsed_hours, change, window_hours, window_change
        allowed = MAX_VALID_CONSUMPTION_RATE_G_PER_HOUR * window_hours
        if -window_change <= allowed or isclose(
            -window_change, allowed, rel_tol=1e-9, abs_tol=1e-9
        ):
            elapsed_hours += window_hours
            change += window_change
        window_hours = 0.0
        window_change = 0.0

    baseline_weight = ordered[0][1]
    previous = ordered[0]
    for timestamp, weight in ordered[1:]:
        interval_seconds = timestamp - previous[0]
        if interval_seconds <= 0 or interval_seconds > MAX_VALID_CONSUMPTION_GAP_SEC:
            finish_window()
            baseline_weight = weight
            previous = (timestamp, weight)
            continue
        interval_hours = interval_seconds / 3600.0
        window_hours += interval_hours
        # The MCU exports whole grams. Do not extrapolate each one-gram
        # staircase step into a minute rate; validate the accumulated hour.
        allowed_drop = MAX_VALID_CONSUMPTION_RATE_G_PER_HOUR * max(1.0, interval_hours)
        # Compare with the consumption baseline, not the previous noisy point.
        # Returning from a positive spike must not discard a new weight minimum.
        difference = weight - baseline_weight
        # Subtracting large weights can introduce rounding error at the limit.
        if difference < -allowed_drop and not isclose(
            -difference, allowed_drop, rel_tol=1e-9, abs_tol=1e-9
        ):
            baseline_weight = weight
            previous = (timestamp, weight)
            if window_hours * 3600 >= CONSUMPTION_RATE_WINDOW_SEC - 1e-9:
                finish_window()
            continue

        if difference < 0:
            window_change += difference
            baseline_weight = weight
        elif difference > 0:
            # A real upward baseline shift is watering. Small positive noise
            # does not move the baseline, so its reversal is not counted twice.
            if difference > WEIGHT_INCREASE_RESET_G:
                baseline_weight = weight
        previous = (timestamp, weight)
        if window_hours * 3600 >= CONSUMPTION_RATE_WINDOW_SEC - 1e-9:
            finish_window()

    finish_window()
    return change / elapsed_hours if elapsed_hours > 0 else None


def detect_watering_events(
    samples: list[tuple[float, float]],
    window_sec: int = WATERING_DETECTION_WINDOW_SEC,
    threshold_g: float = WEIGHT_INCREASE_RESET_G,
    max_amount_g: float = 1000.0,
) -> list[dict[str, float]]:
    """Detect watering from each rise and the maximum in its next time window."""
    if window_sec <= 0:
        raise ValueError("window_sec must be > 0")
    if max_amount_g <= threshold_g:
        raise ValueError("max_amount_g must be greater than threshold_g")
    ordered = sorted(sample for sample in samples if sample[1] > 0)
    if len(ordered) < 2:
        return []

    events: list[dict[str, float]] = []
    index = 1
    while index < len(ordered):
        before = ordered[index - 1]
        current = ordered[index]
        if current[1] <= before[1]:
            index += 1
            continue

        window_end = current[0] + window_sec
        window_stop = index + 1
        while (
            window_stop < len(ordered)
            and ordered[window_stop][0] <= window_end
        ):
            window_stop += 1
        peak = max(
            ordered[index:window_stop],
            key=lambda sample: (sample[1], sample[0]),
        )
        amount_g = peak[1] - before[1]
        if amount_g >= threshold_g:
            events.append(
                {
                    "event_start_at": before[0],
                    "occurred_at": peak[0],
                    "weight_before_g": before[1],
                    "weight_after_g": peak[1],
                    "amount_g": amount_g,
                }
            )
        index = window_stop

    return events


def water_consumption_periods(
    now: datetime,
    history_days: int = WATER_CONSUMPTION_HISTORY_DAYS,
) -> list[tuple[date, str, datetime, datetime]]:
    periods = []
    current_period_date = now.date()
    if now.timetz().replace(tzinfo=None) < time(hour=8):
        current_period_date -= timedelta(days=1)
    for days_ago in range(history_days):
        period_date = current_period_date - timedelta(days=days_ago)
        midnight = datetime.combine(period_date, time.min, now.tzinfo)
        periods.extend(
            [
                (period_date, "night", midnight + timedelta(hours=20), midnight + timedelta(days=1, hours=8)),
                (period_date, "day", midnight + timedelta(hours=8), midnight + timedelta(hours=20)),
            ]
        )
    return periods


def consumption_is_below_median(
    value: float,
    previous_values: list[float],
    threshold_percent: int,
) -> bool:
    """Return whether consumption fell by the configured percentage."""
    if not previous_values:
        return False
    baseline = median(abs(previous) for previous in previous_values)
    if baseline <= 0:
        return False
    return abs(value) <= baseline * (1.0 - threshold_percent / 100.0)


def prometheus_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def prometheus_instance(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.port is not None:
        return parsed.netloc
    default_port = 443 if parsed.scheme == "https" else 80
    return f"{parsed.hostname}:{default_port}"


def water_consumption_query_end(start: datetime, end: datetime, now: datetime) -> datetime | None:
    if start > now:
        return None
    return min(end, now)


def water_consumption_elapsed_hours(start: datetime, query_end: datetime) -> float:
    return max((query_end - start).total_seconds() / 3600.0, 0.0)


class PrometheusClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def range_samples(
        self, query: str, start: datetime, end: datetime
    ) -> list[tuple[float, float]]:
        try:
            start_timestamp = int(start.timestamp() // 60) * 60
            end_timestamp = int(end.timestamp() // 60) * 60
            query_string = urllib.parse.urlencode(
                {
                    "query": query,
                    "start": start_timestamp,
                    "end": end_timestamp,
                    "step": PROMETHEUS_SAMPLE_INTERVAL_SEC,
                }
            )
            request = urllib.request.Request(
                f"{self.base_url.rstrip('/')}/api/v1/query_range?{query_string}",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise PublicApiError(
                f"Prometheus request failed: {exc}", 424, "prometheus_unavailable"
            ) from exc
        if payload.get("status") != "success":
            raise PublicApiError("Prometheus range query failed", 424, "prometheus_query_failed")
        results = payload.get("data", {}).get("result", [])
        if not results:
            return []
        try:
            return [
                (float(sample[0]), float(sample[1]))
                for sample in results[0].get("values", [])
            ]
        except (IndexError, TypeError, ValueError) as exc:
            raise PublicApiError(
                "invalid Prometheus response", 424, "invalid_prometheus_response"
            ) from exc
