from smart_watering.application.service import SmartWateringService

from .config import ApiSettings
from .service import DeviceStateProjectionService
from .card_service import DeviceCardService
from .presence import DevicePresenceMonitor, DevicePresenceRegistry


class ApiRuntime:
    def __init__(self, business: SmartWateringService, settings: ApiSettings) -> None:
        self.business = business
        self.settings = settings
        self.presence = DevicePresenceRegistry()
        self.presence_monitor = DevicePresenceMonitor(
            business.registry, self.presence
        )
        self.device_state = DeviceStateProjectionService(
            business,
            settings.prometheus_url,
            settings.statistics_timezone,
            settings.consumption_drop_threshold_percent,
            settings.consumption_median_days,
            self.presence,
        )
        self.cards = DeviceCardService(self)
