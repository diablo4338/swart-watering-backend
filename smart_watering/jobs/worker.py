#!/usr/bin/env python3
import sys
import os
import time

from smart_watering.application.service import SmartWateringService

from smart_watering.domain import (
    DeviceWorkerSupervisor,
    DeviceType,
    parse_positive_int,
    SmartWateringError,
    WorkerState,
    resolve_node_worker_idle_interval_sec,
    resolve_worker_max_wait_sec,
    resolve_worker_retry_interval_sec,
)


class StatisticsCollectionScheduler:
    """Enqueue periodic scans from the worker loop; never execute them here."""

    def __init__(self, app: SmartWateringService, interval_sec: int = 3600, lookback_hours: int = 3) -> None:
        if interval_sec <= 0 or lookback_hours <= 0:
            raise SmartWateringError("statistics schedule values must be positive")
        self.app = app
        self.interval_sec = interval_sec
        self.lookback_hours = lookback_hours
        self._next_run = 0.0

    def enqueue_due(self) -> None:
        now = time.monotonic()
        if now < self._next_run:
            return
        for device in self.app.registry.list():
            if device.device_type == DeviceType.PLANT:
                self.app.queue_statistics_collection(device.id, lookback_hours=self.lookback_hours)
        self._next_run = now + self.interval_sec


def main() -> int:
    try:
        app = SmartWateringService(reuse_connections=True)
        scheduler = StatisticsCollectionScheduler(
            app,
            parse_positive_int(os.environ.get("SMART_WATERING_DETECTOR_INTERVAL_SEC") or "3600", "SMART_WATERING_DETECTOR_INTERVAL_SEC"),
            parse_positive_int(os.environ.get("SMART_WATERING_DETECTOR_LOOKBACK_HOURS") or "3", "SMART_WATERING_DETECTOR_LOOKBACK_HOURS"),
        )
        worker = DeviceWorkerSupervisor(
            app.api,
            app.queue,
            app.operations,
            WorkerState(),
            resolve_worker_retry_interval_sec(),
            resolve_worker_max_wait_sec(),
        )
        return worker.run_forever(
            resolve_node_worker_idle_interval_sec(), enqueue_scheduled=scheduler.enqueue_due
        )
    except SmartWateringError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

