"""导航控制工具（navigation 技能）

**导航服务 HTTP 的唯一客户端**：对导航后端（configs.paths.NAVIGATION_BASE_URL）的
查询/控制接口，全部收口在这里。原先 status_observer 的状态探针、motion_status 的
两个探针、motion_manager._dispatch_navigation、tool_executor.waypoint_nav 各写一份
`requests.get(NAVIGATION_STATUS_URL)`，超时与解析口径各说各话；现在它们都调本模块。

契约（八个函数一律如此，调用方自己判断）：

    拿到 JSON 应答   → 返回解析后的对象（dict / list，原样）
    应答不是 JSON    → 返回**原文**（str）
    取不到（连不上 / 超时 / 空体）→ 返回 ""（空串）

**永不抛异常**——调用方无需 try/except（导航空闲时取消导航之类「失败也无妨」的场景
直接看返回值即可）。超时必须由调用方给（探针 2s / 控制类 5s / 工具层 10s 是三件事，
见 configs/configs.py：NAVIGATION_PROBE_TIMEOUT / _CONTROL_TIMEOUT / _REQUEST_TIMEOUT），
本模块不藏默认值。

提供（前八条 = skills/waypoint_nav/navigation_api.md 的 §3.1 / §4.1 / §4.2 / §4.3 / §5.1 / §5.2 / §5.3 / §5.4 全部端点）：
    list_landmarks(timeout)              —— 获取所有可导航的目标地标点位（GET /api/v1/landmarks）
    navigation_status(timeout)           —— 查询当前导航任务状态（GET /api/v1/navigation/status）
    localization_status(timeout)         —— 查询底盘是否已完成定位（GET /api/v1/localization/status）
    localization_pose(timeout)           —— 查询实时坐标与朝向（GET /api/v1/localization/pose）
    navigate(destination, timeout)       —— 发起定点导航到地点名（POST /api/v1/navigation/go）
    go_pose(x, y, timeout, yaw=None)     —— 发起定点导航到地图坐标（POST /api/v1/navigation/go_pose）
    relocalize(timeout)                  —— 启动自动重定位（POST /api/v1/localization/relocalize）
    cancel_navigation(timeout)           —— 取消当前导航任务（POST /api/v1/navigation/cancel）

文档的 §6 跟随能力（POST /api/v1/accompany/{start,stop}、GET /api/v1/accompany/status）**不在本模块
范围内**：那是另一种能力，端侧目前完全没有接线，等真有调用方时再加。

**localization_pose / go_pose 目前没有调用方**：加它们是为了让文档 §4.3 与 §5.2 在智能体侧有个
现成的落点（要「汇报当前坐标」或按坐标导航时直接用），不是为了当下就有人调。
"""

import sys
from pathlib import Path
from typing import Any, Optional, Union

# 确保项目根目录在 Python 搜索路径中（支持直接运行）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import requests

from configs.paths import (
    NAVIGATION_CANCEL_URL,
    NAVIGATION_GO_POSE_URL,
    NAVIGATION_GO_URL,
    NAVIGATION_LANDMARKS_URL,
    NAVIGATION_LOCALIZATION_STATUS_URL,
    NAVIGATION_POSE_URL,
    NAVIGATION_RELOCALIZE_URL,
    NAVIGATION_STATUS_URL,
)

# 应答类型：解析后的 JSON 对象 / 非 JSON 的原文 / ""（取不到）
NavReply = Union[dict, list, str]


def _request(method: str, url: str, timeout: float, json_body: Any = None) -> NavReply:
    """按上面的三态契约发一次请求；任何异常都折成 ""。

    不调 raise_for_status：HTTP 状态码不进返回值，判断一律看应答体里的 code/message
    （服务端的 HTTP 状态与体里的 code 同源，见 navigation_api.md 的统一响应格式）。
    """
    try:
        resp = requests.request(method, url, json=json_body, timeout=timeout)
        text = resp.text
    except Exception:  # noqa: BLE001 —— 连不上/超时/requests 缺失都算「取不到」
        return ""
    if not text or not text.strip():
        return ""
    try:
        return resp.json()
    except ValueError:      # 应答不是 JSON（代理的错误页等）：原样交出去，由调用方判断
        return text


