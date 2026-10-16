"""
全局配置参数文件。

集中管理非路径类的可调参数，各模块从这里导入。
约定：**路径**常量登记在 configs/paths.py，**prompt 文案**登记在 configs/prompts.py，
与智能体绑定的表（工具白名单、任务/节点/对话状态枚举）登记在 configs/agents.py，
本文件只管通用参数，也不再转出它们。
"""

import json as _json
from pathlib import Path as _Path

# 供下面 load_supported_locations() 用；外部读者一律直接从 configs.paths 取
from configs.paths import SUPPORTED_LOCATIONS_FILE

# ================================================================ LLM — 大模型配置
LLM_CONFIGS = {
    "large": {"env_model_name": "LOCAL_MODEL_ds", "max_token": 8192},   # 复杂推理 / 任务规划
    "small": {"env_model_name": "LOCAL_MODEL_qw", "max_token": 4096},   # 闲聊 / 简单回复
}

# ============================================================
# ---- Observer — 视觉观测
VISION_TIMEOUT = 10                # 一次视觉问答（VQA）请求的超时（秒）
VISION_MAX_ENTRIES = 10            # vision_obs.yaml 保留的最近观测条数
DEFAULT_INTERVAL = 3               # 视觉观测周期（秒）：等下一帧的超时，也是 vision_observer --interval 的默认值
                                   # 同时是这一项的**出厂默认**（覆盖层里没有 observe_interval 时就用它）


def vision_alive_max_age_sec(observe_interval: float) -> float:
    """vision 心跳的判活阈值（秒）= 3 × 一轮最长耗时（等帧 + 一次 VQA）。

    为什么是个函数而不是常量：观测周期自 2026-09-22 起是**运行时可调**的（覆盖层
    runtime_files/vision_tunables.yaml），阈值必须跟着**生效值**走 —— 否则把周期调慢到 6s，
    status_observer 仍按 3s 那档判活，观测器会被判成「视觉死了」（心跳周期本来就等于观测周期）。
    系数 3 是原来那条常量里的余量，别往下调。status_observer 经
    configs/vision_tunables.py 的 alive_max_age_sec() 现读它。
    """
    return 3 * (float(observe_interval) + VISION_TIMEOUT)


# 视觉帧源：周期抓拍生产者 skills/camera/frame_producer.py
FRAME_PRODUCER_MAX_FRAMES = 10     # 内存帧环保留的最近帧数（消费者错峰 / 追帧窗口）
FRAME_PRODUCER_CAPTURE_TIMEOUT = 1.0   # 单次抓拍超时（秒）
FRAME_PRODUCER_INTERVAL = 0.2      # 后台抓拍间隔（秒）——**固定值，不受运行时覆盖层影响**
                                   # 生产者是全体消费者共享的常驻取帧服务（观测器、视觉工具、任务
                                   # 主循环都从它的帧环取帧），就该一直以高频率抓帧、把帧环攒满；
                                   # 「LLM 能调的频率」调的是观测器多久来看一次，不是它抓得多快。
FRAME_PRODUCER_PHOTOS_KEEP = 50    # 受管图像目录（configs.paths.RUNTIME_PHOTOS_DIR）保留张数，按文件名＝时间序滚动删旧

# ---- 视觉观测参数：**运行时可调**（runtime_files/vision_tunables.yaml，改完下一轮即生效）
VISION_STACK_FRAMES = 3            # 观测每轮拼接帧数的出厂默认（= 历来的硬编码 [-3:]）
VISION_OBSERVE_INTERVAL_MIN = 1.0  # 观测周期下限（秒）：一轮 VQA 本身耗时就以秒计，再短只是让轮次背靠背
VISION_OBSERVE_INTERVAL_MAX = 30.0 # 观测周期上限（秒）：再长就等于「几乎不看了」——那该用 --no-vision-observer
VISION_OBSERVE_RATE_LEVELS = {"low": 60.0, "medium": 5.0, "high": 2.0}

