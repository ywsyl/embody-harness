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


# ---- 公共引用文件 ----------------------------------------------------
# 工具清单与它的实现同在 plugin_tools/（读到它的只有 configs.agents 的白名单与那里的
# tool_parser / tool_executor），不放在只作参考文档用的 reference/。
AVAILABLE_TOOLS_FILE   = ROOT_DIR / "plugin_tools" / "available_tools.yaml"

# 云端点名执行（receiver 的 exe 事件）认的工具集：**另一份清单**，不是上面那份的子集白名单——
# 角色系统按 configs.agents.ROLE_TOOLS 分派，这条腿按这份文件分派，两边互不影响。
AVAILABLE_TOOLS_CLOUD_FILE = ROOT_DIR / "plugin_tools" / "available_tools_cloud.yaml"

# =============================================================================
# Agent Loop — 运行时数据文件（YAML）
# =============================================================================

# runtime_files/ —— 机器人自己的数据区，下面各条 RUNTIME_* 都是这棵目录里的具体文件。
# 提到**整棵目录**的只有两处：Observer/cleanup.py 的体积清理，与 dsh-plugins/file-guard 的
# allowedRoot（交互器 / 编排器 / 监督器 / planner 唯一能读写的项目内目录，经
# scripts/role-daemon.sh 过桥成 ROLE_FILE_GUARD_ALLOWED_ROOTS）。
RUNTIME_DIR = ROOT_DIR / "runtime_files"

RUNTIME_VISION_OBSERVATION       = RUNTIME_DIR / "vision_obs.yaml"
RUNTIME_INTERACTION_HISTORY_FILE = RUNTIME_DIR / "interaction_history.yaml"

# pending_answer.json —— 云端 answer 事件的用户回答单槽（Communicator/pending_answer.py）。
# **当前两边都停用，没有读写方**（写侧 receiver 自 2026-09-28、读侧 listen / speak_listen
# 自 2026-10-08 改本地 ASR，均在代码里注释保留）。原设计：写者只有一个（receiver，收到
# answer 就整份原子覆盖），读者是角色侧的 listen / speak_listen 工具（轮询本文件的 seq 取新
# 回答）。两侧不在同一个进程（receiver 在组件进程，工具是角色 dsh 进程的子进程），所以只能
# 经文件过桥 —— 这也是它从「进程内 Condition」改成落盘的原因。
RUNTIME_PENDING_ANSWER = RUNTIME_DIR / "pending_answer.json"

# spoken_stream.json —— 「这一轮已经播到第几个字」的记录，交互器流式播报的去重依据。
# 写者只有一个：dsh-plugins/speech-stream（只装在 interactor）—— 它在 speak / speak_listen /
# speak_input 的 text 参数**还在逐片到达**时就把已经成句的部分送进 TTS，每交接一句整份原子
# 覆盖本文件（tmp+rename）。读方是角色侧的播报实现（plugin_tools/audio_tools.py，入口是 tool_executor.py 的三个播报工具）：
# 派发时认领与自己 text 相同的那条记录 —— 已经全播过就只等播完、不再调 TTS，播到一半就只补
# 剩下的那截；没有这份文件（插件没装载 / 刚重启 / 记录过期）就照老路整段播。
# **因此开关插件等于开关流式播报**，工具侧没有任何模式分支。
# 读方不写、写方只有一个进程，所以不带 .lock（与 vision_tunables 的「写方有两个」不同）。
# spoken 记的是**已交给 TTS 的明文长度**，按解码后的字符下标算 —— 与工具侧切片同一基准。
# 同一份文件里还有顶层的 drained_at：插件那条逐句队列见底（每句都等到播完才退）的时刻。工具侧
# 等「这段文字播完」只能靠它 —— 播报是插件送出去的、没有 section_id，而状态话题在两句之间本来
# 就会空一次（见 plugin_tools/audio_tools.py 的 _wait_streamed_done）。
RUNTIME_SPOKEN_STREAM_FILE = RUNTIME_DIR / "spoken_stream.json"

RUNTIME_ROBOT_STATUS = RUNTIME_DIR / "robot_status.yaml"
# motion_status.yaml —— 运动状态，由 Motion_Manager/motion_status.py 在 agent 进程内线程里写
RUNTIME_MOTION_STATUS = RUNTIME_DIR / "motion_status.yaml"

