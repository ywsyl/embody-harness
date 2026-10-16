"""
全局配置参数文件。

集中管理非路径类的可调参数，各模块从这里导入。
约定：**路径**常量登记在 configs/paths.py，**prompt 文案**登记在 configs/prompts.py，
与智能体绑定的表（工具白名单、任务/步骤状态枚举）登记在 configs/agents.py，
本文件只管通用参数，也不再转出它们。
"""

import json as _json
import math as _math
import re as _re
from pathlib import Path as _Path

# 供下面 load_supported_locations() 用；外部读者一律直接从 configs.paths 取
from configs.paths import SUPPORTED_LOCATIONS_FILE

# ================================================================ LLM — 大模型配置
# 模型服务参数（端点 / 密钥 / 模型名 / 请求头）不在本仓库里：每台终端各一份，落在
# install/secret.env（不入库），读者是 scripts/settings-seed.sh 与 scripts/role-daemon.sh
# ——两者都经 scripts/lib-local-env.sh 取值。别在这里再登记一份环境变量名或 token 数。

# ============================================================
# ---- Observer — 视觉观测
VISION_TIMEOUT = 10                # 一次视觉问答（VQA）请求的超时（秒）
# 调 visual 技能脚本（skills/vision/scripts/vision_dispatch.py）的子进程超时（秒）。
# 比上面那条大一档：一次调度里含取帧 + 多视角，不是一个 VQA 请求的预算。
VISION_SUBPROCESS_TIMEOUT = 120
VISION_MAX_ENTRIES = 10            # vision_obs.yaml 保留的最近观测条数
# 观测周期的出厂默认不在这里 —— 它是**档位表里的一档**（DEFAULT_INTERVAL，见下面「视觉观测参数」段）。


def vision_alive_max_age_sec(observe_interval: float) -> float:
    """vision 心跳的判活阈值（秒）= 3 × 一轮最长耗时（等帧 + 一次 VQA）。

    为什么是个函数而不是常量：观测周期是**运行时可调**的（覆盖层
    runtime_files/vision_tunables.yaml），阈值必须跟着**生效值**走 —— 否则把周期调慢到 6s，
    status_observer 仍按 3s 那档判活，观测器会被判成「视觉死了」（心跳周期本来就等于观测周期）。
    系数 3 是原来那条常量里的余量，别往下调。status_observer 经
    Orchestrator/vision_tunables.py 的 alive_max_age_sec() 现读它。
    """
    return 3 * (float(observe_interval) + VISION_TIMEOUT)


# 视觉帧源：周期抓拍生产者 skills/camera/frame_producer.py
FRAME_PRODUCER_MAX_FRAMES = 10     # 内存帧环保留的最近帧数（消费者错峰 / 追帧窗口）
FRAME_PRODUCER_CAPTURE_TIMEOUT = 1.0   # 单次抓拍超时（秒）
FRAME_PRODUCER_INTERVAL = 0.2      # 后台抓拍间隔（秒）——**固定值，不受运行时覆盖层影响**
                                   # 生产者是全体消费者共享的常驻取帧服务（观测器、视觉工具、任务
                                   # 主循环都从它的帧环取帧），就该一直以高频率抓帧、把帧环攒满；
                                   # 「LLM 能调的频率」调的是观测器多久来看一次，不是它抓得多快。

# 取帧服务的跨进程参数：帧环在服务进程（`frame_producer.py --serve`）的内存里，消费者经
# unix socket（configs.paths.FRAME_PRODUCER_SOCKET）取帧。三个值都与抓拍节奏无关。
# 客户端单次调用的等待上限（秒）。**一个值两处相关用途**：客户端连接/读写的超时；带
# timeout 参数的等待类调用（wait_for_new_frame 等）取「那个 timeout + 本值」作上限。
# 服务端**不设**每连接的 idle 超时（客户端可能几十秒才来一次，设了会掐掉它的长连接；
# 卡住的客户端只占一个 daemon 线程，客户端个数有限）。
FRAME_PRODUCER_RPC_TIMEOUT = 30.0
# 启动期「连上取帧服务」的重试窗口（秒）：服务是子进程，这个窗口要盖过 start_agent 的
# _CHILD_BOOT_GRACE_SEC(0.8s) 与模块导入时间，否则刚拉起的服务会被判成不可达。
FRAME_PRODUCER_ATTACH_TIMEOUT = 10.0
# 单条 blob（一帧 JPEG / 一张拼图）的 sanity 上限，两端都校验：坏长度当场报错，
# 而不是按它分配一段天量内存。10 帧 @1920×1080 拼起来也就几 MB，这里留足余量。
FRAME_PRODUCER_MAX_BLOB_BYTES = 64 * 1024 * 1024