# ---- Observer — 状态观测
STATUS_WRITE_INTERVAL = 60.0       # runtime_files/robot_status.yaml 写盘周期（秒），整份覆盖写
STATUS_TICK_SEC = 1.0              # status_observer 主循环步进（秒）：每 tick 收一轮 ROS 消息，不能省
NETWORK_READ_TIMEOUT = 5.0         # status_observer 读无线状态的子进程超时（秒）
NAVIGATION_POLL_INTERVAL = 1.0     # 导航状态轮询间隔（秒）

# runtime_files/motion_status.yaml：运动状态只在状态改变时写，另按心跳周期补写
MOTION_STATUS_TICK_SEC = 0.2       # motion_status 主循环步进（秒）：每 tick 收一轮 ROS 消息
MOTION_STATUS_HEARTBEAT_SEC = 2.0  # 运动状态心跳写周期（秒）：状态没变也写一次，判活只看文件 mtime
MOTION_STATUS_ALIVE_MAX_AGE_SEC = 3 * MOTION_STATUS_HEARTBEAT_SEC   # 运动状态判活阈值（秒）
# 运动源静默超过这么久就当作「读不到」（秒）：业务判据，不是报告新鲜度 —— 读不到时不能编一个「空闲」出来
MOTION_SOURCE_STALE_SEC = 5.0
# 定位状态探测周期（秒）：motion_status 现问导航后端「底盘定位好了没」（HTTP，见 NAVIGATION_LOCALIZATION_STATUS_URL）。
# 它是**本文件里唯一按时间节流**的源（其余两段是话题订阅），故 navigation.localized 最多滞后这么久才落盘。
MOTION_LOCALIZATION_PROBE_SEC = 60.0
# 步态探测周期（秒）：motion_status 现问 robdog_control「此刻是什么步态」（只读服务，见 motion_status.SERVICE_GAIT_STATUS），
# 结果落在 motion.gait。比定位那条勤得多：步态会在任务中途被换掉，而这一问是**进程内 DDS 服务**，不是 HTTP。
MOTION_GAIT_PROBE_SEC = 5.0

# ---- 组件存活心跳：谁拥有的进程由谁写，status_observer 只读 mtime
# receiver 心跳周期（秒）：由 rclpy 定时器写，回调卡死时心跳一起停才是「receiver 不再转」的信号（不能改独立线程，理由见 receiver.py）
RECEIVER_HEARTBEAT_SEC = 5.0
RECEIVER_ALIVE_MAX_AGE_SEC = 3 * RECEIVER_HEARTBEAT_SEC   # receiver 判活阈值（秒）

# ========================================================== Waypoint Navigation — 定点导航
def load_supported_locations() -> tuple:
    """读 paths.SUPPORTED_LOCATIONS_FILE 里支持的地标名（读不到或格式不对就给空元组）。"""
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

NAVIGATION_BASE_URL = "http://127.0.0.1:8000"                               # 导航 app 的本机地址，下面几条 URL 都由它派生
NAVIGATION_GO_URL = f"{NAVIGATION_BASE_URL}/api/v1/navigation/go"           # 下发导航目标
NAVIGATION_STATUS_URL = f"{NAVIGATION_BASE_URL}/api/v1/navigation/status"   # 查导航状态
NAVIGATION_CANCEL_URL = f"{NAVIGATION_BASE_URL}/api/v1/navigation/cancel"   # 取消导航
NAVIGATION_LANDMARKS_URL = f"{NAVIGATION_BASE_URL}/api/v1/landmarks"        # 拉地标列表
# 查定位是否就绪（data.localized）；消费者是 Motion_Manager/motion_status.py 的 navigation.localized
NAVIGATION_LOCALIZATION_STATUS_URL = f"{NAVIGATION_BASE_URL}/api/v1/localization/status"
NAVIGATION_TIMEOUT = 300           # 导航工具的执行上限（秒），防止无限等待
NAVIGATION_PROBE_TIMEOUT = 2.0     # 探测导航状态的 GET 超时（秒），motion_status 与 status_observer 共用这一个
# 认得的导航状态白名单（枚举外的应答一律 unknown）：权威定义在服务端 codes/app/model/models.py 的 TaskStatus
NAVIGATION_STATUS_STATES = ('IDLE', 'RUNNING', 'SUCCESS', 'FAILED', 'CANCELLED')

