from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from typing import Any, Dict, Optional

from app.model.models import NavigationStatus, NavigationTask, Pose, TaskStatus, TaskStatusMessage

logger = logging.getLogger(__name__)

NAVIGATION_TASK_COMPLETED_CODES = {2013, 2005}
NAVIGATION_POINT_REACHED_CODES = {2012, 2004}
NAVIGATION_SUCCESS_CODES = NAVIGATION_TASK_COMPLETED_CODES | NAVIGATION_POINT_REACHED_CODES
NAVIGATION_CANCELLED_CODES = {2014, 2006}
NAVIGATION_RUNNING_CODES = {2010}
NAVIGATION_IDLE_CODES = {2011}
NAVIGATION_FAILED_CODES = {2110, 2111, 2112, 2104, 2113, 2105, 2114, 2215}
NAVIGATION_STATUS_CODES = (
    NAVIGATION_SUCCESS_CODES
    | NAVIGATION_CANCELLED_CODES
    | NAVIGATION_RUNNING_CODES
    | NAVIGATION_IDLE_CODES
    | NAVIGATION_FAILED_CODES
)
LOCALIZATION_STATUS_CODES = {
    2000,
    2001,
    2002,
    2003,
    2100,
    2101,
    2102,
    2103,
    2106,
    2107,
    2207,
    2301,
}

try:
    import rclpy
    from nav_msgs.msg import Odometry
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from std_msgs.msg import String
except Exception as exc:  # pragma: no cover - exercised only without ROS2 installed.
    rclpy = None  # type: ignore[assignment]
    Node = object  # type: ignore[assignment,misc]
    QoSProfile = None  # type: ignore[assignment]
    ReliabilityPolicy = None  # type: ignore[assignment]
    String = None  # type: ignore[assignment]
    Odometry = None  # type: ignore[assignment]
    _ROS_IMPORT_ERROR: Optional[Exception] = exc
else:
    _ROS_IMPORT_ERROR = None


def _new_task_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _yaw_from_quaternion(x: float, y: float, z: float, w: float) -> float:
    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    return math.degrees(math.atan2(siny_cosp, cosy_cosp))


def _task_status_qos() -> Any:
    if QoSProfile is None or ReliabilityPolicy is None:
        return 10
    return QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT)


def _normalize_status(value: Any) -> TaskStatus:
    if isinstance(value, int):
        if value in NAVIGATION_SUCCESS_CODES:
            return TaskStatus.SUCCESS
        if value in NAVIGATION_CANCELLED_CODES:
            return TaskStatus.CANCELLED
        if value in NAVIGATION_RUNNING_CODES:
            return TaskStatus.RUNNING
        if value in NAVIGATION_IDLE_CODES:
            return TaskStatus.IDLE
        if value in NAVIGATION_FAILED_CODES:
            return TaskStatus.FAILED

        mapping = {
            0: TaskStatus.IDLE,
            1: TaskStatus.RUNNING,
            2: TaskStatus.SUCCESS,
            3: TaskStatus.FAILED,
            4: TaskStatus.CANCELLED,
        }
        return mapping.get(value, TaskStatus.FAILED)

    text = str(value or "").strip().upper()
    if text.isdigit():
        return _normalize_status(int(text))

    aliases = {
        "IDLE": TaskStatus.IDLE,
        "PENDING": TaskStatus.RUNNING,
        "RUNNING": TaskStatus.RUNNING,
        "ACTIVE": TaskStatus.RUNNING,
        "SUCCESS": TaskStatus.SUCCESS,
        "SUCCEEDED": TaskStatus.SUCCESS,
        "DONE": TaskStatus.SUCCESS,
        "FAILED": TaskStatus.FAILED,
        "FAILURE": TaskStatus.FAILED,
        "ERROR": TaskStatus.FAILED,
        "CANCELLED": TaskStatus.CANCELLED,
        "CANCELED": TaskStatus.CANCELLED,
    }
    return aliases.get(text, TaskStatus.FAILED)


