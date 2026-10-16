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

# ---- CHAT AGENT ----------------------------------------------------
CHAT_AGENT_PROMPT_FILE = ROOT_DIR / "Agents" / "chat" / "chat_agent.md"
CHAT_RESPONSE_FORMAT_FILE = ROOT_DIR / "Agents" / "chat" / "chat_response_format.json"
FOLLOWUP_RESPONSE_FORMAT_FILE = ROOT_DIR / "Agents" / "chat" / "followup_response_format.json"
# ---- TASK AGENT ----------------------------------------------------
TASK_AGENT_PROMPT_FILE = ROOT_DIR / "Agents" / "task" / "task_agent.md"
AVAILABLE_TOOLS_FILE   = ROOT_DIR / "reference" / "available_tools.yaml"
TASK_RESPONSE_FORMAT_FILE   = ROOT_DIR / "Agents" / "task" / "response_format.json"

# =============================================================================
# Agent Loop — 运行时数据文件（YAML）
# =============================================================================

RUNTIME_VISION_OBSERVATION       = ROOT_DIR / "runtime_files" / "vision_obs.yaml"
RUNTIME_INTERACTION_HISTORY_FILE = ROOT_DIR / "runtime_files" / "interaction_history.yaml"
RECENT_FILE = ROOT_DIR / "runtime_files" / "recent.json"
HISTORY_FILE = ROOT_DIR / "runtime_files" / "history.json"
MEMORY_FILE = ROOT_DIR / "runtime_files" / "memory.md"

#   robot_status.yaml —— robot（ROS 订阅来的）+ system（本机现读）+ tools（六个组件各自
RUNTIME_ROBOT_STATUS = ROOT_DIR / "runtime_files" / "robot_status.yaml"
#   motion_status.yaml —— 运动状态，Motion_Manager/motion_status.py 在**agent 进程内线程**里
RUNTIME_MOTION_STATUS = ROOT_DIR / "runtime_files" / "motion_status.yaml"

# frame_producer 跨进程「单实例」锁：保证任意时刻全系统只有一条 frame_producer
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

#   receiver.heartbeat        —— Communicator/receiver.py 的 rclpy 定时器写（RECEIVER_HEARTBEAT_SEC）
RECEIVER_HEARTBEAT_FILE = ROOT_DIR / "runtime_files" / "receiver.heartbeat"
#   vision_observer.heartbeat —— Observer/vision_observer.py 主循环每轮写（就绪之后才开始）
VISION_OBSERVER_HEARTBEAT_FILE = ROOT_DIR / "runtime_files" / "vision_observer.heartbeat"

# 统一启动入口（根目录 start_agent.py）拉起的后台组件日志目录：
RUNTIME_LOGS_DIR = ROOT_DIR / "runtime_files" / "logs"

# 图像文件落盘**唯一**目录
RUNTIME_PHOTOS_DIR = ROOT_DIR / "runtime_files" / "photos"

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

# 设备 SN 常量：任务上报（Communicator）与技能脚本（findobj_search）共用，勿各处重读。
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
# 唤醒回应音频文件名（相对 WAKEUP_RES_DIR，不含目录）。
WAKEUP_REPLY_FILES = ("wakeup04.wav", "wakeup05.wav", "wakeup06.wav", "wakeup07.wav",
                      "wakeup08.wav", "wakeup09.wav", "wakeup10.wav")

# 本机系统状态（仓库外，Observer/status_observer.py 读）
IW_BIN = _env_path("CMCC_IW_BIN", "/usr/sbin/iw")
WIRELESS_IFACES_FILE = Path("/proc/net/wireless")