def _get(url: str, timeout: float) -> NavReply:
    """GET 一次，返回应答（三态同 _request）。"""
    return _request("GET", url, timeout)


def _post(url: str, timeout: float, json_body: Any = None) -> NavReply:
    """POST 一次，返回应答（三态同 _request）。"""
    return _request("POST", url, timeout, json_body)


def list_landmarks(timeout: float) -> NavReply:
    """获取当前环境中所有可导航的目标地标点位。

    适用场景：用户询问当前可前往的地点、校验目标地点名称是否合法。
    """
    return _get(NAVIGATION_LANDMARKS_URL, timeout)


def navigation_status(timeout: float) -> NavReply:
    """查询机器人当前导航任务的运行状态（data.status）。

    status 枚举：
        IDLE      空闲（无导航任务）
        RUNNING   导航进行中
        SUCCESS   导航任务成功完成
        FAILED    导航任务失败
        CANCELLED 导航任务已取消
    """
    return _get(NAVIGATION_STATUS_URL, timeout)


def localization_status(timeout: float) -> NavReply:
    """查询底盘此刻是否已完成定位（data.localized）。

    这是「能不能导航」的前提判据：false 时定点导航会先自动重定位（见 tool_executor）。
    """
    return _get(NAVIGATION_LOCALIZATION_STATUS_URL, timeout)


def localization_pose(timeout: float) -> NavReply:
    """查询机器人在当前地图中的实时坐标与朝向（data.x / data.y / data.yaw，米与度）。

    只在已定位时有意义：未定位时服务端按错误码应答（1001 机器人未定位）。
    """
    return _get(NAVIGATION_POSE_URL, timeout)


def navigate(destination: str, timeout: float) -> NavReply:
    """发起定点导航到某个地点名（不等它跑完，进度另查 navigation_status）。"""
    return _post(NAVIGATION_GO_URL, timeout, {"destination": destination})


def go_pose(x: float, y: float, timeout: float, yaw: Optional[float] = None) -> NavReply:
    """发起定点导航到地图坐标 (x, y[, yaw])，不经过地标名。

    yaw 为 None 时请求体里不带该字段，由服务端用它的默认朝向（0.0）；给 0.0 与不给
    是两件事，别用 0.0 顶替「不知道朝向」。
    """
    body = {"x": x, "y": y} if yaw is None else {"x": x, "y": y, "yaw": yaw}
    return _post(NAVIGATION_GO_POSE_URL, timeout, body)


def relocalize(timeout: float) -> NavReply:
    """启动自动重定位（navigation_api.md §4.2）。

    应答形如 {"code":0,"message":"success","data":{"task_id":"loc_…"}} —— 它只表示
    服务端**受理**了，定位是否真的完成得另查 localization_status。
    """
    return _post(NAVIGATION_RELOCALIZE_URL, timeout)


def cancel_navigation(timeout: float) -> NavReply:
    """取消机器人当前导航任务（导航空闲时失败也无妨，调用方按返回值判断）。"""
    return _post(NAVIGATION_CANCEL_URL, timeout)


if __name__ == "__main__":
    # 自检入口（与 weather.py 末尾同款）：不带参数=逐条 **只读** 接口问一遍，服务没起时打 ''。
    # 控制类（navigate / go_pose / relocalize / cancel_navigation）**故意不进来**：真服务在跑时
    # 它们会让机器狗动起来，自检不能有这种副作用。
    from configs.configs import NAVIGATION_CONTROL_TIMEOUT as _T

    print(f"list_landmarks      -> {list_landmarks(_T)}")
    print(f"navigation_status   -> {navigation_status(_T)}")
    print(f"localization_status -> {localization_status(_T)}")
    print(f"localization_pose   -> {localization_pose(_T)}")
