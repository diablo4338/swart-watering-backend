import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .foundation import (
    DEFAULT_NODE_PORT,
    DISCOVERY_DEVICE_PREFIX,
    NODE_URL_ENV,
    NODE_WORKER_IDLE_INTERVAL_SEC,
    OP_ACCEPTED,
    OP_ERROR,
    OP_SENDING,
    OP_SUCCESS,
    OP_TIMEOUT,
    RETRYABLE_COMMANDS,
    WORKER_PID_PATH,
    WORKER_STALE_CHECK_INTERVAL_SEC,
    DeviceHttpError,
    QueuedCommand,
    RetryableDeviceApiError,
    SmartWateringError,
    discovered_device_config,
)
from .repositories import CommandQueue, DeviceRegistry, DeviceSnapshotStore, OperationLog

class WorkerState:
    def __init__(self, pid_path: str = WORKER_PID_PATH) -> None:
        self.pid_path = pid_path

    def ensure_dir(self) -> None:
        os.makedirs(os.path.dirname(self.pid_path), exist_ok=True)

    def read_pid(self) -> int | None:
        try:
            with open(self.pid_path, "r", encoding="utf-8") as handle:
                return int(handle.read().strip())
        except (FileNotFoundError, ValueError):
            return None

    @staticmethod
    def is_pid_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def is_running(self) -> bool:
        pid = self.read_pid()
        return pid is not None and self.is_pid_running(pid)

    def save_pid(self, pid: int) -> None:
        self.ensure_dir()
        with open(self.pid_path, "w", encoding="utf-8") as handle:
            handle.write(f"{pid}\n")

    def clear(self) -> None:
        try:
            os.remove(self.pid_path)
        except FileNotFoundError:
            return


