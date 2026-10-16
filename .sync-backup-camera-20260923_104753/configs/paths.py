"""
全局路径配置文件。

所有硬编码路径集中管理，各模块只从这里导入。

仓库内路径统一由 ROOT_DIR（自动定位到仓库根目录）推导，移动部署无需改代码；
仓库外依赖路径（ROS 包 / 导航栈 / 设备配置等）默认值集中在此，并支持
环境变量覆盖，换终端路径不同时只需配置环境变量。
"""

import configparser
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

AVAILABLE_TOOLS_FILE   = ROOT_DIR / "reference" / "available_tools.yaml"

# ---- CHAT AGENT / TASK AGENT 的提示词与响应格式（ToolManager/task_manager.py 读）----
# 注意：本树没有 Agents/ 目录（旧代的 chat_loop / task_loop 一并删了），这两个文件也不存在；
# 常量照 cmcc 登记只为让 import 通过，真正要读文件的那条路径在新树里是空转。
CHAT_AGENT_PROMPT_FILE = ROOT_DIR / "Agents" / "chat" / "chat_agent.md"
CHAT_RESPONSE_FORMAT_FILE = ROOT_DIR / "Agents" / "chat" / "chat_response_format.json"
TASK_AGENT_PROMPT_FILE = ROOT_DIR / "Agents" / "task" / "task_agent.md"
TASK_RESPONSE_FORMAT_FILE = ROOT_DIR / "Agents" / "task" / "response_format.json"

# =============================================================================
# Agent Loop — 运行时数据文件（YAML）
# =============================================================================

RUNTIME_VISION_OBSERVATION       = ROOT_DIR / "runtime_files" / "vision_obs.yaml"
RUNTIME_INTERACTION_HISTORY_FILE = ROOT_DIR / "runtime_files" / "interaction_history.yaml"
# MEMORY_FILE = ROOT_DIR / "runtime_files" / "memory.md"
RECENT_FILE = ROOT_DIR / "runtime_files" / "recent.json"

RUNTIME_ROBOT_STATUS = ROOT_DIR / "runtime_files" / "robot_status.yaml"
# motion_status.yaml —— 运动状态，由 Motion_Manager/motion_status.py 在 agent 进程内线程里写
RUNTIME_MOTION_STATUS = ROOT_DIR / "runtime_files" / "motion_status.yaml"

# 上面三条 RUNTIME_*（vision_obs.yaml / robot_status.yaml / motion_status.yaml）同时是
# **监督器（Supervisor）每轮读的观测源**：它按原文读、不解析，形状由各生产者决定（RUNTIME_*
# 各自的注释）。路径经 scripts/role-daemon.sh 过桥进进程环境，由 Supervisor/resident.patch.yml
# 的 `!!js process.env.SUPERVISOR_*` 取回 —— cordis 的配置层读不到 Python，这三条路径只登记在这里。
FRAME_PRODUCER_LOCK_FILE = ROOT_DIR / "runtime_files" / "frame_producer.lock"

#   vision_tunables.yaml —— 视觉观测参数的**运行时可调**覆盖层（观测周期 / 拼接帧数）。
#   读：vision_observer 的观测线程**每轮**取一次（一次 stat，mtime 变了才重解析，
#       见 configs/vision_tunables.py）—— 改它不用重启 agent，下一轮生效。
#       抓拍帧生产者**不读它**：帧生产者是常驻的取帧服务，一直按固定高频率抓帧把帧环
#       攒满，节奏只在构造时定（configs.FRAME_PRODUCER_INTERVAL），不参与运行时调参。
#   写：同一模块的 CLI（人）与 vision_tunables 工具（LLM），都走「独立锁 + tmp+rename」。
#   文件不存在 = 两项都回 configs/configs.py 里的出厂默认 —— 删掉它就是「一键回默认」。
#   注意它**不是**启动期配置，也不是第二份常量：出厂默认与可调区间只登记在
#   configs/configs.py（DEFAULT_INTERVAL / VISION_STACK_FRAMES / VISION_OBSERVE_INTERVAL_*），
#   这里只是覆盖层。
RUNTIME_VISION_TUNABLES = ROOT_DIR / "runtime_files" / "vision_tunables.yaml"
# 锁必须锁在**另一个文件**上：写盘走 tmp+rename 会换 inode，锁在被替换的那个文件上等于没锁
# （与 interaction_history.yaml.lock 同一个理由）。
RUNTIME_VISION_TUNABLES_LOCK = ROOT_DIR / "runtime_files" / "vision_tunables.yaml.lock"

