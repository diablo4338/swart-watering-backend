#!/usr/bin/env python3
import sys

from smart_watering.domain import (
    REQUEST_TIMEOUT_SEC,
    CommandQueue,
    DeviceWorkerSupervisor,
    OperationLog,
    SQLiteStore,
    DeviceApiClient,
    SmartWateringError,
    WorkerState,
    resolve_node_worker_idle_interval_sec,
    resolve_worker_max_wait_sec,
    resolve_worker_retry_interval_sec,
)


def main() -> int:
    try:
        store = SQLiteStore(reuse_connections=True)
        store.init_schema()
        worker = DeviceWorkerSupervisor(
            DeviceApiClient(REQUEST_TIMEOUT_SEC),
            CommandQueue(store),
            OperationLog(store),
            WorkerState(),
            resolve_worker_retry_interval_sec(),
            resolve_worker_max_wait_sec(),
        )
        return worker.run_forever(resolve_node_worker_idle_interval_sec())
    except SmartWateringError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

