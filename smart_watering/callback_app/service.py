import json
from dataclasses import dataclass

from smart_watering.domain import OP_ERROR, OperationLog

from .utils import normalize_operation_status


def format_log_value(value: str) -> str:
    if value and all(not char.isspace() for char in value):
        return value
    return json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class CallbackResult:
    operation_id: str
    device: str
    operation_type: str
    status: str
    detail: str

    @property
    def log_detail(self) -> str:
        return " ".join(
            (
                f"operation_id={self.operation_id}",
                f"device={self.device}",
                f"operation_type={self.operation_type}",
                f"status={self.status}",
                f"detail={format_log_value(self.detail)}",
            )
        )


class CallbackService:
    def __init__(self, operations: OperationLog) -> None:
        self.operations = operations

    def record(self, payload: dict) -> CallbackResult:
        operation_id = str(payload.get("operation_id", ""))
        if not operation_id:
            raise ValueError("missing_operation_id")
        status = normalize_operation_status(str(payload.get("status", OP_ERROR)))
        detail = str(payload.get("detail", "callback"))
        operation = self.operations.get(operation_id)
        self.operations.event(operation_id, status, detail)
        return CallbackResult(
            operation_id=operation_id,
            device=operation["device_name"] if operation else "unknown",
            operation_type=operation["operation_type"] if operation else "unknown",
            status=status,
            detail=detail,
        )