# 上面三条 RUNTIME_*（vision_obs.yaml / robot_status.yaml / motion_status.yaml）同时是
# **监督器（Supervisor）每轮读的观测源**：它按原文读、不解析，形状由各生产者决定（RUNTIME_*
# 各自的注释）。路径经 scripts/role-daemon.sh 过桥进进程环境，由 Supervisor/resident.patch.yml
# 的 `!!js process.env.SUPERVISOR_*` 取回 —— cordis 的配置层读不到 Python，这三条路径只登记在这里。
# frame_producer.lock —— 「全系统只有一个帧环」的判据（flock 独占）。持有者就是取帧服务
# 进程（`frame_producer.py --serve`）：抢到锁才 bind socket、才开抓拍。status_observer 判
# camera 一格死活也是读 /proc/locks 看这把锁（见 Observer/status_observer.probe_camera）。
FRAME_PRODUCER_LOCK_FILE = RUNTIME_DIR / "frame_producer.lock"

# frame_producer.sock —— 取帧服务的 unix socket：帧生产者是**独立进程**
# （`frame_producer.py --serve`，由 start_agent 作为组件子进程拉起），本 socket 是它对外
# 唯一的取帧口。
#   服务端只有一个：持 FRAME_PRODUCER_LOCK_FILE 的那个服务进程。
#   客户端是全体消费者：视觉观测器、视觉技能脚本（vision_dispatch）、角色侧工具进程
#   （search_old / look / bull_chase）—— 它们不在同一个进程里。
#   为什么必须跨进程：帧环是「帧生产者所在进程」的内存，别的进程读不到，这正是本 socket
#   存在的理由。选 unix socket 而不是 TCP/HTTP：帧是二进制大块（单帧约 400KB），原生字节
#   传输、不开监听端口、权限由内核限到本机。
#   陈 socket（服务被 kill -9 留下）由服务端自己处理：抢到 flock 独占锁即证明没有活着的
#   服务 → unlink 后重新 bind，见 frame_producer._serve()。
#   长度须 < 108 字节（unix sun_path 上限），本路径约 60 字节。
FRAME_PRODUCER_SOCKET = RUNTIME_DIR / "frame_producer.sock"

# tts_speak.lock / tts_speak.sock —— 常驻 TTS 服务（TTS_Manager/tts_service.py --serve）。
#   与取帧服务同一形态、同一理由：TTS 客户端要**常驻**（每句起一个进程 = 首句前 0.8s 的
#   shell 前导 + 1.3s 的 CLI 启动与 DDS 发现，逐句投递时那两段全变成句间空档），而消费者
#   分布在好几个进程里（交互器 daemon 的 dsh 插件、工具桥的 Python 进程、audio_interact 的
#   一次性脚本），所以常驻的那个由 start_agent 作组件子进程拉起，经 unix socket 对外服务。
#   服务端只有一个：持 TTS_SERVICE_LOCK_FILE 的那个进程；它同时也是**语音状态话题的唯一
#   长期订阅者**（观察者那一格改为向它要数据，见 Observer/status_observer.probe_audio）。
#   陈 socket（服务被 kill -9 留下）由服务端自己处理：抢到 flock 独占锁即证明没有活着的
#   服务 → unlink 后重新 bind。
TTS_SERVICE_LOCK_FILE = RUNTIME_DIR / "tts_speak.lock"
TTS_SERVICE_SOCKET = RUNTIME_DIR / "tts_speak.sock"

#   vision_tunables.yaml —— 视觉观测参数的**运行时可调**覆盖层（观测周期 / 拼接帧数）。
#   读：vision_observer 的观测线程**每轮**取一次（一次 stat，mtime 变了才重解析，
#       见 Orchestrator/vision_tunables.py）—— 改它不用重启 agent，下一轮生效。
#       抓拍帧生产者**不读它**：帧生产者是常驻的取帧服务，一直按固定高频率抓帧把帧环
#       攒满，节奏只在构造时定（configs.FRAME_PRODUCER_INTERVAL），不参与运行时调参。
#   写：同一模块的 CLI（人）与 vision_tunables 工具（LLM），都走「独立锁 + tmp+rename」。
#   文件不存在 = 两项都回 configs/configs.py 里的出厂默认 —— 删掉它就是「一键回默认」。
#   注意它**不是**启动期配置，也不是第二份常量：出厂默认与可调区间只登记在
#   configs/configs.py（DEFAULT_INTERVAL / VISION_STACK_FRAMES / VISION_OBSERVE_RATE_LEVELS
#   —— 观测周期的可调区间就是那张档位表的跨度），这里只是覆盖层。
RUNTIME_VISION_TUNABLES = RUNTIME_DIR / "vision_tunables.yaml"
# 锁必须锁在**另一个文件**上：写盘走 tmp+rename 会换 inode，锁在被替换的那个文件上等于没锁
# （与 interaction_history.yaml.lock 同一个理由）。
RUNTIME_VISION_TUNABLES_LOCK = RUNTIME_DIR / "vision_tunables.yaml.lock"

