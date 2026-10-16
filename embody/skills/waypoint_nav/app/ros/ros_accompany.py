from __future__ import annotations

from app.model.models import NavigationTask, TaskStatus
from app.ros.ros_node import ROSAdapter, make_navigation_task


class ROSAccompany:
    def __init__(self, adapter: ROSAdapter) -> None:
        self._adapter = adapter

    @property
    def is_available(self) -> bool:
        return self._adapter.is_available

    def start(self) -> NavigationTask:
        task = make_navigation_task("acc")
        self._adapter.publish_accompany_start()
        return task

    def stop(self) -> None:
        self._adapter.publish_accompany_end()

    def get_status(self) -> TaskStatus:
        return self._adapter.get_accompany_status()