# =============================================================================
# Skills — 技能脚本路径
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
# 斗牛客户端：**进程内模块**（不是子进程脚本）——1.3 的视觉搜索要取共享帧环
# （帧生产者所在进程的内存），子进程读不到，故由 tool_executor 按本路径 import 后进程内调用。
# 目录名 bull-chase 带连字符、不能按包路径 import，工具层用 importlib 按文件加载。
BULL_CHASE_SCRIPT = ROOT_DIR / "skills" / "bull-chase" / "scripts" / "bull_chase.py"
ACTION_SCRIPT = ROOT_DIR / "skills" / "robot-dog-actions" / "scripts" / "robot_action.sh"
MOTION_SCRIPT = ROOT_DIR / "skills" / "robot-dog-motion-control" / "scripts" / "motion.sh"
# 运动前站立检查（读 /catch_turtle/action_state）。工具层在 move 下发前调它，
# 判据与等待上限见 configs.ACTION_STATE_STANDING / MOVE_STAND_TIMEOUT。
MOTION_STATE_SCRIPT = ROOT_DIR / "skills" / "robot-dog-motion-control" / "scripts" / "check_action_state.sh"
VISION_DISPATCH_SCRIPT = ROOT_DIR / "skills" / "vision" / "scripts" / "vision_dispatch.py"
# vision 脚本目录本身：要 import vision_local（进程内直调视觉服务）的分析脚本把它插进
# sys.path，别在各处写自己的上跳层数。
VISION_SCRIPTS_DIR = ROOT_DIR / "skills" / "vision" / "scripts"
# 相机抓拍入口：capture.sh 是稳定契约那层薄壳（exec 同目录的 capture_direct.py）；真正
# 抓帧的是 capture_direct.py，直接读 /dev/video*，抓前 **fuser -k 抢设备**（会把正在
# 推流的 video_gst 节点杀掉，它未配 respawn、不会自己回来）。
# frame_producer **不再用它**（已改用下面的 CAMERA_SHM_CAPTURE_SCRIPT）；这里仍是
# harness 的 cmcc_camera 工具与兄弟部署认的对外契约，路径不能动。
CAMERA_CAPTURE_SCRIPT = ROOT_DIR / "skills" / "camera" / "scripts" / "capture.sh"
# 帧生产者的取帧脚本：robot-dog-camera 的 capture.py，用 GStreamer shmsrc 读相机节点
# 写进共享内存的 JPEG 帧流（socket 见 CAMERA_SHM_SOCKET）。
# 与 CAMERA_CAPTURE_SCRIPT 的关键差别：**不碰 /dev/video*、不 fuser -k**，只当共享内存
# 的读者 —— 与本体原有视频流（video_gst 节点自身、以及别的消费者）并存互不干扰。
CAMERA_SHM_CAPTURE_SCRIPT = (
    ROOT_DIR / "skills" / "robot-dog-camera" / "scripts" / "capture.py"
)
# 相机共享内存 socket：video_gst 相机节点用 GStreamer `shmsink` 把 JPEG 帧写到这里
# （caps `image/jpeg, width=1920, height=1080`），capture.py 用 shmsrc 读同一路径。
# 命名规则 = /tmp/<相机>_<档位>：颈部 neck_* / 主相机 foo_*，档位 jpeg / fhd / hd / sd。
# ⚠️ **帧源自「视频流会话」**：socket 由客户端调 `/<ns>/get_video_service`（请求
# `resolution`，响应里带 `socket_path`）才建立并推帧，`end_video_service` 关流时节点会
# `rm /tmp/<ns>_*`。所以 socket 文件**存在 ≠ 有帧**（异常收尾会留死文件，读它 =
# Connection refused）。开会话的客户端（face_detector_node / cmcc_rtc_node 开颈部、
# live_stream_node / robdogQtControlNode 开主相机）**正常开机时常驻、会话一直开着**，
# 2026-09-23 实测帧是持续在写的；只有它们全退出、或节点本身没起，本路径才取不到帧。
CAMERA_SHM_SOCKET = Path("/tmp/neck_jpeg")
LATEST_FRAME_STREAM_MODULE = ROOT_DIR / "skills" / "camera" / "latest_frame_stream.py"
FRAME_PRODUCER_MODULE = ROOT_DIR / "skills" / "camera" / "frame_producer.py"
# 后台观测组件（统一启动入口 start_agent.py 用）。三者与 receiver 是并列组件，
# 由 start_agent 按开关决定起不起：
#   - 状态观测器：独立 CLI 进程（只订阅 ROS 话题，与帧环无关），拉成子进程；与智能体
#     只靠 robot_status.yaml 交互，故不必同进程。
#   - 视觉观测器：必须与帧生产者/主循环**同进程**起线程，不能拉成子进程，
#     原因见 Observer/vision_observer.py 与 ToolManager/tool_executor.py 的 _vision_dispatch。
#   - 运动状态观测器：同样**本进程内线程**，理由是它要直读 MotionManager 进程内的运动记录
#     （模块内存，不落盘）—— 挪出进程它就看不到「这一笔是谁下发的」了，见 motion_status.py。
#     （它不起子进程，故这里不登记模块路径常量：start_agent 按包名 import 后起线程。）
VISION_OBSERVER_MODULE = ROOT_DIR / "Observer" / "vision_observer.py"
STATUS_OBSERVER_MODULE = ROOT_DIR / "Observer" / "status_observer.py"
# 「转向目标」脚本：只做运动、不碰图像（tool_executor 的 search_old 找到目标后调它）
TURN2SUBJECT_SCRIPT = ROOT_DIR / "skills" / "vision" / "scripts" / "local-search" / "scripts" / "turn2subject.sh"
# 「过来」技能：常驻跟随的起停（harness 的 CMCC_TURN_TO_ME_*_SCRIPT 指这两个）
TURN_TO_ME_START_SCRIPT = ROOT_DIR / "skills" / "turn-to-me" / "scripts" / "summon_start.sh"
TURN_TO_ME_STOP_SCRIPT = ROOT_DIR / "skills" / "turn-to-me" / "scripts" / "summon_stop.sh"
# 导航支持的目标地标名单，直接读导航 app 的数据文件（skills/waypoint_nav/landmarks.json 是它的软联接）
SUPPORTED_LOCATIONS_FILE = ROOT_DIR / "skills" / "waypoint_nav" / "codes" / "app" / "data" / "landmarks.json"
# 导航 VLN 栈位于仓库外，随终端部署不同 → 走环境变量覆盖（换机器只需配 CMCC_VLN_SCRIPT）
VLN_SCRIPT = _env_path(
    "CMCC_VLN_SCRIPT",
    "/root/omninav_stack_runs/dog_omninav_stack_isolated_20260701/scripts/run_vln_instruction.sh",
)

# =============================================================================
# 统一启动入口 — Agent 循环入口文件（根目录 start_agent.py）
# =============================================================================
RECEIVER_MODULE  = ROOT_DIR / "Communicator" / "receiver.py"
TASK_LOOP_MODULE = ROOT_DIR / "Agents" / "task" / "task_loop.py"
# 入口脚本自身：receiver / task_loop 都是在**这个进程里**跑（start_agent 导入后直接调它们的
# main），所以那套系统的根进程命令行是 `python3 start_agent.py cloud`、**根本不含 receiver.py**。
# 判「有没有另一套 agent 在跑」必须首先看它，否则抓不到最常见的重复启动方式。
START_AGENT_MODULE = ROOT_DIR / "start_agent.py"

# =============================================================================
# ROS 服务 / 话题
# =============================================================================
# ROS_SIGC_DATA_SERVICE  = "/homi_speech/sigc_data_service"
ROS_SIGC_EVENT_TOPIC   = "/homi_speech/sigc_event_topic"
ROS_TTS_SERVICE        = "/audio_center/play_tts"
