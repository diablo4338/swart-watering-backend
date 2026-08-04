from smart_watering.application.service import SmartWateringService

from .config import ApiSettings
from .main import create_app
from .runtime import ApiRuntime


runtime = ApiRuntime(SmartWateringService(), ApiSettings.from_env())
app = create_app(runtime)