class NavigationRosNode(Node):  # type: ignore[misc]
    def __init__(self) -> None:
        super().__init__("navigation_http_service")
        self.localization_control_pub = self.create_publisher(String, "/localization/control", 10)
        self.navigation_control_pub = self.create_publisher(String, "/navigation/control", 10)
        self.accompany_start_pub = self.create_publisher(String, "/accompany/start", 10)
        self.accompany_end_pub = self.create_publisher(String, "/accompany/end", 10)
        self.odom_sub = self.create_subscription(Odometry, "/localization/odom", self._on_odom, 10)
        self.task_status_sub = self.create_subscription(
            String,
            "/task_status",
            self._on_task_status,
            _task_status_qos(),
        )
        self._lock = threading.RLock()
        self._pose: Optional[Pose] = None
        self._task_status: Optional[TaskStatusMessage] = None
        self._navigation_status = NavigationStatus(status=TaskStatus.IDLE)
        self._accompany_status: TaskStatus = TaskStatus.IDLE
        self._last_task_status_raw: Optional[str] = None
        self._last_task_status_code: Optional[int] = None
        self._last_task_status_received_at: Optional[float] = None
        self.get_logger().info("Subscribed to /task_status with BEST_EFFORT QoS.")

    def publish_accompany_start(self) -> None:
        self._publish_string(self.accompany_start_pub, "start")
        with self._lock:
            self._accompany_status = TaskStatus.RUNNING

    def publish_accompany_end(self) -> None:
        self._publish_string(self.accompany_end_pub, "end")
        with self._lock:
            self._accompany_status = TaskStatus.IDLE

    def get_accompany_status(self) -> TaskStatus:
        with self._lock:
            return self._accompany_status

    def publish_localization(self, payload: Dict[str, Any]) -> None:
        self._publish_json(self.localization_control_pub, payload)

    def publish_navigation(self, payload: Dict[str, Any]) -> None:
        self._publish_json(self.navigation_control_pub, payload)
        if payload.get("action") == 0:
            first_point = _first_point(payload)
            with self._lock:
                self._navigation_status = NavigationStatus(
                    status=TaskStatus.RUNNING,
                    destination=_optional_str(
                        payload.get("target")
                        or payload.get("destination")
                        or (first_point or {}).get("uid")
                    ),
                )
        elif payload.get("action") == 1:
            with self._lock:
                self._navigation_status.status = TaskStatus.CANCELLED

    def get_pose(self) -> Optional[Pose]:
        with self._lock:
            return self._pose

    def get_task_status(self) -> Optional[TaskStatusMessage]:
        with self._lock:
            return self._task_status

    def get_navigation_status(self) -> NavigationStatus:
        with self._lock:
            return self._navigation_status

    def get_debug_state(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "navigation_status": self._navigation_status.model_dump(mode="json"),
                "last_task_status_code": self._last_task_status_code,
                "last_task_status_raw": self._last_task_status_raw,
                "last_task_status_received_at": self._last_task_status_received_at,
            }

    def _publish_json(self, publisher: Any, payload: Dict[str, Any]) -> None:
        msg = String()
        msg.data = json.dumps(payload, ensure_ascii=False)
        publisher.publish(msg)
        self.get_logger().info(f"Published ROS control message: {msg.data}")

    def _publish_string(self, publisher: Any, text: str) -> None:
        msg = String()
        msg.data = text
        publisher.publish(msg)
        topic = getattr(publisher, "topic_name", "topic")
        self.get_logger().info(f"Published ROS string message to {topic}: {msg.data}")

    def _on_odom(self, msg: Any) -> None:
        position = msg.pose.pose.position
        orientation = msg.pose.pose.orientation
        pose = Pose(
            x=round(float(position.x), 3),
            y=round(float(position.y), 3),
            yaw=round(
                _yaw_from_quaternion(
                    float(orientation.x),
                    float(orientation.y),
                    float(orientation.z),
                    float(orientation.w),
                ),
                2,
            ),
        )
        with self._lock:
            self._pose = pose

    def _on_task_status(self, msg: Any) -> None:
        raw_text = str(getattr(msg, "data", ""))
        self.get_logger().info(f"Received /task_status: {_preview_text(raw_text)}")
        with self._lock:
            self._last_task_status_raw = raw_text
            self._last_task_status_received_at = time.time()
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            self.get_logger().warning(f"Failed to decode /task_status JSON: {_preview_text(raw_text)}")
            payload = {"status": raw_text}

        if not isinstance(payload, dict):
            self.get_logger().warning(f"Unexpected /task_status payload type: {type(payload).__name__}")
            payload = {"status": payload}

        detail = payload.get("msg")
        if not isinstance(detail, dict):
            detail = {}

        status_code = _optional_int(payload.get("code"))
        with self._lock:
            self._last_task_status_code = status_code
        self.get_logger().info(
            "Parsed /task_status: "
            f"code={status_code}, "
            f"detail_msg={detail.get('msg')}, "
            f"detail_uid={detail.get('uid')}"
        )

        if status_code in NAVIGATION_TASK_COMPLETED_CODES:
            status = TaskStatusMessage(
                task_id=_optional_str(payload.get("task_id") or payload.get("id")),
                task_type="navigation",
                status=TaskStatus.SUCCESS,
                target=_optional_str(
                    payload.get("target")
                    or payload.get("destination")
                    or payload.get("uid")
                    or detail.get("target")
                    or detail.get("destination")
                    or detail.get("uid")
                ),
                remaining_distance=_optional_float(
                    _first_present(
                        payload,
                        "remaining_distance",
                        "distance_remaining",
                        "distance",
                        fallback=_first_present(detail, "remaining_distance", "distance_remaining", "distance"),
                    )
                ),
                message=_optional_str(
                    payload.get("message")
                    or detail.get("message")
                    or detail.get("msg")
                    or payload.get("msg")
                ),
                raw=payload,
            )
            with self._lock:
                self._task_status = status
                self._navigation_status = NavigationStatus(
                    status=TaskStatus.SUCCESS,
                    destination=status.target or self._navigation_status.destination,
                )
                current_navigation_status = self._navigation_status
            self.get_logger().info(
                "Navigation completed from /task_status: "
                f"code={status_code}, "
                f"status={current_navigation_status.status.value}, "
                f"destination={current_navigation_status.destination}"
            )
            return

        status = TaskStatusMessage(
            task_id=_optional_str(payload.get("task_id") or payload.get("id")),
            task_type=_optional_str(
                payload.get("task_type") or payload.get("type") or _task_type_from_code(status_code)
            ),
            status=_normalize_status(_first_present(payload, "status", "state", fallback=status_code)),
            target=_optional_str(
                payload.get("target")
                or payload.get("destination")
                or payload.get("uid")
                or detail.get("target")
                or detail.get("destination")
                or detail.get("uid")
            ),
            remaining_distance=_optional_float(
                _first_present(
                    payload,
                    "remaining_distance",
                    "distance_remaining",
                    "distance",
                    fallback=_first_present(detail, "remaining_distance", "distance_remaining", "distance"),
                )
            ),
            message=_optional_str(
                payload.get("message")
                or detail.get("message")
                or detail.get("msg")
                or payload.get("msg")
            ),
            raw=payload,
        )

        with self._lock:
            self._task_status = status
            if status.task_type in (None, "navigation", "nav", "NAVIGATION"):
                self._navigation_status = NavigationStatus(
                    status=status.status,
                    destination=status.target or self._navigation_status.destination,
                )
                current_navigation_status = self._navigation_status
                self.get_logger().info(
                    "Navigation status updated from /task_status: "
                    f"task_type={status.task_type}, "
                    f"status={current_navigation_status.status.value}, "
                    f"destination={current_navigation_status.destination}, "
                    f"message={status.message}"
                )
            else:
                self.get_logger().info(
                    "Ignored non-navigation /task_status: "
                    f"task_type={status.task_type}, "
                    f"status={status.status.value}, "
                    f"message={status.message}"
                )


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _preview_text(value: str, limit: int = 500) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...<truncated {len(value) - limit} chars>"


