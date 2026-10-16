from __future__ import annotations

import time

from app.model.models import LocalizationStatus, NavigationTask, Pose
from app.ros.ros_localization import ROSLocalization
from app.service.exceptions import RosUnavailableError


class LocalizationService:
    def __init__(self, ros_localization: ROSLocalization) -> None:
        self._ros_localization = ros_localization

    def get_status(self) -> LocalizationStatus:
        self._ensure_ros_available()
        pose = self._ros_localization.get_pose()
        return LocalizationStatus(localized=pose is not None)

    def get_pose(self, timeout_sec: float = 1.0) -> Pose:
        self._ensure_ros_available()
        pose = self._ros_localization.get_pose()
        if pose is None and timeout_sec > 0:
            deadline = time.time() + timeout_sec
            while pose is None and time.time() < deadline:
                time.sleep(0.05)
                pose = self._ros_localization.get_pose()
        if pose is None:
            raise RosUnavailableError()
        return pose

    def relocalize(self) -> NavigationTask:
        self._ensure_ros_available()
        return self._ros_localization.relocalize()

    def _ensure_ros_available(self) -> None:
        if not self._ros_localization.is_available:
            raise RosUnavailableError()
