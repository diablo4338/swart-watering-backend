from smart_watering.application.service import SmartWateringService

from .config import ApiSettings
from .service import PublicApiService


class ApiRuntime:
    def __init__(self, business: SmartWateringService, settings: ApiSettings) -> None:
        self.business = business
        self.settings = settings
        self.service = PublicApiService(
            business,
            settings.prometheus_url,
            settings.statistics_timezone,
            settings.consumption_drop_threshold_percent,
            settings.consumption_median_days,
        )