def _first_present(payload: Dict[str, Any], *keys: str, fallback: Any = None) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return fallback


def _task_type_from_code(value: Any) -> Optional[str]:
    code = _optional_int(value)
    if code is None:
        return None
    if code in NAVIGATION_STATUS_CODES:
        return "navigation"
    if code in LOCALIZATION_STATUS_CODES:
        return "localization"
    return None


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _first_point(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    points = payload.get("points")
    if isinstance(points, list) and points and isinstance(points[0], dict):
        return points[0]
    return None


class ROSAdapter:
    """Owns ROS2 initialization and exposes safe methods to service classes."""

    def __init__(self) -> None:
        self._node: Optional[NavigationRosNode] = None
        self._spin_thread: Optional[threading.Thread] = None
        self._available = False
        self._startup_error: Optional[str] = None

    @property
    def is_available(self) -> bool:
        return self._available and self._node is not None

    @property
    def startup_error(self) -> Optional[str]:
        return self._startup_error

    def start(self) -> None:
        if self.is_available:
            return

        if rclpy is None:
            self._startup_error = f"ROS2 Python libraries are not available: {_ROS_IMPORT_ERROR}"
            logger.warning(self._startup_error)
            return

        try:
            if not rclpy.ok():
                rclpy.init(args=None)
            self._node = NavigationRosNode()
            self._available = True
            self._spin_thread = threading.Thread(target=self._spin, name="ros2-spin", daemon=True)
            self._spin_thread.start()
            logger.info("ROS2 adapter started.")
        except Exception as exc:
            self._available = False
            self._startup_error = str(exc)
            logger.exception("Failed to start ROS2 adapter.")

    def stop(self) -> None:
        if rclpy is None:
            return

        node = self._node
        self._available = False
        self._node = None
        if node is not None:
            try:
                node.destroy_node()
            except Exception:
                logger.exception("Failed to destroy ROS2 node.")

        try:
            if rclpy.ok():
                rclpy.shutdown()
        except Exception:
            logger.exception("Failed to shutdown rclpy.")

        if self._spin_thread and self._spin_thread.is_alive():
            self._spin_thread.join(timeout=2.0)

    def publish_localization_control(self, payload: Dict[str, Any]) -> None:
        node = self._require_node()
        node.publish_localization(payload)

    def publish_navigation_control(self, payload: Dict[str, Any]) -> None:
        node = self._require_node()
        node.publish_navigation(payload)

    def get_pose(self) -> Optional[Pose]:
        node = self._require_node()
        return node.get_pose()

    def get_navigation_status(self) -> NavigationStatus:
        node = self._require_node()
        return node.get_navigation_status()

    def publish_accompany_start(self) -> None:
        node = self._require_node()
        node.publish_accompany_start()

    def publish_accompany_end(self) -> None:
        node = self._require_node()
        node.publish_accompany_end()

    def get_accompany_status(self) -> TaskStatus:
        if not self.is_available or self._node is None:
            return TaskStatus.IDLE
        return self._node.get_accompany_status()

    def get_debug_state(self) -> Dict[str, Any]:
        if not self.is_available or self._node is None:
            return {
                "available": False,
                "startup_error": self._startup_error,
            }
        return {
            "available": True,
            "startup_error": self._startup_error,
            **self._node.get_debug_state(),
        }

    def _spin(self) -> None:
        logger.info("ROS2 spin thread started.")
        while self._available and self._node is not None and rclpy is not None and rclpy.ok():
            try:
                rclpy.spin_once(self._node, timeout_sec=0.2)
            except Exception:
                logger.exception("ROS2 spin failed.")
                self._available = False
                time.sleep(1.0)
        logger.info("ROS2 spin thread stopped.")

    def _require_node(self) -> NavigationRosNode:
        if not self.is_available or self._node is None:
            raise RuntimeError("ROS2 adapter is unavailable.")
        return self._node


def make_navigation_task(prefix: str) -> NavigationTask:
    return NavigationTask(task_id=_new_task_id(prefix))
