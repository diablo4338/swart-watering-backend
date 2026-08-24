import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from threading import Lock
from typing import Any

from smart_watering.domain import DeviceApiClient


@dataclass(frozen=True)
class DevicePresence:
    state: str = "unknown"
    checked_at: float | None = None
    last_online_at: float | None = None
    last_error: str | None = None

    @property
    def online(self) -> bool:
        return self.state == "online"


class DevicePresenceRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._states: dict[str, DevicePresence] = {}

    def get(self, device_name: str) -> DevicePresence:
        with self._lock:
            return self._states.get(device_name, DevicePresence())

    def mark_online(self, device_name: str, checked_at: float | None = None) -> None:
        now = checked_at if checked_at is not None else time.time()
        with self._lock:
            self._states[device_name] = DevicePresence(
                state="online",
                checked_at=now,
                last_online_at=now,
                last_error=None,
            )

    def mark_offline(
        self, device_name: str, error: str, checked_at: float | None = None
    ) -> None:
        now = checked_at if checked_at is not None else time.time()
        with self._lock:
            previous = self._states.get(device_name, DevicePresence())
            self._states[device_name] = DevicePresence(
                state="offline",
                checked_at=now,
                last_online_at=previous.last_online_at,
                last_error=error,
            )


class DevicePresenceMonitor:
    def __init__(
        self,
        device_registry: Any,
        presence: DevicePresenceRegistry,
        interval_sec: float = 5.0,
        timeout_sec: float = 0.2,
        concurrency: int = 8,
    ) -> None:
        self.device_registry = device_registry
        self.presence = presence
        self.interval_sec = interval_sec
        self.api = DeviceApiClient(timeout_sec)
        self._semaphore = asyncio.Semaphore(concurrency)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self.run(), name="device-presence-monitor"
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def run(self) -> None:
        while True:
            started_at = time.monotonic()
            try:
                devices = await asyncio.to_thread(self.device_registry.list)
                await asyncio.gather(*(self._probe_device(device) for device in devices))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"device presence cycle failed: {error}", flush=True)
            elapsed = time.monotonic() - started_at
            await asyncio.sleep(max(0.0, self.interval_sec - elapsed))

    async def _probe_device(self, device: Any) -> None:
        async with self._semaphore:
            try:
                await asyncio.to_thread(
                    self.api.request_text, device.base_url, "/healthz", "GET"
                )
            except Exception as error:
                self.presence.mark_offline(device.name, str(error))
            else:
                self.presence.mark_online(device.name)