# 组件心跳文件：谁拥有的进程由谁写，status_observer 只读 mtime 判活
#   receiver.heartbeat        —— Communicator/receiver.py 的 rclpy 定时器写（RECEIVER_HEARTBEAT_SEC）
#   vision_observer.heartbeat —— Observer/vision_observer.py 主循环每轮写（就绪之后才开始）
RECEIVER_HEARTBEAT_FILE = ROOT_DIR / "runtime_files" / "receiver.heartbeat"
VISION_OBSERVER_HEARTBEAT_FILE = ROOT_DIR / "runtime_files" / "vision_observer.heartbeat"

# 待做任务清单（plugin_tools/task_board.py）。活跃清单只装未终结的任务、整份原子覆盖；
# 归档只追加已终结的任务。两者都在 runtime_files/ 下，随运行时产生，不入库。
TASK_BOARD_FILE = ROOT_DIR / "runtime_files" / "task_board.yaml"
TASK_BOARD_ARCHIVE_FILE = ROOT_DIR / "runtime_files" / "task_board_archive.jsonl"

# 统一启动入口（根目录 start_agent.py）拉起的后台组件日志目录：
RUNTIME_LOGS_DIR = ROOT_DIR / "runtime_files" / "logs"

# 视觉抓拍落盘目录：vision_dispatch（环绕搜索）与 gesture_check（四宫格）共用，
RUNTIME_CAMERA_DIR = ROOT_DIR / "runtime_files" / "camera"

# 图像文件落盘**唯一**目录：frame_producer（周期抓拍）、capture_direct、cleanup 共用，
# 保留张数见 configs.FRAME_PRODUCER_PHOTOS_KEEP
RUNTIME_PHOTOS_DIR = ROOT_DIR / "runtime_files" / "photos"

# =============================================================================
# Agent Loop — 任务目录
# =============================================================================

TASKS_DIR = ROOT_DIR / "tasks"
def task_progress_path(task_id: str) -> Path:
    return TASKS_DIR / task_id / "task_progress.yaml"

def task_info_path(task_id: str) -> Path:
    return TASKS_DIR / task_id / "completer_result.json"

SKILL_VALIDATION_FILE = ROOT_DIR / "reference" / "skill_validation.md"

# =============================================================================
# Receiver
# =============================================================================

DEVICE_CONFIG_FILE = _env_path("CMCC_DEVICE_CONFIG", "/etc/cmcc_robot/cmcc_dev.ini")

def _read_device_id() -> str:
    """从设备配置文件读 SN（factory/devSn），失败回退 "unknown"。

    只在本模块导入时读一次：设备 SN 一次运行内不会变，读失败也不该中断启动。
    """
    try:
        if not DEVICE_CONFIG_FILE.exists():
            raise FileNotFoundError(f"配置文件不存在: {DEVICE_CONFIG_FILE}")
        config = configparser.ConfigParser()
        config.read(str(DEVICE_CONFIG_FILE))
        return config.get("factory", "devSn")
    except Exception as exc:
        print(f"[设备ID读取失败] {exc}")
        return "unknown"

DEVICE_ID = _read_device_id()

# =============================================================================
# 外部依赖
# =============================================================================

# CMCC 机器人安装（仓库外依赖）
CMCC_ROBOT_INSTALL = _env_path("CMCC_ROBOT_INSTALL", "/usr/bin/cmcc_robot/install")
# ROS 环境入口：消息/服务定义以运行版为准，source 这个（不是 /root/cmcc_robot/setup.bash）
CMCC_ROBOT_SETUP = CMCC_ROBOT_INSTALL / "setup.bash"
# 运行版自带库目录（如 libw_esr.so）。旧脚本曾写死陈旧树的 env/lib，会用错版本。
CMCC_ROBOT_LIB_DIR = CMCC_ROBOT_INSTALL / "env" / "lib"
# 唤醒回应音频目录：wakeup04~10.wav 在此被临时替换为静音（见 skills/asr-listener）
WAKEUP_RES_DIR = (CMCC_ROBOT_INSTALL / "homi_speech" / "share" / "homi_speech"
                  / "launch" / "res")

# ROS 2 发行版与 unitree overlay（仓库外固定安装）
ROS_SETUP = _env_path("CMCC_ROS_SETUP", "/opt/ros/foxy/setup.bash")
UNITREE_ROS_SETUP = _env_path(
    "CMCC_UNITREE_ROS_SETUP",
    "/opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash",
)

