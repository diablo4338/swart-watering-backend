from smart_watering.domain import SmartWateringError


class PublicApiError(SmartWateringError):
    def __init__(self, message: str, status_code: int = 400, code: str = "bad_request") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
