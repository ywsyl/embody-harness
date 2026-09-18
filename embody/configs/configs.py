"""
全局配置参数文件。

集中管理非路径类的可调参数，各模块从这里导入。
"""

import json as _json
import re as _re
from pathlib import Path as _Path

# =============================================================================
# LLM — 大模型配置
# =============================================================================

LLM_CONFIGS = {
    # 高质量模型：复杂推理 / 任务规划等重场景
    "large": {
        "env_model_name": "LOCAL_MODEL_ds",     
        "max_token": 8192,
    },
    # 轻量模型：闲聊 / 简单回复等低成本场景
    "small": {
        "env_model_name": "LOCAL_MODEL_qw",
        "max_token": 4096,
    },
}

# =============================================================================
# Observer — 视觉观测
# =============================================================================

VISION_PROMPT = "详细描述一下你看到的场景和场景中的人、物等（100字以内）"
DEFAULT_INTERVAL = 5    # 秒
VISION_TIMEOUT = 30       # 秒
VISION_MAX_ENTRIES = 1  # vision_obs.yaml 最多保留最近 N 条观测记录

# ---- 视觉帧源：robot-dog-camera 周期抓拍生产者（skills/camera/frame_producer.py）
FRAME_PRODUCER_INTERVAL = 3.0        # 后台周期抓拍间隔（秒）
FRAME_PRODUCER_CAPTURE_TIMEOUT = 5   # 单次 robot-dog-camera 拍照超时（秒）
FRAME_PRODUCER_MAX_FRAMES = 3        # 内存帧环保留最近帧数（消费者错峰/追帧窗口）


# =============================================================================
# Supervisor — 监督器定时观察
# =============================================================================
# 监督器每隔 N 秒读一遍观测文件（runtime_files/vision_obs.yaml、robot_status.yaml），
SUPERVISOR_INTERVAL = 10   # 秒

# 上下文预算。
SUPERVISOR_RETAIN_ROUNDS = 10        # 压缩后保留最近约 N 轮原文
SUPERVISOR_ROUND_TOKENS = 1200       # 一轮的量级（token），把上面的轮数换算成预算
SUPERVISOR_CONTEXT_CEILING = 50000   # 总上下文上限（token），到这儿开始压缩；须大于保留预算
SUPERVISOR_SESSION_MAX_MB = 0.1      # 单份会话日志到多少 MB 就轮转换成新的一份。


def supervisor_context_window() -> int:
    """
    路由声明的上下文窗口（token）。

    压缩的触发点（thresholdRatio）只有「占窗口的比例」一种写法，所以要从窗口换算。窗口的
    事实源是 configs/settings-model.yaml 的 contextWindow，这里按文本取那个键：取不到就报错，
    而不是退回某个默认窗口——那会让阈值悄悄变一个量级。
    """
    path = _Path(__file__).with_name("settings-model.yaml")
    match = _re.search(r"^\s*contextWindow:\s*(\d+)\s*$", path.read_text(encoding="utf-8"), _re.M)
    if match is None:
        raise ValueError(f"settings-model.yaml 里找不到 contextWindow：{path}")
    return int(match.group(1))


# =============================================================================
# Waypoint Navigation — 定点导航
# =============================================================================
SUPPORTED_LOCATIONS_FILE = "/root/tjc/agent2/app/data/landmarks.json"
def load_supported_locations() -> tuple:
    try:
        data = _json.loads(_Path(SUPPORTED_LOCATIONS_FILE).read_text(encoding="utf-8"))
    except Exception:
        return ()
    if not isinstance(data, list):
        return ()
    names = [
        str(item.get("name")).strip()
        for item in data
        if isinstance(item, dict) and item.get("name")
    ]
    return tuple(names)

NAVIGATION_BASE_URL = "http://127.0.0.1:8000"
NAVIGATION_GO_URL = f"{NAVIGATION_BASE_URL}/api/v1/navigation/go"
NAVIGATION_STATUS_URL = f"{NAVIGATION_BASE_URL}/api/v1/navigation/status"
NAVIGATION_CANCEL_URL = f"{NAVIGATION_BASE_URL}/api/v1/navigation/cancel"
NAVIGATION_LANDMARKS_URL = f"{NAVIGATION_BASE_URL}/api/v1/landmarks"


