#!/usr/bin/env python3
import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from smart_watering.application.service import SmartWateringService
from smart_watering.application.watering_detection import PlantWateringDetector
from smart_watering.domain import SmartWateringError, parse_positive_int
from smart_watering.public_api_app.config import (
    DEFAULT_PROMETHEUS_URL,
    PROMETHEUS_URL_ENV,
)


DETECTOR_INTERVAL_SEC_ENV = "SMART_WATERING_DETECTOR_INTERVAL_SEC"
DETECTOR_LOOKBACK_HOURS_ENV = "SMART_WATERING_DETECTOR_LOOKBACK_HOURS"
DEFAULT_DETECTOR_INTERVAL_SEC = 3600
DEFAULT_DETECTOR_LOOKBACK_HOURS = 3


def resolve_positive_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or not raw.strip() else parse_positive_int(raw, name)


class WateringDetectionScheduler:
    def __init__(
        self,
        app: SmartWateringService,
        prometheus_url: str,
        interval_sec: int,
        lookback_hours: int,
    ) -> None:
        self.detector = PlantWateringDetector(app, prometheus_url)
        self.interval_sec = interval_sec
        self.lookback_hours = lookback_hours

    @staticmethod
    def log(message: str) -> None:
        print(f"watering-detector: {message}", flush=True)

    def scan_once(self) -> int:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=self.lookback_hours)
        results = self.detector.scan_all(start, end)
        for result in results:
            self.log(
                f"device={result.device} points={result.scanned_points} "
                f"detected={result.detected} created={result.created} "
                f"existing={result.existing}"
            )
        return sum(result.created for result in results)

    def run_forever(self) -> int:
        self.log(
            f"started interval={self.interval_sec}s "
            f"lookback={self.lookback_hours}h"
        )
        while True:
            self.scan_once()
            time.sleep(self.interval_sec)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect plant watering from Prometheus")
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--interval-sec",
        type=int,
        default=resolve_positive_env(
            DETECTOR_INTERVAL_SEC_ENV, DEFAULT_DETECTOR_INTERVAL_SEC
        ),
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=resolve_positive_env(
            DETECTOR_LOOKBACK_HOURS_ENV, DEFAULT_DETECTOR_LOOKBACK_HOURS
        ),
    )
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        scheduler = WateringDetectionScheduler(
            SmartWateringService(reuse_connections=True),
            os.environ.get(PROMETHEUS_URL_ENV, DEFAULT_PROMETHEUS_URL),
            args.interval_sec,
            args.lookback_hours,
        )
        if args.once:
            scheduler.scan_once()
            return 0
        return scheduler.run_forever()
    except SmartWateringError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