class DeviceApiClient:
    def __init__(self, timeout_sec: int) -> None:
        self.timeout_sec = timeout_sec

    @staticmethod
    def build_url(base_url: str, path: str) -> str:
        return urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))

    def request_text(self, base_url: str, path: str, method: str, payload: dict[str, Any] | None = None) -> str:
        data = None
        headers = {}

        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            self.build_url(base_url, path),
            data=data,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                return response.read().decode("utf-8").strip()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace").strip()
            raise DeviceHttpError(exc.code, path, body) from exc
        except urllib.error.URLError as exc:
            raise RetryableDeviceApiError(f"request failed for {path}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RetryableDeviceApiError(f"request timed out for {path}") from exc
        except UnicodeError as exc:
            raise SmartWateringError(f"invalid URL for {path}: {base_url}") from exc

    def request_json(self, base_url: str, path: str, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = self.request_text(base_url, path, method, payload)
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise SmartWateringError(f"invalid JSON from {path}: {body}") from exc


class BackgroundWorker:
    def __init__(
        self,
        api: DeviceApiClient,
        queue: CommandQueue,
        operations: OperationLog,
        state: WorkerState,
        retry_interval_sec: int,
        max_wait_sec: int,
    ) -> None:
        self.api = api
        self.queue = queue
        self.operations = operations
        self.state = state
        self.retry_interval_sec = retry_interval_sec
        self.max_wait_sec = max_wait_sec
        self._was_idle = False

    @staticmethod
    def log(message: str, device_label: str | None = None) -> None:
        prefix = "worker"
        if device_label is not None:
            prefix = f"{prefix} device={device_label}"
        print(f"{prefix}: {message}", flush=True)

    @staticmethod
    def _is_watering_already_stopped(command: QueuedCommand, exc: RetryableDeviceApiError) -> bool:
        if command.method != "POST" or command.path != "/watering/stop":
            return False
        if isinstance(exc, DeviceHttpError):
            if exc.status_code != 409:
                return False
            try:
                body = json.loads(exc.body)
            except json.JSONDecodeError:
                return "watering_not_active" in exc.body
            return body.get("error") == "watering_not_active"
        return "HTTP 409" in str(exc) and "watering_not_active" in str(exc)

    @staticmethod
    def _device_http_error_detail(exc: DeviceHttpError) -> str:
        if not exc.body:
            return str(exc)
        try:
            body = json.loads(exc.body)
        except json.JSONDecodeError:
            return exc.body
        error = body.get("error")
        return error if isinstance(error, str) and error else exc.body

    @classmethod
    def _is_permanent_watering_start_failure(cls, command: QueuedCommand, exc: RetryableDeviceApiError) -> bool:
        if command.method != "POST" or command.path != "/watering/start":
            return False
        if not isinstance(exc, DeviceHttpError):
            return False
        return 400 <= exc.status_code < 500 or (
            exc.status_code == 500
            and cls._device_http_error_detail(exc)
            in {"watering_start_failed", "invalid_target_g", "watering_already_active", "device_not_tank", "no_memory", "task_create_failed"}
        )

    @staticmethod
    def _should_retry(command: QueuedCommand) -> bool:
        return (
            command.device_name.startswith(DISCOVERY_DEVICE_PREFIX)
            or (command.method, command.path) in RETRYABLE_COMMANDS
        )

    @staticmethod
    def _is_discovery(command: QueuedCommand) -> bool:
        return (
            command.device_name.startswith(DISCOVERY_DEVICE_PREFIX)
            and command.method == "GET"
            and command.path == "/watering"
            and command.payload is None
        )

    def _complete_discovery(self, command: QueuedCommand, response: dict[str, Any]) -> str:
        name, device_type, settings = discovered_device_config(response)
        registry = DeviceRegistry(self.queue.store)
        device = registry.register_discovered(command.base_url, device_type, name)
        if settings:
            registry.confirm_watering_settings(device.id, settings, time.time())
        DeviceSnapshotStore(self.queue.store).save(device.id, response)
        self.operations.update_payload(
            command.operation_id,
            {"base_url": command.base_url, "discovered_name": name, **settings},
        )
        return device.name

    def run(self) -> int:
        self.state.save_pid(os.getpid())
        self.log(f"started pid={os.getpid()} mode=drain-once")
        try:
            return self.run_until_empty()
        finally:
            self.log(f"stopped pid={os.getpid()}")
            self.state.clear()

    def run_forever(self, idle_interval_sec: int = NODE_WORKER_IDLE_INTERVAL_SEC) -> int:
        self.state.save_pid(os.getpid())
        self.log(f"started pid={os.getpid()} mode=forever idle_interval={idle_interval_sec}s")
        try:
            while True:
                self.run_until_empty()
                time.sleep(idle_interval_sec)
        finally:
            self.log(f"stopped pid={os.getpid()}")
            self.state.clear()

    def run_until_empty(self, device_id: str | None = None) -> int:
        active_command_id: int | None = None
        active_started_at = 0.0

        while True:
            command = self.queue.peek(device_id)
            if command is None:
                if not self._was_idle:
                    self.log("queue is empty", device_id)
                    self._was_idle = True
                return 0

            if command.id != active_command_id:
                active_command_id = command.id
                now = time.time()
                active_started_at = command.started_at or now
                self.queue.mark_started(command.id)
                if command.started_at is None:
                    self.operations.event(
                        command.operation_id, OP_SENDING, "worker picked operation",
                        source="worker", event_type="command.picked",
                        data={"queue_id": command.id, "method": command.method, "path": command.path},
                    )
                self._was_idle = False
                self.log(
                    "processing "
                    f"id={command.id} operation_id={command.operation_id} "
                    f"method={command.method} path={command.path} "
                    f"description={command.description!r}",
                    command.device_name,
                )

            if self.operations.is_cancelled(command.operation_id):
                self.log(f"cancelled id={command.id} operation_id={command.operation_id}", command.device_name)
                self.queue.pop(command.id)
                active_command_id = None
                active_started_at = 0.0
                continue

            if command.device_name.startswith(DISCOVERY_DEVICE_PREFIX) and not self._is_discovery(command):
                self.operations.event(
                    command.operation_id,
                    OP_ERROR,
                    "unsafe discovery command rejected",
                    source="worker",
                    event_type="operation.failed",
                )
                self.queue.pop(command.id)
                self.log(
                    f"rejected unsafe discovery id={command.id} "
                    f"method={command.method} path={command.path}",
                    command.device_name,
                )
                active_command_id = None
                active_started_at = 0.0
                continue

            try:
                response = self.api.request_json(command.base_url, command.path, command.method, command.payload)
                if self._is_discovery(command):
                    if self.operations.is_cancelled(command.operation_id):
                        self.queue.pop(command.id)
                        active_command_id = None
                        active_started_at = 0.0
                        continue
                    discovered_name = self._complete_discovery(command, response)
                    self.operations.update_result(command.operation_id, response)
                    self.operations.event(
                        command.operation_id, OP_SUCCESS, f"device discovered: {discovered_name}",
                        source="worker", event_type="operation.succeeded",
                    )
                    self.queue.pop(command.id)
                    self.log(
                        f"discovered id={command.id} operation_id={command.operation_id} "
                        f"name={discovered_name}",
                        command.device_name,
                    )
                    active_command_id = None
                    active_started_at = 0.0
                    continue
                self.operations.event(
                    command.operation_id, OP_ACCEPTED, "device accepted command",
                    source="controller", event_type="command.accepted",
                )
                self.queue.pop(command.id)
                self.log(f"sent id={command.id} operation_id={command.operation_id}", command.device_name)
                active_command_id = None
                active_started_at = 0.0
                continue
            except RetryableDeviceApiError as exc:
                if self._is_watering_already_stopped(command, exc):
                    self.operations.event(command.operation_id, OP_SUCCESS, "no active watering")
                    self.queue.pop(command.id)
                    self.log(
                        f"sent id={command.id} operation_id={command.operation_id} "
                        "result=watering_already_stopped",
                        command.device_name,
                    )
                    active_command_id = None
                    active_started_at = 0.0
                    continue
                if not self._should_retry(command):
                    if isinstance(exc, DeviceHttpError):
                        detail = self._device_http_error_detail(exc)
                        self.log(
                            f"error id={command.id} operation_id={command.operation_id} "
                            f"detail={detail}",
                            command.device_name,
                        )
                        self.operations.event(command.operation_id, OP_ERROR, detail)
                    else:
                        self.log(
                            f"timeout id={command.id} operation_id={command.operation_id} "
                            f"without_retry error={exc}",
                            command.device_name,
                        )
                        self.operations.event(command.operation_id, OP_TIMEOUT, f"device did not respond: {exc}")
                    self.queue.pop(command.id)
                    active_command_id = None
                    active_started_at = 0.0
                    continue
                if self._is_permanent_watering_start_failure(command, exc):
                    detail = self._device_http_error_detail(exc) if isinstance(exc, DeviceHttpError) else str(exc)
                    self.log(
                        f"error id={command.id} operation_id={command.operation_id} "
                        f"detail={detail}",
                        command.device_name,
                    )
                    self.operations.event(command.operation_id, OP_ERROR, detail)
                    self.queue.pop(command.id)
                    active_command_id = None
                    active_started_at = 0.0
                    continue
                if time.time() - active_started_at >= self.max_wait_sec:
                    self.log(
                        f"timeout id={command.id} operation_id={command.operation_id} "
                        f"after={self.max_wait_sec}s error={exc}",
                        command.device_name,
                    )
                    self.operations.event(
                        command.operation_id,
                        OP_TIMEOUT,
                        f"device did not respond within {self.max_wait_sec}s: {exc}",
                    )
                    self.queue.pop(command.id)
                    active_command_id = None
                    active_started_at = 0.0
                    continue
                self.log(
                    f"retry id={command.id} operation_id={command.operation_id} "
                    f"in={self.retry_interval_sec}s error={exc}",
                    command.device_name,
                )
                if self.operations.is_cancelled(command.operation_id):
                    self.log(f"cancelled id={command.id} operation_id={command.operation_id}", command.device_name)
                    self.queue.pop(command.id)
                    active_command_id = None
                    active_started_at = 0.0
                    continue
                new_id = self.queue.move_to_tail_if_other(command, active_started_at, device_id)
                if new_id is not None:
                    self.log(
                        f"retry deferred id={command.id} new_id={new_id} "
                        f"operation_id={command.operation_id} error={exc}",
                        command.device_name,
                    )
                    active_command_id = None
                    active_started_at = 0.0
                    continue
                time.sleep(self.retry_interval_sec)
                continue
            except SmartWateringError as exc:
                self.log(f"error id={command.id} operation_id={command.operation_id} error={exc}", command.device_name)
                self.operations.event(command.operation_id, OP_ERROR, str(exc))
                self.queue.pop(command.id)
                active_command_id = None
                active_started_at = 0.0
                continue


class DeviceWorkerSupervisor:
    def __init__(
        self,
        api: DeviceApiClient,
        queue: CommandQueue,
        operations: OperationLog,
        state: WorkerState,
        retry_interval_sec: int,
        max_wait_sec: int,
    ) -> None:
        self.api = api
        self.queue = queue
        self.operations = operations
        self.state = state
        self.retry_interval_sec = retry_interval_sec
        self.max_wait_sec = max_wait_sec
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._was_idle = False

    def run_forever(self, idle_interval_sec: int = NODE_WORKER_IDLE_INTERVAL_SEC) -> int:
        self.state.save_pid(os.getpid())
        BackgroundWorker.log(
            f"started pid={os.getpid()} mode=per-device idle_interval={idle_interval_sec}s"
        )
        next_stale_check_at = 0.0
        try:
            while True:
                now = time.monotonic()
                check_stale = now >= next_stale_check_at
                self.start_pending_workers(check_stale=check_stale)
                if check_stale:
                    next_stale_check_at = now + WORKER_STALE_CHECK_INTERVAL_SEC
                time.sleep(idle_interval_sec)
        finally:
            BackgroundWorker.log(f"stopped pid={os.getpid()}")
            self.state.clear()

    def start_pending_workers(self, check_stale: bool = True) -> None:
        with self._lock:
            self._threads = {
                device_name: thread
                for device_name, thread in self._threads.items()
                if thread.is_alive()
            }
            if check_stale:
                timed_out = self.operations.timeout_stale_controller_results(self.max_wait_sec)
                if timed_out:
                    BackgroundWorker.log(f"timed out stale controller results count={timed_out}")
            worker_keys = self.queue.pending_worker_keys()
            if not worker_keys:
                if not self._was_idle:
                    BackgroundWorker.log("queue is empty")
                    self._was_idle = True
                return

            self._was_idle = False
            for worker_key in worker_keys:
                if worker_key in self._threads:
                    continue
                thread = threading.Thread(
                    target=self._run_device_worker,
                    args=(worker_key,),
                    name=f"smart-watering-{worker_key}",
                    daemon=True,
                )
                self._threads[worker_key] = thread
                thread.start()

    def wait_for_idle(self) -> None:
        while True:
            with self._lock:
                threads = list(self._threads.values())
            if not threads:
                return
            for thread in threads:
                thread.join()
            with self._lock:
                self._threads = {
                    device_name: thread
                    for device_name, thread in self._threads.items()
                    if thread.is_alive()
                }

    def _run_device_worker(self, worker_key: str) -> None:
        BackgroundWorker.log("device worker started", worker_key)
        try:
            worker = BackgroundWorker(
                self.api,
                self.queue,
                self.operations,
                self.state,
                self.retry_interval_sec,
                self.max_wait_sec,
            )
            worker.run_until_empty(worker_key)
        finally:
            BackgroundWorker.log("device worker stopped", worker_key)


def detect_callback_base_url(port: int = DEFAULT_NODE_PORT) -> str:
    env_value = os.environ.get(NODE_URL_ENV)
    if env_value:
        return env_value.rstrip("/")

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            host = sock.getsockname()[0]
    except OSError:
        host = "127.0.0.1"

    return f"http://{host}:{port}"


def build_callback_url(port: int = DEFAULT_NODE_PORT) -> str:
    return f"{detect_callback_base_url(port).rstrip('/')}/operations/callback"