# =============================================================================
# 云端 HTTP 服务
# =============================================================================
# 上报接口
CLOUD_BASE_URL = "http://36.137.127.46:8071"
# CLOUD_WRITE_STATE_URL = "http://36.137.127.46:8071/image_service/write_state"
CLOUD_WRITE_STATE_URL = "http://36.137.127.46:8071//image_service-yanshi/write_state"
# 视觉服务
VISION_LOCAL_VQA_URL = "http://36.137.127.46:8070/test_vqa-yanshi"
# VISION_LOCAL_VQA_URL = "http://36.137.127.46:8070/test_vqa"


# =============================================================================
# Motion — move 工具速度参数（robot-dog-motion-control 技能）
# =============================================================================
MOVE_LINEAR_SPEED = 0.6    # 平移速度（米/秒），技能建议 0.3-0.8
MOVE_ANGULAR_SPEED = 1.2   # 旋转速度
MOVE_90_DURATION = 1.6     # 旋转90度需要的时间

# 超出上限的值被裁剪到上限（非拒绝），防止误传巨值导致运动失控。
MOVE_MAX_LINEAR_DISTANCE = 10.0    # 平移最大距离（米）
MOVE_MAX_ANGULAR_DEGREES = 720.0   # 旋转最大角度（度）


# =============================================================================
# Interaction History — 交互历史
# =============================================================================
# load_interaction_history 最多保留的历史记录条数（截取末尾 N 条），
INTERACTION_HISTORY_MAX = 30

# =============================================================================
# TTS 播报时长估算
# =============================================================================
TTS_RATE = 5.          # 估算语速（字/秒），接近实测播放语速，宁长勿短
TTS_BASE = 0.          # 末句收尾 + 安全余量（秒）；首分句在服务返回前已开播
TTS_FLOOR = 1.2         # 最短等待（秒），短文本由它兜底，避免播完后长静默

# =============================================================================
# 状态值参数 — 任务级（TASK_STATUS）与步骤级（STEP_STATUS）
# =============================================================================
TASK_STATUS = {
    "PENDING": {
        "value": "pending",
        "description": "该任务已创建但尚未开始执行",
    },
    "EXECUTING": {
        "value": "executing",
        "description": "任务尚未完成，当前任务仍可继续执行",
    },
    "WAITING": {
        "value": "waiting",
        "description": "执行时阻塞中：等待信息返回、资源释放中",
    },
    "SUCCEEDED": {
        "value": "succeeded",
        "description": "任务无需后续执行：目标达成或已明确完成",
    },
    "FAILED": {
        "value": "failed",
        "description": "任务被系统停止：用户要求的任务目标已无法合理继续（连续失败且没有合理重试方案）",
    },
    "STOPPED": {
        "value": "stopped",
        "description": "任务被用户停止：用户暂停（中止）/取消（终止）/ 触发急停状态 等",
    },
}

# 步骤级状态（interaction_X.status / step_X.status）的合法值，
STEP_STATUS = {
    "WAITING": {
        "value": "waiting",
        "description": "该步骤阻塞中：等待信息返回、资源释放中",
    },
    "RUNNING": {
        "value": "running",
        "description": "该步骤正在执行中",
    },
    "SUCCEEDED": {
        "value": "succeeded",
        "description": "该步骤已有充分证据表明实际完成",
    },
    "FAILED": {
        "value": "failed",
        "description": "该步骤返回了预期外结果，或已无法合理继续（连续失败超出重试限制等）",
    }
}


# 上报/播报类工具（调用前先播报预置文案），见 plugin_tools/tool_executor.py 的
# parse_and_execute。空元组 = 该预播报不启用；本表只在这一处被 tool_executor 按名成员测试。
REPORT_TOOLS: tuple = ()
