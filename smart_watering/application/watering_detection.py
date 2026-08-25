from dataclasses import dataclass
from datetime import datetime, timedelta
import os
from typing import Any

from smart_watering.domain import DeviceType, SmartWateringError, parse_positive_int
from smart_watering.public_api_app.statistics import (
    WATER_WEIGHT_METRIC,
    PrometheusClient,
    detect_watering_events,
    prometheus_instance,
    prometheus_string,
)

from .service import SmartWateringService


@dataclass(frozen=True)
class DetectionResult:
    device: str
    scanned_points: int
    detected: int
    created: int
    existing: int


MAX_DETECTED_WATERING_G_ENV = "SMART_WATERING_MAX_DETECTED_WATERING_G"
DEFAULT_MAX_DETECTED_WATERING_G = 1000.0
DETECTION_WINDOW_MIN_ENV = "SMART_WATERING_DETECTION_WINDOW_MIN"
DEFAULT_DETECTION_WINDOW_MIN = 5
MAX_DEVICE_SAMPLE_INTERVAL_MIN = 50


def resolve_max_detected_watering_g() -> float:
    raw = os.environ.get(MAX_DETECTED_WATERING_G_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_MAX_DETECTED_WATERING_G
    try:
        value = float(raw)
    except ValueError as exc:
        raise SmartWateringError(
            f"{MAX_DETECTED_WATERING_G_ENV} must be a number"
        ) from exc
    if value <= 10:
        raise SmartWateringError(f"{MAX_DETECTED_WATERING_G_ENV} must be > 10")
    return value


def resolve_detection_window_min() -> int:
    raw = os.environ.get(DETECTION_WINDOW_MIN_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_DETECTION_WINDOW_MIN
    return parse_positive_int(raw, DETECTION_WINDOW_MIN_ENV)


class PlantWateringDetector:
    MAX_QUERY_SPAN = timedelta(days=1)

    def __init__(
        self,
        app: SmartWateringService,
        prometheus_url: str,
        max_amount_g: float | None = None,
        window_min: int | None = None,
    ) -> None:
        self.app = app
        self.prometheus = PrometheusClient(prometheus_url)
        self.max_amount_g = (
            resolve_max_detected_watering_g()
            if max_amount_g is None
            else max_amount_g
        )
        self.window_min = (
            resolve_detection_window_min()
            if window_min is None
            else window_min
        )
        if self.window_min <= 0:
            raise SmartWateringError("detection window minutes must be > 0")

    def scan_device(
        self, device_id: str, start: datetime, end: datetime
    ) -> DetectionResult:
        device = self.app.registry.get_by_id(device_id)
        if device.device_type != DeviceType.PLANT:
            return DetectionResult(device.name, 0, 0, 0, 0)
        instance = prometheus_instance(device.base_url)
        selector = (
            f'{WATER_WEIGHT_METRIC}'
            f'{{instance="{prometheus_string(instance)}"}}'
        )
        samples_by_timestamp: dict[float, float] = {}
        # A watering at the very beginning of the requested range still needs
        # the preceding weight as its baseline. Devices may sleep for up to 50
        # minutes, so the detection window alone is not enough history.
        overlap = timedelta(
            minutes=max(self.window_min, MAX_DEVICE_SAMPLE_INTERVAL_MIN)
        )
        chunk_start = start - overlap
        while chunk_start < end:
            chunk_end = min(chunk_start + self.MAX_QUERY_SPAN, end)
            for timestamp, weight in self.prometheus.range_samples(
                selector, chunk_start, chunk_end
            ):
                samples_by_timestamp[timestamp] = weight
            if chunk_end >= end:
                break
            chunk_start = chunk_end - overlap
        samples = sorted(samples_by_timestamp.items())
        self.app.plant_waterings.invalidate_above_amount(
            device.id, self.max_amount_g
        )
        self.app.plant_waterings.invalidate_exact_duplicates(device.id)
        events = detect_watering_events(
            samples,
            window_sec=self.window_min * 60,
            max_amount_g=self.max_amount_g,
        )
        start_timestamp = start.timestamp()
        end_timestamp = end.timestamp()
        events = [
            event
            for event in events
            if start_timestamp <= event["occurred_at"] <= end_timestamp
        ]
        created = 0
        for event in events:
            if event.get("_anomaly_recovery"):
                self.app.plant_waterings.invalidate_events_inside(
                    device.id,
                    event["event_start_at"],
                    event["occurred_at"],
                    event["amount_g"],
                )
            _stored, was_created = self.app.plant_waterings.upsert_detected(
                device.id, event
            )
            created += int(was_created)
        return DetectionResult(
            device.name,
            len(samples),
            len(events),
            created,
            len(events) - created,
        )

    def scan_all(self, start: datetime, end: datetime) -> list[DetectionResult]:
        return [
            self.scan_device(device.id, start, end)
            for device in self.app.registry.list()
            if device.device_type == DeviceType.PLANT
        ]
