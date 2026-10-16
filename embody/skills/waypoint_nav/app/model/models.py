from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Generic, Optional, TypeVar

from pydantic import BaseModel, Field


T = TypeVar("T")


class ErrorCode(int, Enum):
    SUCCESS = 0
    ROBOT_NOT_LOCALIZED = 1001
    LANDMARK_NOT_FOUND = 1002
    NAVIGATION_ALREADY_RUNNING = 1003
    TARGET_UNREACHABLE = 1004
    LOCALIZATION_FAILED = 1005
    ROS_UNAVAILABLE = 1006
    TASK_CANCELLED = 1007
    ACCOMPANY_FAILED = 1008
    VALIDATION_ERROR = 1042
    INTERNAL_ERROR = 1099


class TaskStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ApiResponse(BaseModel, Generic[T]):
    code: int = Field(default=0)
    message: str = Field(default="success")
    data: Optional[T] = None


class Pose(BaseModel):
    x: float
    y: float
    yaw: float = Field(description="Heading in degrees.")


class Landmark(BaseModel):
    name: str
    description: Optional[str] = None
    pose: Pose


class LandmarkSummary(BaseModel):
    name: str
    description: Optional[str] = None


class RelocalizeRequest(BaseModel):
    pass


class NavigationGoRequest(BaseModel):
    destination: str


class NavigationGoPoseRequest(BaseModel):
    x: float
    y: float
    yaw: float = 0.0


class NavigationTask(BaseModel):
    task_id: str


class NavigationStatus(BaseModel):
    status: TaskStatus
    destination: Optional[str] = None


class LocalizationStatus(BaseModel):
    localized: bool


class AccompanyStatus(BaseModel):
    status: TaskStatus


class TaskStatusMessage(BaseModel):
    task_id: Optional[str] = None
    task_type: Optional[str] = None
    status: TaskStatus = TaskStatus.IDLE
    target: Optional[str] = None
    remaining_distance: Optional[float] = None
    message: Optional[str] = None
    raw: Dict[str, Any] = Field(default_factory=dict)