# 组件心跳文件：谁拥有的进程由谁写，status_observer 只读 mtime 判活
#   receiver.heartbeat        —— Communicator/receiver.py 的 rclpy 定时器写（RECEIVER_HEARTBEAT_SEC）
#   vision_observer.heartbeat —— Observer/vision_observer.py 主循环每轮写（就绪之后才开始）
RECEIVER_HEARTBEAT_FILE = RUNTIME_DIR / "receiver.heartbeat"
VISION_OBSERVER_HEARTBEAT_FILE = RUNTIME_DIR / "vision_observer.heartbeat"

# 待做任务清单（plugin_tools/task_board.py）—— 交互器与编排器共享一份。
RUNTIME_TASKS_DIR = RUNTIME_DIR / "tasks"
RUNTIME_PENDING_TASKS_FILE = RUNTIME_TASKS_DIR / "pending_tasks.yaml"
# 锁必须锁在**另一个文件**上：写盘走 tmp+rename 会换 inode，锁在被替换的那个文件上等于没锁
RUNTIME_PENDING_TASKS_LOCK = RUNTIME_TASKS_DIR / "pending_tasks.yaml.lock"

def runtime_task_file(task_id: str) -> Path:
    """一条**已出清单**任务的存档文件（终结的，或停了太久被移出来的），文件名即 task_id。

    task_id 会直接当文件名用，调用方须先过 plugin_tools.task_board.check_task_id。
    """
    return RUNTIME_TASKS_DIR / f"{task_id}.yaml"

# 统一启动入口（根目录 start_agent.py）拉起的后台组件日志目录：
RUNTIME_LOGS_DIR = RUNTIME_DIR / "logs"

# 图像文件落盘**唯一**目录：frame_producer（周期抓拍）、capture_direct、cleanup 共用。
# 体积上限见 configs.RUNTIME_PHOTOS_TRIGGER_MB / _TARGET_MB
RUNTIME_PHOTOS_DIR = RUNTIME_DIR / "photos"

# 三个 runtime_files/ 子目录的体积清理入口（Observer/cleanup.py）。判据（两档水位、哪些文件
# 可删）全在那个模块与 configs/configs.py 里，这里只提供**给外部执行者用的绝对路径**：
# 调它的是 dsh-plugins/supervisor-loop，路径经 scripts/role-daemon.sh 过桥成
# SUPERVISOR_CLEANUP_SCRIPT（同 CAMERA_CAPTURE_SCRIPT 那类「跨语言调用的脚本路径」）。
RUNTIME_CLEANUP_SCRIPT = ROOT_DIR / "Observer" / "cleanup.py"

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