# runtime_files/ 的体积清理：判据与实现在 Observer/cleanup.py（`--runtime-sweep`），
# 由 dsh-plugins/supervisor-loop 每轮执行一次（这是它唯一的调用点）。**按体积、不按份数**：
# 份数那套已经是别的机制的事（logs 有轮转），这里只兜「目录整体长胖了」。
# **两档水位**：目录超过 TRIGGER 就按 mtime 删最旧的合规文件，删到 <= TARGET 为止。留出这段落差
# 是刻意的——贴着单一上限删到「刚好不超」之后，下一轮只要再长一点就又触发，于是每轮都删一点；
# 有了落差就能一次删够、之后安静一阵（photos/ 这种持续写入的目录尤其明显）。
RUNTIME_LOGS_TRIGGER_MB = 5      # logs/：只删轮转下来的 <组件名>.log.prev（进程正持有的 *.log 不动）
RUNTIME_LOGS_TARGET_MB = 3
RUNTIME_TASKS_TRIGGER_MB = 5     # tasks/：只删 <task_id>.yaml 已终结存档（活的 pending_tasks 不动）
RUNTIME_TASKS_TARGET_MB = 2
RUNTIME_PHOTOS_TRIGGER_MB = 30   # photos/：只删 frame_producer 命名格式的受管图
RUNTIME_PHOTOS_TARGET_MB = 15

# 任务板：用户暂停/取消（status=stopped）的任务在待做清单里最多留多少小时。
# 交给了「先不做」的任务一直留在清单里等着被接着做，但**不该永远等**：清单正是交互器开机自查
# 要问的那批（plugin_tools/task_board.boot_check_tasks），一条永远停着的任务会让以后每一轮重启
# 都再问一次。超过本时限的由 starter.sh 拉起时移出清单、转成同目录的历史存档（只搬不删，
# 实现见 plugin_tools/task_board.expire_stopped_tasks，经 task_manager --expire-stopped 调用）。
# 72 小时＝三天。
TASK_STOPPED_EXPIRE_HOURS = 72

# ---- 视觉观测参数：**运行时可调**（runtime_files/vision_tunables.yaml，改完下一轮即生效）
VISION_STACK_FRAMES = 3            # 观测每轮拼接帧数的出厂默认（= 历来的硬编码 [-3:]）
# 观测频率的三档档位（档位名 → 观测周期秒数）。**它同时是「可调区间」的唯一来源**：区间 = 这张表
# 的最小/最大值（`vision_tunables._observe_interval_bounds()` 现算），不另立上下限常量。
# 2026-09-24 之前另有一对独立的 `VISION_OBSERVE_INTERVAL_MIN/MAX = 1.0/30.0`，于是「档位 ⊆ 区间」
# 这条只能靠人记住 —— `low`=60 落在区间外，写侧直接拒（LLM 选 low 得到「observe_interval=60.0
# 超出范围，可调 1~30 秒」、值不变），那一档等于不存在，而 CLI 回显、README、reference/status.md
# 都在宣称它可用：两张表没有任何一处同时看，就漂过去了。现在区间随表走，漂不动。
# **改档位值就是在改可调区间**：上限从 30s 抬到 60s（原先那条「再长就等于几乎不看了 —— 那该用
# --no-vision-observer」的护栏随之消失，要拦就得改这张表本身）、下限从 1s 抬到 2s。
# 连带效应：观测周期越长，监督器「vision_obs.yaml 末条 timestamp 明显落后于抬头」那条判据越难判
# —— 它不知道周期是多少，看到的只是「60s 没更新」。**同日已收口**：判活阈值
# vision_alive_max_age_sec() 的现算值直接渲染进 robot_status.yaml 的 tools.vision.status 注释里
# （见 Observer/status_observer.py 的 render），监督器照那个秒数比 updated_at 即可；
# Supervisor/prompts/supervisor.md 的两条判据也改成「别按固定秒数认落后」。
VISION_OBSERVE_RATE_LEVELS = {"low": 60.0, "medium": 5.0, "high": 2.0}
# 观测周期的**出厂默认 = 档位表里的一档**（2026-09-24 用户定：取 low），按档位名取、不写秒数：
# 默认值与档位同源就不会再脱钩 —— 此前它是写死的 3（旧表里 medium=3 恰好等于它），改了档位表之后
# 默认值就成了表外的「自定义值」，回显只能说「对不上任何档位」。**它的值因此是 60s**：
# 出厂默认看得最稀，任务里要看紧一点由 LLM 用 vision_tunables 工具调到 medium / high。
# 改档位表的 low 就等于改出厂默认 —— 这里不再有第二个秒数。
# 消费者：等下一帧的超时 + 本轮间隔（Observer/vision_observer.py 的主循环）、vision_observer
# --interval 的缺省、覆盖层缺 observe_interval 时的回落（Orchestrator/vision_tunables.py）。
DEFAULT_INTERVAL = VISION_OBSERVE_RATE_LEVELS["low"]