# ================================================================ 云端 HTTP 服务
# 任务状态上报接口（值里的双斜杠是服务端现状，别顺手「修正」）
CLOUD_WRITE_STATE_URL = "http://36.137.127.46:8071//image_service-yanshi/write_state"
VISION_LOCAL_VQA_URL = "http://36.137.127.46:8070/test_vqa-yanshi"   # 视觉问答（VQA）接口

# ============================================================ Motion — move 工具速度参数
MOVE_LINEAR_SPEED = 0.6            # 平移速度（米/秒），技能建议 0.3-0.8
MOVE_ANGULAR_SPEED = 1.2           # 旋转速度（度/秒）
MOVE_90_DURATION = 1.6             # 旋转 90 度需要的时间（秒）
MOVE_MAX_LINEAR_DISTANCE = 10.0    # 平移距离上限（米）：超限的值被裁剪到上限（非拒绝），防止误传巨值失控
MOVE_MAX_ANGULAR_DEGREES = 720.0   # 旋转角度上限（度）：同样裁剪

# ---- 动作状态：/catch_turtle/action_state 的取值域（《机器人功能接口规格.md》第 3 节）
ACTION_STATES = {
    0: "初始化中，未执行动作",
    1: "正在执行动作",
    2: "静止站立",
    3: "静止趴下",
    4: "运动中",
    5: "切换步态中",
    6: "急停状态",
}

ACTION_STATE_STANDING = 2          # 静止站立
ACTION_STATE_LYING = 3             # 静止趴下
MOVING_ACTION_STATES = frozenset({1, 4, 5})                                   # 正在动：执行动作 / 运动中 / 切换步态
IDLE_ACTION_STATES = frozenset({2, 3})   # 静止：站立或趴下

# ---- 步态（《机器人功能接口规格.md》第 3 节「步态切换」）：action_type 的 sportMode 通道
# 本表是步态清单的**唯一来源**：gait 工具的合法取值（ToolExecutor 直接取它的键）与说明都在这儿，
# 别在工具里另抄一份。切换期间 action_state 会走 5（切换步态中），完成后回到 2（静止站立）。
GAIT_TYPES = {
    "AINimble": "全地形步态（AI 自适应，适用于复杂地形）",
    "AIClassic": "经典步态（AI 经典，日常行走推荐）",
    "run": "跑步步态（高速移动）",
    "traction": "牵引步态（被牵引时使用）",
}

# 调「等状态」类子进程的额外余量（秒）：子进程超时 = 逻辑上限 + 它，多留这几秒是为了让子进程的退出码能传出来
MOTION_TOOL_TIMEOUT_SLACK = 10.0
MOVE_STAND_TIMEOUT = 10.0                                                 # 趴着时先 standUp、再等它站起来的上限（秒）
MOVE_STATE_TOOL_TIMEOUT = MOVE_STAND_TIMEOUT + MOTION_TOOL_TIMEOUT_SLACK   # 调 check_action_state 的子进程超时（秒）

# ---- Motion_Manager：统一运动管理（/root/cmcc_agent/Motion_Manager）
MOTION_ACTION_END_CONSECUTIVE_FRAMES = 10   # 动作结束的消抖：终态要**连续收到这么多帧**才算数
# 进程内等状态的轮询步长（秒）。**只决定中止响应延迟**（stop_all 作废世代后，等状态那一步
# 最多过这么久才发现）：判据是按**帧数**算的（见 motion_status.ActionFrame），与步长无关，
# 所以它与 MOTION_STATUS_TICK_SEC 各是一个数——那个是订阅线程收消息的节奏，这里是我们看的节奏。
MOTION_WAIT_POLL_SEC = 0.1
MOTION_LYING_ACTIONS = frozenset({"getDown"})   # 以「趴下」收尾的动作
MOTION_STAND_ACTIONS = frozenset({"standUp"})   # 本身就是「站起来」的动作