# 可直接当标识用的设备 SN：读失败时 DEVICE_ID 是占位符 "unknown"，它不是一个有效标识
# （拿它当会话 ID / SN 用，会让所有读不到 SN 的机器共用同一处），故归一成空串。
# 消费方：VQA 请求的 session_id 默认值（skills/vision/scripts/vision_local.py、
# Observer/vision_observer.py）——没传 session_id 就用它，空串则整个字段不传。
DEVICE_SN = "" if DEVICE_ID == "unknown" else DEVICE_ID.strip()

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
# 只等一次**已经在放**的播报播完（播报是 dsh 插件送出去的，调用方拿不到 section_id）。
# 流式播报下「这段文字已经由插件送出去了」时用；run_tts_speak.sh 的 wait_done=true 是
# 自己发出去再自己等，两者走同一个判定脚本 wait_tts_done.py。
WAIT_TTS_DONE_SCRIPT = ROOT_DIR / "skills" / "tts-speak" / "scripts" / "wait_tts_done.sh"
# 一趟完整交互（audio_interact 工具）：静默唤醒（复用 asr-listener）→ 播报（复用 tts-speak）
# → 等状态话题收尾，把期间收到的 ASR 最终文本（status=2 finish=true）当用户这一轮的话返回。
# 它跨了三个技能的代码，既不是 tts-speak 的附属也不是 asr-listener 的，故单开一个技能目录。
AUDIO_INTERACT_SCRIPT = ROOT_DIR / "skills" / "audio_interact" / "audio_interact.sh"
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
# 相机抓拍入口：capture.sh 是稳定契约那层薄壳（exec 同目录的 capture_direct.py）；真正
# 抓帧的是 capture_direct.py，直接读 /dev/video*，抓前 **fuser -k 抢设备**（会把正在
# 推流的 video_gst 节点杀掉，它未配 respawn、不会自己回来）。
# frame_producer **不再用它**（已改用下面的 CAMERA_SHM_CAPTURE_SCRIPT）；这里仍是
# harness 的 cmcc_camera 工具与兄弟部署认的对外契约，路径不能动。
CAMERA_CAPTURE_SCRIPT = ROOT_DIR / "skills" / "camera" / "scripts" / "capture.sh"
# 帧生产者的取帧脚本：skills/camera/scripts/capture.py（2026-09-23 由 skills/robot-dog-camera/
# scripts/ 并入），用 GStreamer shmsrc 读相机节点写进共享内存的 JPEG 帧流（socket 见 CAMERA_SHM_SOCKET）。
# 与 CAMERA_CAPTURE_SCRIPT 的关键差别：**不碰 /dev/video*、不 fuser -k**，只当共享内存
# 的读者 —— 与本体原有视频流（video_gst 节点自身、以及别的消费者）并存互不干扰。
CAMERA_SHM_CAPTURE_SCRIPT = (
    ROOT_DIR / "skills" / "camera" / "scripts" / "capture.py"
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
FRAME_PRODUCER_MODULE = ROOT_DIR / "skills" / "camera" / "frame_producer.py"
# 后台观测组件（统一启动入口 start_agent.py 用）。两者与 receiver 是并列组件，
VISION_OBSERVER_MODULE = ROOT_DIR / "Observer" / "vision_observer.py"
STATUS_OBSERVER_MODULE = ROOT_DIR / "Observer" / "status_observer.py"
# 常驻 TTS 服务（与 Motion_Manager/ 同级的那套播报入口的服务端，见 configs/paths.py 的
# TTS_SERVICE_SOCKET 一段）。同样由 start_agent 作并列组件子进程拉起。
TTS_SERVICE_MODULE = ROOT_DIR / "TTS_Manager" / "tts_service.py"
TURN2SUBJECT_SCRIPT = ROOT_DIR / "skills" / "vision" / "scripts" / "local-search" / "scripts" / "turn2subject.sh"
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

# =============================================================================
# 导航服务 HTTP 端点
# =============================================================================
# URL 类常量一律归本文件 —— 与 configs/configs.py 的分工是「**地址在这里，参数在那里**」：
# 超时、轮询间隔、状态枚举仍在 configs/configs.py。
# 唯一消费者是 plugin_tools/nav_tools.py（导航服务 HTTP 的唯一客户端）：motion_status /
# status_observer / motion_manager / tool_executor 都经它取数，不再各自 requests.get。
NAVIGATION_BASE_URL = "http://127.0.0.1:8000"                               # 导航 app 的本机地址，下面几条 URL 都由它派生
NAVIGATION_GO_URL = f"{NAVIGATION_BASE_URL}/api/v1/navigation/go"           # 下发导航目标（按地点名）
NAVIGATION_GO_POSE_URL = f"{NAVIGATION_BASE_URL}/api/v1/navigation/go_pose" # 下发导航目标（按地图坐标 x/y/yaw，navigation_api.md §5.2；暂无调用方）
NAVIGATION_STATUS_URL = f"{NAVIGATION_BASE_URL}/api/v1/navigation/status"   # 查导航状态
NAVIGATION_CANCEL_URL = f"{NAVIGATION_BASE_URL}/api/v1/navigation/cancel"   # 取消导航
NAVIGATION_LANDMARKS_URL = f"{NAVIGATION_BASE_URL}/api/v1/landmarks"        # 拉地标列表
NAVIGATION_LOCALIZATION_STATUS_URL = f"{NAVIGATION_BASE_URL}/api/v1/localization/status"  # 查定位是否就绪（data.localized）
NAVIGATION_RELOCALIZE_URL = f"{NAVIGATION_BASE_URL}/api/v1/localization/relocalize"      # 自动重定位（skills/waypoint_nav/navigation_api.md §4.2）
NAVIGATION_POSE_URL = f"{NAVIGATION_BASE_URL}/api/v1/localization/pose"     # 查实时坐标与朝向（x/y/yaw，navigation_api.md §4.3；暂无调用方）