# ---- Observer — 状态观测
STATUS_WRITE_INTERVAL = 60.0       # runtime_files/robot_status.yaml 写盘周期（秒），整份覆盖写
STATUS_TICK_SEC = 1.0              # status_observer 主循环步进（秒）：每 tick 收一轮 ROS 消息，不能省
NETWORK_READ_TIMEOUT = 5.0         # status_observer 读无线状态的子进程超时（秒）
# 地理位置（robot_status.yaml 的 location 段）的两条外部通路：先问本机公网 IP，再拿它反查归属地。
# URL 与字段名都是**外部契约**（ifconfig.me 返回一行纯文本 IP；ipwho.is 返回 JSON，见
# status_observer.read_location），别在别处再抄一份。curl 路径在 configs.paths.CURL_BIN。
PUBLIC_IP_URL = "https://ifconfig.me"
GEO_IP_URL_TEMPLATE = "https://ipwho.is/{ip}"
# 上面每条 curl 的超时（秒）：一轮要跑两条，故最坏多等 2×它。**10s 不是随手定的**——
# 2026-09-23 在本机实测：ifconfig.me 单次 0.75~3.7s、ipwho.is 单次 0.66~7.7s（同一条链路抖动很大），
# 5s 那档会把大部分成功请求掐成超时。真出现「老是超时」就先量一遍再改这个数。
LOCATION_READ_TIMEOUT = 10.0
NAVIGATION_POLL_INTERVAL = 1.0     # 导航状态轮询间隔（秒）

# robot 段那两条源时间戳（battery_updated_at / device_updated_at）的判失效阈值（秒）：只被
# Supervisor/prompts/supervisor.md 按名引用，Python 侧无消费方 —— 判定发生在监督器读
# robot_status.yaml 时。**这两条戳的分辨率就是写盘周期**：它们是「**写盘那一刻**该源最后收到
# 消息的时刻」，两次写盘之间任何一次采样看到的年龄都从 0 涨到 STATUS_WRITE_INTERVAL，所以阈值
# 低于 2×STATUS_WRITE_INTERVAL 会把活着的源整轮整轮误判成断流 —— 2026-09-24 实测：此前那档
# 按源频率定的 5s/30s 让监督器每轮都报「断连」，交互器对着用户重复播报「电池监测离线了」，
# 而同一份快照里的 battery_updated_at 每 60s 都跟着写盘往前走 1s 以内（源在正常 1Hz 推）。
# 判据 = 年龄 > 它。代价是检出最慢滞后 2 个写盘周期；观测器自己挂了也会满足它，先看下面那条 mtime。
STATUS_FEED_STALE_SEC = 2 * STATUS_WRITE_INTERVAL

# runtime_files/motion_status.yaml：运动状态只在状态改变时写，另按心跳周期补写
MOTION_STATUS_TICK_SEC = 0.2       # motion_status 主循环步进（秒）：每 tick 收一轮 ROS 消息
MOTION_STATUS_HEARTBEAT_SEC = 2.0  # 运动状态心跳写周期（秒）：状态没变也写一次，判活只看文件 mtime
MOTION_STATUS_ALIVE_MAX_AGE_SEC = 3 * MOTION_STATUS_HEARTBEAT_SEC   # 运动状态判活阈值（秒）
# 运动源静默超过这么久就当作「读不到」（秒）：业务判据，不是报告新鲜度 —— 读不到时不能编一个「空闲」出来
MOTION_SOURCE_STALE_SEC = 5.0
# 定位状态探测周期（秒）：motion_status 现问导航后端「底盘定位好了没」（HTTP，经 plugin_tools/nav_tools.localization_status）。
# 它是**本文件里唯一按时间节流**的源（其余两段是话题订阅），故 navigation.localized 最多滞后这么久才落盘。
MOTION_LOCALIZATION_PROBE_SEC = 60.0
# 步态探测周期（秒）：motion_status 现问 robdog_control「此刻是什么步态」（只读服务，见 motion_status.SERVICE_GAIT_STATUS），
# 结果落在 motion.gait。比定位那条勤得多：步态会在任务中途被换掉，而这一问是**进程内 DDS 服务**，不是 HTTP。
MOTION_GAIT_PROBE_SEC = 5.0

