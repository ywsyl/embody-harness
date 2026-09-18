"""
全局路径配置文件。

所有硬编码路径集中管理，各模块只从这里导入。

仓库内路径统一由 ROOT_DIR（自动定位到仓库根目录）推导，移动部署无需改代码；
仓库外依赖路径（ROS 包 / 导航栈 / 设备配置等）默认值集中在此，并支持
环境变量覆盖，换终端路径不同时只需配置环境变量。
"""

import os
from pathlib import Path


def _env_path(key: str, default: str) -> Path:
    """外部依赖路径：优先取环境变量 key，缺省用 default（支持 ~ 展开）。

    仅用于仓库外、随终端环境变化的路径；仓库内路径一律用 ROOT_DIR 推导。
    """
    return Path(os.environ.get(key, default)).expanduser()


# =============================================================================
# 基础目录
# =============================================================================

# 自动定位仓库根目录（本文件 configs/paths.py 的上上级目录）
ROOT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT_DIR / "tasks"


# ---- 公共引用文件 ----------------------------------------------------
SCHEMA_FILE = ROOT_DIR / "reference" / "schema.json"
CONFIDENCE_FILE = ROOT_DIR / "reference" / "confidence.md"
SCHEMA_DOC_FILE = ROOT_DIR / "reference" / "schema.md"
CONSTRAINTS_FILE = ROOT_DIR / "reference" / "constraints.md"

# ---- CHAT AGENT ----------------------------------------------------
CHAT_AGENT_PROMPT_FILE = ROOT_DIR / "Agents" / "chat" / "chat_agent.md"
CHAT_RESPONSE_FORMAT_FILE = ROOT_DIR / "Agents" / "chat" / "chat_response_format.json"
FOLLOWUP_RESPONSE_FORMAT_FILE = ROOT_DIR / "Agents" / "chat" / "followup_response_format.json"
# ---- TASK AGENT ----------------------------------------------------
TASK_AGENT_PROMPT_FILE = ROOT_DIR / "Agents" / "task" / "task_agent.md"
AVAILABLE_TOOLS_FILE   = ROOT_DIR / "plugin_tools" / "available_tools.yaml"
TASK_RESPONSE_FORMAT_FILE   = ROOT_DIR / "Agents" / "task" / "response_format.json"

# =============================================================================
# Agent Loop — 运行时数据文件（YAML）
# =============================================================================

RUNTIME_VISION_OBSERVATION       = ROOT_DIR / "runtime_files" / "vision_obs.yaml"
# 本体状态清单：监督器（dsh-plugins/supervisor-loop）每轮读的第二份观测。读侧只把原文拼进
# 上下文、不解析，所以形状由生产端决定，样例见 Supervisor/robot_status.example.yaml；
# 生产端（谁往这里写）尚未落地。
RUNTIME_ROBOT_STATUS             = ROOT_DIR / "runtime_files" / "robot_status.yaml"
RUNTIME_INTERACTION_HISTORY_FILE = ROOT_DIR / "runtime_files" / "interaction_history.yaml"
RECENT_FILE = ROOT_DIR / "runtime_files" / "recent.json"
HISTORY_FILE = ROOT_DIR / "runtime_files" / "history.json"
MEMORY_FILE = ROOT_DIR / "runtime_files" / "memory.md"

# 待做任务清单（plugin_tools/task_board.py）。活跃清单只装未终结的任务、整份原子覆盖；
# 归档只追加已终结的任务。两者都在 runtime_files/ 下，随运行时产生，不入库。
TASK_BOARD_FILE = ROOT_DIR / "runtime_files" / "task_board.yaml"
TASK_BOARD_ARCHIVE_FILE = ROOT_DIR / "runtime_files" / "task_board_archive.jsonl"

# [2026-09-02 停用] 相机抢占 flock 设计整体取消：加锁后视觉技能调用变慢（含
# capture_direct 恢复等待）。恢复时：取消本常量注释 + 取消 vision_lock.py /
# task_loop.py / tool_executor.py / vision_observer.py / capture_direct.py 中
# 标注 [2026-09-02 停用] 的代码。
# VISION_TOOL_LOCK_FILE = ROOT_DIR / "runtime_files" / "vision_tool_active.lock"