# 动作「从下发到回到终态」的上限（秒），按整段动作时长分档；启动确认与结束等待共用同一条（都经 action_timeout 取）
MOTION_ACTION_TIMEOUT_SHORT = 15.0    # 短档：一碰就动的姿态切换
MOTION_ACTION_TIMEOUT_MEDIUM = 30.0  # 中档：成套动作，起势前有准备动作
MOTION_ACTION_TIMEOUT_LONG = 60.0    # 长档：整段舞蹈，序列最长
MOTION_ACTION_TIMEOUT_BY_ACTION = {
    "standUp": MOTION_ACTION_TIMEOUT_SHORT,       # 站立
    "getDown": MOTION_ACTION_TIMEOUT_SHORT,       # 趴下
    "greeting": MOTION_ACTION_TIMEOUT_SHORT,      # 打招呼
    "sitDown": MOTION_ACTION_TIMEOUT_MEDIUM,      # 坐下
    "stretch": MOTION_ACTION_TIMEOUT_MEDIUM,      # 伸懒腰
    "newYearCall": MOTION_ACTION_TIMEOUT_MEDIUM,  # 拜年
    "fingerHeart": MOTION_ACTION_TIMEOUT_MEDIUM,  # 比心
    "happy": MOTION_ACTION_TIMEOUT_MEDIUM,        # 开心
    "jumpForward": MOTION_ACTION_TIMEOUT_MEDIUM,  # 前跳
    "leap": MOTION_ACTION_TIMEOUT_MEDIUM,         # 扑人
    "walkUpsideDown": MOTION_ACTION_TIMEOUT_MEDIUM,  # 倒立
    "standErect": MOTION_ACTION_TIMEOUT_MEDIUM,   # 直立
    "handShake": MOTION_ACTION_TIMEOUT_MEDIUM,    # 握手
    "makeBow": MOTION_ACTION_TIMEOUT_MEDIUM,      # 鞠躬
    "twistBody": MOTION_ACTION_TIMEOUT_MEDIUM,    # 扭身
    "twistAss": MOTION_ACTION_TIMEOUT_MEDIUM,     # 扭屁股
    "shakeBody": MOTION_ACTION_TIMEOUT_MEDIUM,    # 抖身（快抖）
    "backflip": MOTION_ACTION_TIMEOUT_MEDIUM,     # 后空翻
    "turnOver": MOTION_ACTION_TIMEOUT_MEDIUM,     # 翻滚
    "twistJump": MOTION_ACTION_TIMEOUT_MEDIUM,    # 扭身跳
    "dance": MOTION_ACTION_TIMEOUT_LONG,          # 舞蹈
    "danceV2": MOTION_ACTION_TIMEOUT_LONG,        # 舞蹈（V2）
}
MOTION_MOVE_END_TIMEOUT = 60.0     # move 结束后等回到静止站立的上限（秒）
# 步态切换的上限（秒）：等启动与等终态共用这一条（与 action_timeout 的形状一样）。
# 单开一个而不是塞进 MOTION_ACTION_TIMEOUT_BY_ACTION：那张表是「动作名 → 时长」的，
# 步态是通道参数，混进去会让两件事看起来是一件事。
MOTION_GAIT_TIMEOUT = 15.0
MOTION_SCRIPT_TIMEOUT = 20.0       # 下发脚本的子进程超时（秒）：action 发一条 topic 就返回，move 的 client 会阻塞到运动结束
MOTION_MOVE_SCRIPT_TIMEOUT = MOTION_MOVE_END_TIMEOUT + MOTION_SCRIPT_TIMEOUT   # move 下发脚本的子进程超时（秒）

def action_timeout(action_type: str) -> float:
    """取某个动作的等待上限（秒）：启动确认与结束等待共用这一条。

    表已覆盖全部合法动作，兜底到短档只是防「新加了动作却忘了登记档位」——那时宁可
    早一点报「没观察到动作启动」，也不要按长档干等一分钟。
    """
    return MOTION_ACTION_TIMEOUT_BY_ACTION.get(
        action_type, MOTION_ACTION_TIMEOUT_SHORT
    )