# ---- 组件存活心跳：谁拥有的进程由谁写，status_observer 只读 mtime
# receiver 心跳周期（秒）：由 rclpy 定时器写，回调卡死时心跳一起停才是「receiver 不再转」的信号（不能改独立线程，理由见 receiver.py）
RECEIVER_HEARTBEAT_SEC = 5.0
RECEIVER_ALIVE_MAX_AGE_SEC = 3 * RECEIVER_HEARTBEAT_SEC   # receiver 判活阈值（秒）

# ---- 云端下发事件上屏：Communicator/receiver.py 写、starter.sh 按它筛
# receiver 每收到一条云端下发的 robot_agent_task（create/modify/interrupt/info/answer…）就把 body
# 原文打一行，正文自带本前缀；starter.sh 用它在组件日志里挑出这几行打到终端
# （scripts/lib-starter.sh 的 starter_start_components）。**它就是两侧的接口**：一边 python（直接
# import）、一边 shell（经 configs/lib-shell-paths.sh 的 cmcc_conf 取值）——各抄一份必有一处漂移，
# 所以落在这里，而不是两个文件里各写一个字面量。
CLOUD_EVENT_LOG_TAG = "[Receiver] 📨 云端下发"

# ---- 快照文件渲染：三份 yaml 的写法必须一致
# 消费者是 Supervisor 与 LLM —— 它们按原文读 robot_status.yaml / motion_status.yaml，
# 只在「时间戳都长一个样」时才能直接比大小、比先后。所以格式与注释列都只登记在这里，
# 写入方（Observer/status_observer.py、Motion_Manager/motion_status.py、
# Motion_Manager/motion_record.py）一律取这几条。
# 注意下面这条**不同于** status_observer 里给 /proc/net/wireless 等外部读数的那些格式：
# 那些是别处给的原文，这里是我们自己渲染快照时的格式。
SNAPSHOT_TS_FMT = '%Y-%m-%d %H:%M:%S'   # 固定零填充，字典序 = 时间序；别改成 12 小时制或去掉零填充
# 日期 + 星期，只给 robot_status.yaml 的 system.date 用。%A 是**英文**星期全名（Wednesday）：
# 本进程不调 locale.setlocale，strftime 停在 C locale，不受 LANG/LC_* 影响 —— 读方（LLM、正则）
# 都按英文名读它，所以要往进程里加 setlocale 时先想这件事，否则这个值会变成中文星期名。
# 前半段 %Y-%m-%d 与 SNAPSHOT_TS_FMT 的日期部分逐字一致，两组值可以直接比。
SNAPSHOT_DATE_FMT = '%Y-%m-%d %A'
SNAPSHOT_COMMENT_COL = 40               # 行尾注释列的起始列（值短的行补空格对齐，读起来是一张表）

# =============================================================================
# Supervisor — 监督器定时观察
# =============================================================================
# 下面六个值有两条消费路径，改这里两条一起变：
#   1) dsh 侧：scripts/role-daemon.sh 经 `configs_value` 取出来放进进程环境，再由
#      Supervisor/resident.patch.yml 与 Supervisor/cordis.patch.yml 的 `!!js process.env.*`
#      读回去（cordis 的配置层读不到 Python，进程环境是那两者之间的唯一通道）。
#   2) Python 侧：直接 import。
# 监督器每隔 N 秒读一遍观测文件（runtime_files/vision_obs.yaml、robot_status.yaml）。
SUPERVISOR_INTERVAL = 10   # 秒

# 上下文预算。**只有一个可调的参数**：整个请求（系统提示词 + 工具 + 历史）到
# SUPERVISOR_CONTEXT_CEILING 这个 token 数就开始压缩；压缩后保留多少原文由它**现算**——
# 扣掉固定开销余量，见 supervisor_compact_retain_tokens()。不再单列「保留几轮」的口径
# （2026-10-12 用户定：只留上限一个参数；此前那套「保留最近 N 轮」已删）。
# 体量是实测的，不是估的（2026-10-12 在监督器活会话上量）：三份观测原文约 16KB ≈ 4000 token/轮，
# 系统提示词 + 工具另占约 2.5k。压缩阈值比的是**整个请求**，所以保留预算要从上限里扣掉这块
# 固定开销再留余量——否则压完一轮还压在阈值上，下一跳又触发。
SUPERVISOR_CONTEXT_CEILING = 30000        # 总上下文上限（token），到这儿开始压缩
# 压缩后给固定开销留的余量。实测系统提示词 + 工具约 2.5k，这里取 5k：既保证保留预算严格小于
# 上限（引擎装载时校验 retainTokens < thresholdTokens），又让压完一轮落回阈值以下、不会每跳都压。
SUPERVISOR_COMPACT_RESERVE_TOKENS = 5000
SUPERVISOR_SESSION_MAX_MB = 20      # 单份会话日志到多少 MB 就轮转换成新的一份。

