import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from smart_watering.domain import SmartWateringError
from smart_watering.public_api_app.presence import (
    DevicePresenceMonitor,
    DevicePresenceRegistry,
)


def test_presence_monitor_updates_runtime_state_from_health_probe() -> None:
    device = SimpleNamespace(name="avocado", base_url="http://10.0.0.1")
    presence = DevicePresenceRegistry()
    assert presence.get("avocado").state == "offline"
    monitor = DevicePresenceMonitor(
        SimpleNamespace(list=lambda: [device]), presence
    )

    async def scenario() -> None:
        with patch.object(monitor.api, "request_text", return_value="ok") as request:
            await monitor._probe_device(device)
        assert presence.get("avocado").online is True
        request.assert_called_once_with("http://10.0.0.1", "/healthz", "GET")

        with patch.object(
            monitor.api,
            "request_text",
            side_effect=SmartWateringError("timeout"),
        ):
            await monitor._probe_device(device)

    asyncio.run(scenario())

    state = presence.get("avocado")
    assert state.online is False
    assert state.last_online_at is not None
    assert state.last_error == "timeout"
