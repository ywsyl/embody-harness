from __future__ import annotations

from typing import Optional

from app.model.models import NavigationTask, Pose
from app.ros.ros_node import ROSAdapter, make_navigation_task


class ROSLocalization:
    def __init__(self, adapter: ROSAdapter) -> None:
        self._adapter = adapter

    @property
    def is_available(self) -> bool:
        return self._adapter.is_available

    def get_pose(self) -> Optional[Pose]:
        return self._adapter.get_pose()

    def relocalize(self) -> NavigationTask:
        task = make_navigation_task("loc")
        self._adapter.publish_localization_control(
            {
                "action": 2,
                "task_id": task.task_id,
            }
        )
        return task