# 监督器的**首轮延迟**（秒）：从它本进程的定时器装配好那一刻算起，头这么久里的每一跳都丢掉，
# 让三份观测文件先写出**本轮的**产出，监督器再开始读。
# 取三份里最长的产出周期：robot_status.yaml 的 STATUS_WRITE_INTERVAL（60s，恒为最大项）、
# vision_obs.yaml 的观测周期（只能取档位表的最大值 —— 生效值在观测器进程里，这里的消费方读不到）、
# motion_status.yaml 的 MOTION_STATUS_HEARTBEAT_SEC。
# 为什么不是「写出第一份就够」：观测器的第一份快照在启动后约 1s 就落盘，那一刻各组件的首笔痕还没写
# （receiver 心跳等定时器第一个周期、视觉心跳等首帧 + 一次 VQA），留下的还是**上一轮**的痕 ——
# 2026-09-24 14:23 实测：监督器读到它报「通信链路中断：receiver 心跳最后更新于 11:25:55（距今约 3
# 小时）」，一分钟后又报「通信链路恢复」。等满一个写盘周期，第二份快照写出，第一轮读到的才是新鲜账
# （各组件首笔痕最晚 +13s 就落齐，都在这个窗口里）。
# 这一等从**监督器的定时器装配时刻**起算，而那已经晚于观测器的第一份快照（组件健康检查 + profile
# 物化 + 前几个角色的 stagger + dsh 会话建立），再叠上它自己的第一个间隔（SUPERVISOR_INTERVAL）
# ——所以一个周期就够，不需要两个。
# **2026-09-24 改过位置**：原先它是启动脚本里的 `sleep`（拉 supervisor 之前等），那一段排在角色拉起
# 循环里，谁先起谁后起都被它绑住；现在延迟落在监督器自己的定时器里
# （dsh-plugins/supervisor-loop 的 firstTickDelayMs），三个角色的进程照常立刻起来，被推迟的只有
# 监督器问模型的第一次。消费路径与其余 Supervisor 值同一条：role-daemon.sh 经 `configs_value`
# 换算成毫秒进进程环境，Supervisor/resident.patch.yml 用 `!!js process.env.*` 读回去。
SUPERVISOR_FIRST_TICK_DELAY_SEC = max(
    STATUS_WRITE_INTERVAL,                     # robot_status.yaml 写盘周期
    # max(VISION_OBSERVE_RATE_LEVELS.values()),  # vision_obs.yaml 观测周期（= 档位表的最大值）
    MOTION_STATUS_HEARTBEAT_SEC,               # motion_status.yaml 心跳写周期
)

def supervisor_context_window() -> int:
    """
    路由声明的上下文窗口（token）。

    压缩的触发点（thresholdRatio）只有「占窗口的比例」一种写法，所以要从窗口换算。窗口的
    事实源是 configs/settings-model.yaml 的 contextWindow，这里按文本取那个键：取不到就报错，
    而不是退回某个默认窗口——那会让阈值悄悄变一个量级。

    取的是**第一个**匹配（各条路由都声明同一个窗口；模板里那条「关思考」的镜像路由是后面
    的第二处）。往模板里加路由时，各条的 contextWindow 必须同值，否则这里读到的是第一条、
    而别的角色按另一条发的请求——那正是本函数不想发生的「阈值悄悄变一个量级」。
    """
    path = _Path(__file__).with_name("settings-model.yaml")
    match = _re.search(r"^\s*contextWindow:\s*(\d+)\s*$", path.read_text(encoding="utf-8"), _re.M)
    if match is None:
        raise ValueError(f"settings-model.yaml 里找不到 contextWindow：{path}")
    return int(match.group(1))

def supervisor_compact_retain_tokens() -> int:
    """
    压缩后保留的原文预算（token），从上限现算。

    保留不是第二个可调参数：用户只看 SUPERVISOR_CONTEXT_CEILING ——到上限就压，压完留到
    「上限减固定开销余量」（SUPERVISOR_COMPACT_RESERVE_TOKENS）。余量是必须的：引擎装载时
    校验 retainTokens < thresholdTokens（thresholdTokens 就是上限），而压完一轮若还压在阈值
    上，下一跳又触发、压缩等于没做。
    """
    return SUPERVISOR_CONTEXT_CEILING - SUPERVISOR_COMPACT_RESERVE_TOKENS

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