# 拼图标签字体：frame_producer 与 camera/capture_direct 都要在同一张图上写字，
LABEL_FONT_PATH = _env_path(
    "CMCC_LABEL_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
)
# ASR 静音 WAV：asr_monitor.py 生成、mute_wakeup_reply.sh 读同一份
SILENCE_WAV = _env_path("CMCC_SILENCE_WAV", "/tmp/asr_silence_16k.wav")
# 唤醒回应音频文件名（相对 WAKEUP_RES_DIR，不含目录）
WAKEUP_REPLY_FILES = ("wakeup04.wav", "wakeup05.wav", "wakeup06.wav", "wakeup07.wav",
                      "wakeup08.wav", "wakeup09.wav", "wakeup10.wav")

# 本机系统状态（仓库外，Observer/status_observer.py 读）
IW_BIN = _env_path("CMCC_IW_BIN", "/usr/sbin/iw")
# 公网 IP 反查（机器人所在地理位置，robot_status.yaml 的 location 段）用的 curl；
# 两条 URL 登记在 configs/configs.py
CURL_BIN = _env_path("CMCC_CURL_BIN", "/usr/bin/curl")
WIRELESS_IFACES_FILE = Path("/proc/net/wireless")
# 判定 camera 那把跨进程单实例锁有没有被持有：lock 文件在 /proc/locks 里的记录（内核给的
# 设备号 + inode，见 status_observer 的 _camera_lock_held）。与上面那条一样是「本机现读」。
PROC_LOCKS_FILE = Path("/proc/locks")

# =============================================================================
# Skills — 技能脚本路径
# 所有技能脚本路径统一在此登记，各模块从这里导入，不再硬编码。
# =============================================================================