# =============================================================================
# Agent Loop — 任务目录
# =============================================================================

TASKS_DIR = ROOT_DIR / "tasks"
def task_resolved_path(task_id: str) -> Path:
    return TASKS_DIR / task_id / "task_solved.json"

def task_progress_path(task_id: str) -> Path:
    return TASKS_DIR / task_id / "task_progress.yaml"

def task_info_path(task_id: str) -> Path:
    return TASKS_DIR / task_id / "completer_result.json"

def task_extract_path(task_id: str) -> Path:
    return TASKS_DIR / task_id / "extractor_result.json"

# =============================================================================
# InfoCompleter
# =============================================================================

EXTRACTOR_PROMPT_FILE = ROOT_DIR / "InfoCompleter" / "extractor.md"
COMPLETER_PROMPT = ROOT_DIR / "InfoCompleter" / "complete.md"
SKILL_VALIDATION_FILE = ROOT_DIR / "reference" / "skill_validation.md"

# ---- Router（Stage1 信息提取） ------------------------------------------
ROUTER_PROMPT_FILE = ROOT_DIR / "Router" / "router.md"

# =============================================================================
# Receiver
# =============================================================================

DEVICE_CONFIG_FILE = _env_path("CMCC_DEVICE_CONFIG", "/etc/cmcc_robot/cmcc_dev.ini")

# claw_client ROS2 包路径（receiver 备用导入，默认未启用）
CLAW_CLIENT_LIB = _env_path("CMCC_CLAW_CLIENT_LIB", "/root/cmcc_robot/claw_client/lib/python3.8/site-packages")

# =============================================================================
# Skills — 技能脚本路径
# 所有技能脚本路径统一在此登记，各模块从这里导入，不再硬编码。
# =============================================================================

TTS_SCRIPT = ROOT_DIR / "skills" / "tts-speak" / "scripts" / "run_tts_speak.sh"
ASR_SCRIPT = ROOT_DIR / "skills" / "asr-listener" / "scripts" / "run_asr.sh"
FIND_OBJ_SCRIPT = ROOT_DIR / "skills" / "search" / "scripts" / "run_findobj_search.sh"
ACTION_SCRIPT = ROOT_DIR / "skills" / "robot-dog-actions" / "scripts" / "robot_action.sh"
MOTION_SCRIPT = ROOT_DIR / "skills" / "robot-dog-motion-control" / "scripts" / "motion.sh"
VISION_DISPATCH_SCRIPT = ROOT_DIR / "skills" / "vision" / "scripts" / "vision_dispatch.py"
VISION_LOCAL_SCRIPT = ROOT_DIR / "skills" / "vision" / "scripts" / "vision_local.py"
LOCAL_SEARCH_SCRIPT = ROOT_DIR / "skills" / "vision" / "local-search" / "scripts" / "run_local_search.sh"
CAMERA_CAPTURE_SCRIPT = ROOT_DIR / "skills" / "robot-dog-camera" / "scripts" / "capture.sh"
LATEST_FRAME_STREAM_MODULE = ROOT_DIR / "skills" / "camera" / "latest_frame_stream.py"
FRAME_PRODUCER_MODULE = ROOT_DIR / "skills" / "camera" / "frame_producer.py"
TURN2SUBJECT_SCRIPT = ROOT_DIR / "skills" / "vision" / "local-search" / "scripts" / "turn2subject.sh"
NAVIGATION_LANDMARKS_FILE = ROOT_DIR / "skills" / "waypoint_nav" / "scripts" / "landmarks.json"
# 导航 VLN 栈位于仓库外，支持环境变量覆盖
VLN_SCRIPT = _env_path("CMCC_VLN_SCRIPT", "/root/omninav_stack_runs/dog_omninav_stack_isolated_20260701/scripts/run_vln_instruction.sh")

# =============================================================================
# ROS 服务 / 话题
# =============================================================================

ROS_SIGC_DATA_SERVICE  = "/homi_speech/sigc_data_service"
ROS_SIGC_EVENT_TOPIC   = "/homi_speech/sigc_event_topic"
ROS_TTS_SERVICE        = "/audio_center/play_tts"