def supported_locations_text() -> str:
    """
    支持地点清单的**注入正文**：每行一条 `- 名字`（读不到就是空串，调用方按空处理）。

    只有渲染，没有第二份抽取：名字仍只由上面的 load_supported_locations() 决定。每轮注入它的那条路
    是 dsh-plugins/location-context（交互器与监督器），正文经 scripts/role-daemon.sh 过桥进进程环境；
    load_supported_locations() 的另一条路是 waypoint_nav 的工具说明（plugin_tools/bridge.py），
    两边取的是同一份数据。
    """
    return "\n".join(f"- {name}" for name in load_supported_locations())

# 导航 app 的 URL 组（BASE_URL 与派生出来的 go/status/cancel/landmarks/localization/relocalize）
# 在 configs/paths.py —— URL 类常量归那里，这里只留调用它们的参数（超时/轮询）。
NAVIGATION_TIMEOUT = 300           # 导航工具的执行上限（秒），防止无限等待
NAVIGATION_PROBE_TIMEOUT = 2.0     # 探测导航状态的 GET 超时（秒），motion_status 与 status_observer 共用这一个
# 工具层调导航 HTTP 的等待上限（秒）：waypoint_nav 一次调用里的定位查询 / 自动重定位 / 发起导航 /
# 轮询状态四条请求共用它。三条超时**故意不同值**，别合并：探针是后台高频调用，超时当「读不到」；
# 控制类调用（下一条）等的是「这一步到底成没成」，宁可多等两秒也不要误报失败；工具层等的是整条
# 流程能不能推进（含算法侧可能要算一会儿的自动重定位），再宽一档。
NAVIGATION_REQUEST_TIMEOUT = 10.0
# 下发给导航 app 的控制类请求（查地标 / 查状态 / 取消导航）的 GET/POST 超时（秒），见 plugin_tools/nav_tools.py。
NAVIGATION_CONTROL_TIMEOUT = 5.0
# 认得的导航状态白名单（枚举外的应答一律 unknown）：权威定义在服务端 codes/app/model/models.py 的 TaskStatus
NAVIGATION_STATUS_STATES = ('IDLE', 'RUNNING', 'SUCCESS', 'FAILED', 'CANCELLED')

# ================================================================ 云端服务
# 任务状态上报**不走 HTTP**：经本体信令的 ROS 服务 `/homi_speech/sigc_data_service` 下发，
# 服务名在 configs/paths.py 的 ROS_SIGC_DATA_SERVICE（地址在 paths、参数在这里）。
VISION_LOCAL_VQA_URL = "http://36.137.127.46:8070/test_vqa-yanshi"   # 视觉问答（VQA）接口
CLOUD_REPORT_TIMEOUT = 5.0         # 经信令服务上报一次的总超时（秒）：等就绪 + 等应答共用这一个预算，见 Communicator/sender.py

# ============================================================== 天气查询 — open-meteo
# 天气工具（plugin_tools/weather.py，工具名 weather）的两个外部接口。**URL 与字段名都是
# 外部契约**（open-meteo 的公开 API），别在别处再抄一份：预报接口的 current/daily 字段集、
# geocoding 的 results 字段名都写在那边的模块里，改接口只动那一处。
WEATHER_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"          # 当前天气 + 逐日预报（timezone=auto）
WEATHER_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search" # 地名 → 经纬度
# 上面每条 curl 的超时（秒），与 LOCATION_READ_TIMEOUT 同值同理由：2026-09-28 在本机实测，
# 两个接口的稳定耗时约 1s，但这条外网链路会抖（同一天量到过 8s 才回、也量到过整批超时），
# 5s 那档会把一部分本来能成功的请求掐成超时。走 curl -4 不用 requests 的理由见 weather.py 的 _CURL_ARGS。
WEATHER_REQUEST_TIMEOUT = 10.0

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

# ---- Motion_Manager：统一运动管理（Motion_Manager/，跑在 agent 同进程内）
# 状态判据的时间基准（2026-10-08 由「终态要连续收到 N 帧」改来：帧数是字面量、又跟着话题频率漂，
# 读代码看不出到底要求连续静止多久；现在时长与检测频率都在这，命中次数由它们算出来）。
MOTION_IDLE_HOLD_SEC = 0.2          # 「静止」要**连续保持这么久**才算数（秒）
MOTION_STATE_CHECK_HZ = 5.0         # 状态检测频率（Hz）：等状态时每 1/HZ 秒读一次常驻订阅的缓存帧
# 连续命中次数：窗口两端各要点一次 → ceil(0.2 × 5Hz) + 1 = 2 次。改时长或改频率，它自己跟着走。
# 它同时也是中止响应延迟的上限（stop_all 作废世代后，等状态那一步最多过 1/HZ 秒才发现）。
MOTION_IDLE_HITS = _math.ceil(MOTION_IDLE_HOLD_SEC * MOTION_STATE_CHECK_HZ) + 1
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
    1: "Running(对话进行中)",
    2: "AsrResponsed(收听中)",
    3: "AsrTextout(转录/播报中)",
    4: "AsrEnd(ASR 结束)",
    5: "Aborting(对话中止)",
    6: "OfflineInstructionMatched(离线指令匹配)",
    7: "DisplayError(显示错误)",
    8: "UserAbort(用户中止)",
    9: "Wakeup(唤醒)",
    11: "静默等待",                 # 机器人回复完用户、用户没开启新一轮对话时的静默态；
                                   # 此时继续等 x 秒仍无指令，端上认为这轮对话结束 → 0
}

