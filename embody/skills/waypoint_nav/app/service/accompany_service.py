from __future__ import annotations

from app.model.models import AccompanyStatus, NavigationTask
from app.ros.ros_accompany import ROSAccompany
from app.service.exceptions import RosUnavailableError


class AccompanyService:
    def __init__(self, ros_accompany: ROSAccompany) -> None:
        self._ros_accompany = ros_accompany

    def start(self) -> NavigationTask:
        self._ensure_ros_available()
        return self._ros_accompany.start()

    def stop(self) -> None:
        self._ensure_ros_available()
        self._ros_accompany.stop()

    def get_status(self) -> AccompanyStatus:
        self._ensure_ros_available()
        return AccompanyStatus(status=self._ros_accompany.get_status())

    def _ensure_ros_available(self) -> None:
        if not self._ros_accompany.is_available:
            raise RosUnavailableError()
