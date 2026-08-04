from smart_watering.domain import OperationLog, SQLiteStore

from .main import create_app
from .service import CallbackService


store = SQLiteStore()
store.init_schema()
app = create_app(CallbackService(OperationLog(store)))