# ================================================================ 语音工具超时
ASR_LISTEN_WINDOW = 15             # ASR 收音窗口（秒）。读者：唤醒链路（skills/asr-listener）的默认
                                   # 超时；设备自己那一轮收音（≈16s）也是它，故下面 audio_interact
                                   # 的上限估算拿它当这一段
TTS_WAIT_DONE_MAX_WAIT = 30        # 等播报真正播完（status=0 Idle）的上限（秒）。读者：回退路径的
                                   # wait_tts_done.py 与常驻服务 TTS_Manager/tts_service.py 的
                                   # 看门狗（判定代码同一份，见 TTS_Manager/speech_status.py）
TTS_TOOL_TIMEOUT = TTS_WAIT_DONE_MAX_WAIT + 5   # 播报这一类调用的上限（秒）：调 run_tts_speak.sh
                                   # 的子进程超时、tts_client 等 drained 的缺省上限
TTS_SPEAK_CALL_TIMEOUT = 10        # 常驻服务（TTS_Manager/tts_service.py）单次 play_tts 调用的
                                   # 应答上限（秒）。**必须有**：等不到的应答若不判超时，那一句会
                                   # 永远挂在「在飞」里，而「队列见底」（drained）要求它为空 ——
                                   # 一次丢应答就会让 drained 再不上报。取 10s：稳态实测在毫秒级，
                                   # 它只是「服务还在不在」的兜底，不掺进播报节奏
ASR_TOOL_TIMEOUT = ASR_LISTEN_WINDOW + 5        # 调 run_asr.sh 的子进程超时（秒）。读者：工具层的
                                   # listen / speak_listen（audio_tools._run_asr）。比收音窗口多的 5s 不是余量而是
                                   # 脚本自己的收尾：asr_monitor 的 trigger_wait 1s + abort_session ≤2s
CLOUD_ANSWER_WAIT = 15             # 云端 answer 事件的等待窗口（秒）。**当前没有消费方**：原读它的
                                   # listen / speak_listen 已改回本地 ASR（见 plugin_tools/audio_tools.py
                                   # 里注释掉的 _await_cloud_answer），写侧也自 2026-09-28 起停用
                                   # （Communicator/receiver.py 的 answer 两条腿）。恢复云端 answer 链路时
                                   # 与它一起放开
WAKEUP_REPLY_MUTE_WAIT = 2.0       # 主动唤醒时把唤醒回应（"在呢"）临时静音多久（秒），须盖过最长
                                   # 回应音频 1.5s。读者：skills/asr-listener 的 run_asr.sh 与
                                   # skills/audio_interact（后者先播报、后静默唤醒，静音从发出
                                   # Wakeup 那一刻起算）
INPUT_WAIT = 120                   # speak_input 等终端输入一行的窗口（秒）。比云端窗口长得多：
                                   # 它的对手是坐在终端前的人，不是 ROS 上的服务

# ============================ audio_interact 自己的等待（播报 + 静默唤醒 + 等状态收尾）
# 这个工具一趟 = 播报（复用 tts-speak）→ 静默唤醒（复用 asr-listener）→ 等设备侧 ASR 回话。
# 前两段用的常量在上一节（与那两个技能共用，不在这儿抄一份）；这一节只放它那条状态判据自己的等待。
AUDIO_INTERACT_REPLY_WAIT = 3      # 还没听到用户说话时的静默窗口（秒）：设备回到静默等待/空闲
                                   # （11/0 —— 很可能只是它那句回应刚播完）后，等用户开口这么久；
                                   # 窗口内每收到一次「对话有动静」（1/2/3/9）就从头计时。
                                   # 收到最终 ASR（status=2 finish=true）就直接收工，不走这个窗口
