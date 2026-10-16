from __future__ import annotations

from typing import Optional

from app.model.models import NavigationStatus, NavigationTask, Pose
from app.ros.ros_node import ROSAdapter, make_navigation_task


class ROSNavigation:
    def __init__(self, adapter: ROSAdapter) -> None:
        self._adapter = adapter

    @property
    def is_available(self) -> bool:
        return self._adapter.is_available

    def navigate_to_pose(self, pose: Pose, target: Optional[str] = None) -> NavigationTask:
        task = make_navigation_task("nav")
        self._adapter.publish_navigation_control(
            {
                "action": 0,
                "points": [
                    {
                        "x": pose.x,
                        "y": pose.y,
                        "yaw": pose.yaw,
                        "uid": target or task.task_id,
                    }
                ],
            }
        )
        return task

    def cancel(self) -> NavigationTask:
        task = make_navigation_task("nav_cancel")
        self._adapter.publish_navigation_control(
            {
                "action": 1,
                "task_id": task.task_id,
            }
        )
        return task

    def get_navigation_status(self) -> NavigationStatus:
        return self._adapter.get_navigation_status()
