from __future__ import annotations

from typing import Optional

from app.model.models import NavigationStatus, NavigationTask, Pose
from app.ros.ros_navigation import ROSNavigation
from app.service.exceptions import RosUnavailableError
from app.service.landmark_service import LandmarkService


class NavigationService:
    def __init__(self, landmark_service: LandmarkService, ros_navigation: ROSNavigation) -> None:
        self._landmark_service = landmark_service
        self._ros_navigation = ros_navigation

    def navigate_to(self, name: str) -> NavigationTask:
        self._ensure_ros_available()
        landmark = self._landmark_service.get_landmark(name)
        return self._ros_navigation.navigate_to_pose(landmark.pose, target=name)

    def navigate_to_pose(self, pose: Pose, target: Optional[str] = None) -> NavigationTask:
        self._ensure_ros_available()
        return self._ros_navigation.navigate_to_pose(pose, target=target or f"({pose.x}, {pose.y})")

    def cancel(self) -> None:
        self._ensure_ros_available()
        self._ros_navigation.cancel()

    def get_navigation_status(self) -> NavigationStatus:
        self._ensure_ros_available()
        return self._ros_navigation.get_navigation_status()

    def _ensure_ros_available(self) -> None:
        if not self._ros_navigation.is_available:
            raise RosUnavailableError()