AUDIO_INTERACT_MAX_WAIT = (TTS_WAIT_DONE_MAX_WAIT + WAKEUP_REPLY_MUTE_WAIT + ASR_LISTEN_WINDOW
                           + AUDIO_INTERACT_REPLY_WAIT)
                                   # audio_interact 整个工具的上限（秒），就是上面四段加起来（播报 /
                                   # 静默那 2.5s / 设备自己那一轮收音 / 等用户开口）。正常情况收下
                                   # 用户回话就收工，这里只是兜底：一直没人说话时也得退出
AUDIO_INTERACT_TOOL_TIMEOUT = AUDIO_INTERACT_MAX_WAIT + 10
                                   # 工具层调 audio_interact.sh 的子进程超时（秒）。这 10s 不是余量而是
                                   # MAX_WAIT 之外的固定开销：source ROS 环境与运行安装、rclpy 起、
                                   # 等状态发布者（≤3s）、退出时调 abort（≤2s）。给紧了就会在
                                   # 脚本还正常干活时被 SIGKILL —— 那种死法连 EXIT trap 都跑不到

# ============================================== 交互器流式播报（dsh-plugins/speech-stream）
# 播报稿来自 speak / speak_listen / speak_input 的 text 参数（**不是模型正文**），插件在这些
# 参数还在逐片到达时就把已经成句的部分送进 TTS：首句出声从「整段生成完 + 派发 + 调服务」提前到
# 「第一句生成完」。工具侧按插件留下的记录去重（configs/paths.py 的 RUNTIME_SPOKEN_STREAM_FILE）。
# 开不开这个插件是**开关**，登记在 configs/agents.py 的 SPEECH_STREAM_ENABLED（与智能体绑定的
# 开关都在那边）；本节的这两个是它生效后的调参，是通用参数，留在这里。
SPEECH_MAX_CHARS_PER_CHUNK = 60    # 软边界（字数）：一句长到这里还没说完，就在最近的逗号处断开
                                   # ——不让一句长话把出声拖到最后。有句末标点就按标点断
SPEECH_ITEM_STALE_SEC = 120        # 工具侧忽略多旧的播报记录（秒）：过期 = 当没有记录 → 整段播
SPEECH_QUEUE_DRAIN_MAX_WAIT = SPEECH_ITEM_STALE_SEC
                                   # 工具等插件那条逐句队列见底的上限（秒，读者
                                   # audio_tools._wait_streamed_done）。这是**整段**的上限而不是
                                   # 一句的：整段耗时 = 各句之和（句数随文本长度走，上限便无从
                                   # 预估）。取记录有效期同值：记录过了 120s 就被当成过期，再等
                                   # 下去读到的也不是它了
SPEECH_DELIVERY_MODE = "serial"    # 插件交下一句的时机（读者：dsh-plugins/speech-stream 的
                                   # Config.deliveryMode）。serial = 等这一句播完（常驻服务广播
                                   # drained）再交下一句。**默认必须是它**：设备没有队列 —— 每来
                                   # 一条新 play_tts 请求，speech_core 都先 `speech app stop`
                                   # 停掉在播的那条再 runInquiry（2026-10-12 15:37~15:39 实测
                                   # 三次：前一句分别在 75/96/185ms 处被后一句掐断 —— 用户听到
                                   # 的就是「只播了后半句」）。pipeline = 收到受理就交下一句、
                                   # 指望设备排队连播；句间只多设备侧那 0.46s，但它会掐断前一句，
                                   # 只有设备改成真排队才成立

# ========================================== bull_chase（斗牛）— 端侧状态等待
BULL_CHASE_STATUS_TIMEOUT = 60.0   # 单次等算法状态的窗口（秒）：等到 12（冲刺完成）才开下一轮，否则按超时收场
# 下发首条 ctl 前等算法订阅者上线的上限（秒）：DDS 发现异步，发太早报文会被丢；见到订阅者即刻返回
BULL_CHASE_PUBLISHER_TIMEOUT = 5.0
BULL_CHASE_VISION_RESIZE = (1280, 720)   # 斗牛送给视觉模型的图缩放尺寸（宽, 高）
BULL_CHASE_VISION_AUDIO = "大朋友小朋友们小心，我要开始冲刺啦！"

# ================================================================ ROS 2 运行时环境
ROS_DOMAIN_ID = 2                  # ROS 2 domain id
RMW_IMPLEMENTATION = "rmw_cyclonedds_cpp"   # DDS 实现
CYCLONEDDS_URI = (                 # 网络接口：只走 eth0，组播发现走 spdp
    '<CycloneDDS><Domain><General><Interfaces><NetworkInterface name="eth0" '
    'priority="default" multicast="default" /></Interfaces>'
    '<AllowMulticast>spdp</AllowMulticast></General></Domain></CycloneDDS>'
)
