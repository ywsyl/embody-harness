from __future__ import annotations

from app.model.models import ErrorCode


class ServiceError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class LandmarkNotFoundError(ServiceError):
    def __init__(self, name: str) -> None:
        super().__init__(ErrorCode.LANDMARK_NOT_FOUND, f"Landmark '{name}' was not found.")


class RosUnavailableError(ServiceError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.ROS_UNAVAILABLE, "ROS2 navigation service is unavailable.")