TTS_SCRIPT = ROOT_DIR / "skills" / "tts-speak" / "scripts" / "run_tts_speak.sh"
ASR_SCRIPT = ROOT_DIR / "skills" / "asr-listener" / "scripts" / "run_asr.sh"
# run_asr.sh 只是环境壳，真正收声的在这里
ASR_MONITOR_SCRIPT = ROOT_DIR / "skills" / "asr-listener" / "scripts" / "asr_monitor.py"
# 唤醒回应音频的原件备份（mute/restore 两个脚本与 asr_monitor.py 共用同一份）
WAKEUP_BACKUP_DIR = ROOT_DIR / "skills" / "asr-listener" / "backup_wakeup"
# 唤醒回应音频恢复脚本（幂等，可重复调用）：ASR 子进程被超时 SIGKILL 时，
# asr_monitor.py 的 finally 可能来不及跑，唤醒回应会停在静音态，由父进程补调一次。
RESTORE_WAKEUP_REPLY_SCRIPT = ROOT_DIR / "skills" / "asr-listener" / "scripts" / "restore_wakeup_reply.sh"
FIND_OBJ_SCRIPT = ROOT_DIR / "skills" / "search" / "scripts" / "run_findobj_search.sh"
ACTION_SCRIPT = ROOT_DIR / "skills" / "robot-dog-actions" / "scripts" / "robot_action.sh"
MOTION_SCRIPT = ROOT_DIR / "skills" / "robot-dog-motion-control" / "scripts" / "motion.sh"
# 运动前站立检查（读 /catch_turtle/action_state）。工具层在 move 下发前调它，
# 判据与等待上限见 configs.ACTION_STATE_STANDING / MOVE_STAND_TIMEOUT。
MOTION_STATE_SCRIPT = ROOT_DIR / "skills" / "robot-dog-motion-control" / "scripts" / "check_action_state.sh"
VISION_DISPATCH_SCRIPT = ROOT_DIR / "skills" / "vision" / "scripts" / "vision_dispatch.py"
VISION_LOCAL_SCRIPT = ROOT_DIR / "skills" / "vision" / "scripts" / "vision_local.py"
# vision 脚本目录本身：gesture_check / local-search 的分析脚本要把它插进 sys.path
# 才能 import vision_local，别在各处写自己的上跳层数。
VISION_SCRIPTS_DIR = ROOT_DIR / "skills" / "vision" / "scripts"
LOCAL_SEARCH_SCRIPT = ROOT_DIR / "skills" / "vision" / "scripts" / "local-search" / "scripts" / "run_local_search.sh"
# 相机抓拍入口：capture.sh 是稳定契约那层薄壳（exec 同目录的 capture_direct.py）；真正
# 抓帧的是 capture_direct.py，直接读 /dev/video*，抓前 fuser -k 抢设备。
CAMERA_CAPTURE_SCRIPT = ROOT_DIR / "skills" / "camera" / "scripts" / "capture.sh"
LATEST_FRAME_STREAM_MODULE = ROOT_DIR / "skills" / "camera" / "latest_frame_stream.py"
FRAME_PRODUCER_MODULE = ROOT_DIR / "skills" / "camera" / "frame_producer.py"
# 后台观测组件（统一启动入口 start_agent.py 用）。两者与 receiver 是并列组件，
VISION_OBSERVER_MODULE = ROOT_DIR / "Observer" / "vision_observer.py"
STATUS_OBSERVER_MODULE = ROOT_DIR / "Observer" / "status_observer.py"
# get_stacked_frame 每次返回的拼接图落盘目录（时间戳命名，保留张数见 configs）
CAMERA_PHOTOS_DIR = ROOT_DIR / "skills" / "camera" / "photos"
TURN2SUBJECT_SCRIPT = ROOT_DIR / "skills" / "vision" / "scripts" / "local-search" / "scripts" / "turn2subject.sh"
# 手势确认技能：run_gesture_check.sh 连拍四张拼四宫格，交给 analyze_vision_check.py 判读
GESTURE_CHECK_SCRIPT = ROOT_DIR / "skills" / "gesture_check" / "scripts" / "run_gesture_check.sh"
ANALYZE_VISION_CHECK_SCRIPT = ROOT_DIR / "skills" / "gesture_check" / "scripts" / "analyze_vision_check.py"
# 「过来」技能：常驻跟随的起停（harness 的 CMCC_TURN_TO_ME_*_SCRIPT 指这两个）
TURN_TO_ME_START_SCRIPT = ROOT_DIR / "skills" / "turn-to-me" / "scripts" / "summon_start.sh"
TURN_TO_ME_STOP_SCRIPT = ROOT_DIR / "skills" / "turn-to-me" / "scripts" / "summon_stop.sh"
# 斗牛技能：目录带连字符不能按包路径 import，tool_executor 按文件路径 importlib 加载
BULL_CHASE_SCRIPT = ROOT_DIR / "skills" / "bull-chase" / "scripts" / "bull_chase.py"
# 导航支持的目标地标名单，直接读导航 app 的数据文件（skills/waypoint_nav/landmarks.json 是它的软联接）
SUPPORTED_LOCATIONS_FILE = ROOT_DIR / "skills" / "waypoint_nav" / "app" / "data" / "landmarks.json"
# 导航 VLN 栈位于仓库外，随终端部署不同 → 走环境变量覆盖（换机器只需配 CMCC_VLN_SCRIPT）
VLN_SCRIPT = _env_path(
    "CMCC_VLN_SCRIPT",
    "/root/omninav_stack_runs/dog_omninav_stack_isolated_20260701/scripts/run_vln_instruction.sh",
)

# =============================================================================
# 统一启动入口 — Agent 循环入口文件（根目录 start_agent.py）
# =============================================================================
RECEIVER_MODULE  = ROOT_DIR / "Communicator" / "receiver.py"
# 旧代的 task 主循环入口。本树没有 Agents/（task_loop 那套连同 Agents/ 一起删了），
# 常量照 cmcc 登记只为让 Communicator/startup_cleanup.py 的 import 通过：
# 它的 _MAIN_ENTRIES 拿这条判「有没有另一套系统在跑」，新树里这个路径永远不会命中。
TASK_LOOP_MODULE = ROOT_DIR / "Agents" / "task" / "task_loop.py"
# 入口脚本自身：receiver 是在**这个进程里**跑（start_agent 导入后直接调它的 main），
# 所以那套系统的根进程命令行是 `python3 start_agent.py cloud`、**根本不含 receiver.py**。
# 判「有没有另一套 agent 在跑」必须首先看它，否则抓不到最常见的重复启动方式。
START_AGENT_MODULE = ROOT_DIR / "start_agent.py"

# =============================================================================
# ROS 服务 / 话题
# =============================================================================
ROS_SIGC_DATA_SERVICE  = "/homi_speech/sigc_data_service"
ROS_SIGC_EVENT_TOPIC   = "/homi_speech/sigc_event_topic"
ROS_TTS_SERVICE        = "/audio_center/play_tts"
