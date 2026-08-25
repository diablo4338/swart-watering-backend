#!/usr/bin/env python3
import argparse
import os
import sys
import time

from smart_watering.application.service import SmartWateringService
from smart_watering.domain import SmartWateringError


SNAPSHOT_INTERVAL_SEC_ENV = "SMART_WATERING_SNAPSHOT_INTERVAL_SEC"
DEFAULT_SNAPSHOT_INTERVAL_SEC = 60


def parse_positive_int(raw_value: str, name: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise SmartWateringError(f"{name} must be an integer") from exc
    if value <= 0:
        raise SmartWateringError(f"{name} must be > 0")
    return value


def resolve_snapshot_interval_sec() -> int:
    raw_value = os.environ.get(SNAPSHOT_INTERVAL_SEC_ENV)
    if raw_value is None or raw_value.strip() == "":
        return DEFAULT_SNAPSHOT_INTERVAL_SEC
    return parse_positive_int(raw_value, SNAPSHOT_INTERVAL_SEC_ENV)


class StatusSnapshotScheduler:
    def __init__(self, app: SmartWateringService, interval_sec: int) -> None:
        if interval_sec <= 0:
            raise SmartWateringError("snapshot interval must be > 0")
        self.app = app
        self.interval_sec = interval_sec
        self._was_idle = False

    @staticmethod
    def log(message: str) -> None:
        print(f"snapshotter: {message}", flush=True)

    def enqueue_once(self) -> int:
        devices = self.app.registry.list()
        if not devices:
            if not self._was_idle:
                self.log("no registered devices")
                self._was_idle = True
            return 0

        self._was_idle = False
        queued = 0
        for device in devices:
            existing = self.app.queue.find_duplicate(
                device.base_url, "/watering", "GET", None
            )
            if existing is not None:
                self.log(
                    f"snapshot already pending device={device.name} operation_id={existing}"
                )
                continue
            operation_id = self.app.queue_device_status(device.name)
            queued += 1
            self.log(f"queued device_status device={device.name} operation_id={operation_id}")
        return queued

    def run_forever(self) -> int:
        self.log(f"started interval={self.interval_sec}s")
        while True:
            self.enqueue_once()
            time.sleep(self.interval_sec)


class SnapshotterApp:
    def __init__(self, db_path: str | None = None) -> None:
        self.app = SmartWateringService(db_path, reuse_connections=True)

    @staticmethod
    def build_parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description="Queue periodic device status snapshots")
        parser.add_argument(
            "--interval-sec",
            type=int,
            default=resolve_snapshot_interval_sec(),
            help=f"Snapshot enqueue interval, defaults to ${SNAPSHOT_INTERVAL_SEC_ENV} or {DEFAULT_SNAPSHOT_INTERVAL_SEC}",
        )
        parser.add_argument("--once", action="store_true", help="Queue one snapshot pass and exit")
        return parser

    def run(self, argv: list[str] | None = None) -> int:
        args = self.build_parser().parse_args(argv)
        scheduler = StatusSnapshotScheduler(self.app, args.interval_sec)
        if args.once:
            scheduler.enqueue_once()
            return 0
        return scheduler.run_forever()


def main() -> int:
    try:
        return SnapshotterApp().run()
    except SmartWateringError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