MOTION_STOP_ALL_WAIT = 5.0         # 退出 / 终止时 stop_all 等运动停住的上限（秒）

# ---- 收进程（启动前清理 Communicator/startup_cleanup.py 与 start_agent 的退出收摊共用）
PROC_TERM_WAIT_SEC = 5.0           # SIGTERM 后等它自己退出的上限（秒），超时再 SIGKILL
PROC_KILL_WAIT_SEC = 2.0           # SIGKILL 后再等它消失的上限（秒），仍不消失就拒绝启动
EMERGENCY_ACTION = "emergencyStop"  # 急停动作名：全仓唯一字面量，只有 stop(emergency=True) 那一条路径能用它

# ---- 动作通道（RobdogAction 的 actiontype，见 skills/robot-dog-actions/SKILL.md）
# robot_action.sh 的第二个位置参数就是它（缺省 motorSkill）。与 EMERGENCY_ACTION 一样放在
# configs：通道名是接口字面量，别散在各个调用点上。
ACTION_CHANNEL_SKILL = "motorSkill"   # 预设单个动作（standUp / dance / ...）
ACTION_CHANNEL_GAIT = "sportMode"     # 步态切换，参数取 GAIT_TYPES 的键

# ============================================ 语音助手状态码 — /homi_speech/speech_assistant_status_topic
AUDIO_STATUS_NAMES = {             # 播报结束看 status=0（Idle）；8 是唤醒掐断了没播完的 TTS
                                   # 它同时是 tools.audio 三态里 available 的**全部**取值范围
                                   # （码表里有 → available；码表外 → unknown，即接口漂移）。
    0: "Idle(空闲)",
    1: "Running(运行中)",
    2: "AsrResponsed(收听中)",
    3: "AsrTextout(转录中)",
    4: "AsrEnd(收听结束)",
    5: "Aborting(对话中止)",
    6: "OfflineInstructionMatched(离线指令匹配)",
    7: "DisplayError(显示错误)",
    8: "UserAbort(用户中止)",
    9: "Wakeup(唤醒)",
}

# ================================================================ Interaction History
INTERACTION_HISTORY_MAX = 30       # load_interaction_history 最多保留的条数（截末尾 N 条）

# ================================================================ TTS / ASR 超时
ASR_LISTEN_WINDOW = 15             # ASR 收音窗口（秒）
TTS_WAIT_DONE_MAX_WAIT = 30        # wait_tts_done.py 等播报真正播完（status=0 Idle）的上限（秒）
TTS_TOOL_TIMEOUT = TTS_WAIT_DONE_MAX_WAIT + 5   # 调 run_tts_speak.sh 的子进程超时（秒）
ASR_TOOL_TIMEOUT = ASR_LISTEN_WINDOW + 5        # 调 run_asr.sh 的子进程超时（秒）
CLOUD_ANSWER_WAIT = 15             # speak_listen 等云端 answer 事件的窗口（秒）

# ========================================== bull_chase（斗牛）— 端侧状态等待
BULL_CHASE_STATUS_TIMEOUT = 60.0   # 单次等算法状态的窗口（秒）：等到 12（冲刺完成）才开下一轮，否则按超时收场
# 下发首条 ctl 前等算法订阅者上线的上限（秒）：DDS 发现异步，发太早报文会被丢；见到订阅者即刻返回
BULL_CHASE_PUBLISHER_TIMEOUT = 5.0
BULL_CHASE_VISION_RESIZE = (1280, 720)   # 斗牛送给视觉模型的图缩放尺寸（宽, 高）

# ================================================================ ROS 2 运行时环境
ROS_DOMAIN_ID = 2                  # ROS 2 domain id
RMW_IMPLEMENTATION = "rmw_cyclonedds_cpp"   # DDS 实现
CYCLONEDDS_URI = (                 # 网络接口：只走 eth0，组播发现走 spdp
    '<CycloneDDS><Domain><General><Interfaces><NetworkInterface name="eth0" '
    'priority="default" multicast="default" /></Interfaces>'
    '<AllowMulticast>spdp</AllowMulticast></General></Domain></CycloneDDS>'
)