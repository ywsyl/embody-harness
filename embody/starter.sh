#!/usr/bin/env bash
# =============================================================================
# starter.sh —— 一条命令起完整系统：后台是 start_agent.py 的组件，前台是多智能体角色系统
#
# 本脚本是这台机器上唯一的启动入口，一个进程里把两套东西拼起来，谁的启动逻辑都不重写：
#   后台（组件）  python3 start_agent.py cloud [组件侧开关]
#                 —— 状态观测器（独立子进程）+ 帧生产者 + 视觉观测器线程 + 运动状态观测器
#                    线程 + 云端收发桥（Communicator/receiver.py，把云端 SIGCEvent 转成
#                    rolebus 事件投给角色），即 start_agent.py 的**全部组件**，只是不进
#                    task_loop（本树里它已被角色系统取代）；
#   前台（角色）  scripts/role-daemon.sh --role <角色>（逐角色一个常驻 dsh 进程）
#                 —— interactor / orchestrator / supervisor 各一个 + 一个覆盖全部角色的
#                    role-watch，即 task_loop 那个单智能体循环的替代物。角色**怎么起**不在本
#                    脚本里重写：物化 profile、seed settings、不挂 headless 而叠
#                    <Role>/resident.patch.yml、解析模型 key、单实例守卫全在
#                    scripts/role-daemon.sh（角色的唤醒方式也在那里与 dsh-plugins/*/src 里）。
#
# 本脚本自己只做两件事：算 REPO_ROOT、**按下面的顺序调用那些步骤**。每一步的实现都在
# scripts/lib-starter.sh（环境、参数、校验、守卫、清收件箱、清单收尾、收尾设施、各启动步骤），
# 入口脚本上留下的就是顺序本身——实现混在里面时，顺序会被函数体淹掉。
#
# 为什么是「后台组件 + 前台角色」，而不是把角色塞进组件进程：角色的循环在 node 里由 dsh 引擎
# 驱动，组件的循环在 python 里（帧环与「最近一笔运动是谁下发的」都是进程内存，见
# start_agent.py 头部）；两者只能各在自己的进程里跑，靠文件（robot_status.yaml /
# vision_obs.yaml / motion_status.yaml）与 rolebus 事件交互。
#
# 组件侧的环境由本脚本自己备齐（start_agent.py 不自带环境准备）：三份 setup.bash
# （ROS / unitree_ros2 / 机器人运行时包，路径全取 configs/paths.py）、
# RMW_IMPLEMENTATION / CYCLONEDDS_URI / ROS_DOMAIN_ID（取 configs/configs.py）、LOCAL_*（环境优先，
# 其次 install/secret.env），备好后直接跑 start_agent.py。组件侧手工单跑
# （`python3 start_agent.py cloud`）时这套环境要自己 source（见 README「ROS2 环境」）——
# 两边同一口径：LOCAL_* 都不编默认值。
#
# 共享 DSH_HOME：组件侧的 receiver 要把云端事件投进**角色系统的 rolebus spool**（spool 是
# $DSH_HOME 的子目录），投错一份是静默失败（文件写下去了，没有进程会看到），所以本脚本按
# scripts/lib-roles.sh 的 role_system_dsh_home 算一次、export 给两个子系统。角色侧的「投分立版
# 还是合并版」判据同样读这份 DSH_HOME 下的角色标记（plugin_tools/rolebus.py 的 cloud_variant）。
#
# 落点：start_agent.py 从**自己所在目录**导入 configs/，所以它必须落在能同时看到 configs/ 的那
# 一级——历史上就是仓库根，本脚本也在仓库根（用 BASH_SOURCE 自己算根）。它对不上时当场报出布局
# 原因（否则表现是 python 里一句 ModuleNotFoundError: configs，看不出是布局问题）。
#
# 启动顺序（前六步零副作用，第七步起才碰东西；终端日志那一步只是写一份文件，夹在参数与落点校验之间）
# ----------------------------------------------------------------------------------------------
#   环境（ROS 三件套 + DDS + LOCAL_* + node/引擎自检）→ 参数 → 终端日志（本次拉起的这份）→
#   角色校验 → 共享 DSH_HOME → 单实例守卫 →
#   清收件箱 → 上一轮 executing 任务收尾 → 停了太久的 stopped 任务转历史 → 起组件
#   → 等组件健康 → 串行物化角色
#   profile 与 settings → 逐角色后台拉起（三个角色都立刻起；监督器的第一轮由它自己的定时器推迟
#   SUPERVISOR_FIRST_TICK_DELAY_SEC，等观测器写出本轮的观测，否则它第一轮读到的还是上一轮的账）
#   → 等角色健康 → 起一个总 watcher → 打就绪块 → 守望。
#   守卫（exit 3）与清收件箱刻意排在组件之前：前者被拒时不该已经拆掉在跑的那一套，后者要赶在
#   receiver 上线之前——它一上线就往 spool 投事件，物化 profile 那几十秒里投进来的信会被启动流程的
#   清收件箱删掉。（--force 那一路上守卫是**就地收掉在跑的角色**再往下走，同样排在组件之前：被收掉
#   的那套还在轮询同一份收件箱，收它要赶在后面的清收件箱与投递之前。）
#   两处任务动作（上一轮 executing 的收尾、停了太久的 stopped 转历史）紧挨着清收件箱、同样在起
#   组件之前：它们与「谁重开一局」无关（动的是清单本身），但要赶在**任何角色**被叫起来之前——
#   角色一醒就会读到清单。
#
# 清收件箱是一次**刻意反着 at-least-once 做**的事：rolebus 的收件侧重启后本来会补收收件箱里的
# 存量（dsh-plugins/rolebus/src/transport.ts 的 deliver），而角色的上下文随进程一起没了——上一轮
# 遗留的事件此刻多半指向一个已经不存在的任务，补收就是让角色被一封过期的信唤醒。只对
# configs/agents.py 的 RESET_ON_START_ROLES（「重开一局」的角色）清：表外角色的收件箱**正是它
# 续接上一轮的那个上下文**的配套，清了就成了「记得任务却等不到那封信」。同一张表也决定谁的会话
# 在拉起前被归档（scripts/role-daemon.sh），两件事绑在一处是刻意的。删哪些由收件侧的判据取反
# 决定，理由全在函数注释里。
#
# 生命周期（两边的死活互相牵连，收尾只有一个出口）
# ----------------------------------------------
#   · 组件意外退出 → 收掉角色系统并以非零码退出：角色的观测源没了，半个系统继续跑等于
#     让模型看着一份冻住的快照下判断，比不跑更糟；
#   · 角色系统自己结束（全部角色退出 / watcher 退出 / 启动失败）→ 收掉组件，退出码照传；
#   · 本终端的 Ctrl-C → 本脚本的 trap 收两边，退出码 130。三个角色是后台作业、按 POSIX
#     忽略 SIGINT，Ctrl-C 不会直接打到它们（本脚本显式收整棵进程树才收得掉）；
#   · 收尾顺序：**先角色、后组件**（报账顺序；TERM 同时发给两边，并行收）。两边的根进程都装了
#     自己的收尾路径（role-daemon.sh 收本角色的进程树、start_agent.py 收状态观测器与帧生产者），
#     所以各给一个 TERM 让它自己走完整流程（归档计数 / 逐轮记录都照打），等
#     ${KILL_GRACE_SECONDS}s 还没走完才强杀整棵树。
#   · 会话历史的归档不在这里：它属于「下一轮从哪儿开始」，挪在**该角色拉起之前**
#     （scripts/role-daemon.sh 的 reset_role_history），名单同样是 RESET_ON_START_ROLES。
#
# 参数路由（白名单，写在模式名之外；组件侧与角色侧同名参数不会打架，因为本脚本只有这一层解析）
# -----------------------------------------------------------------------------
#   组件侧（原样交给 start_agent.py）:
#     --no-frame-producer / --no-vision-observer / --no-status-observer / --no-motion-status
#     --no-tts-service                          不起常驻 TTS 服务（播报退回逐句脚本，且
#                                               robot_status.yaml 的 tools.audio 变成 unknown）
#     --no-receiver                             关掉云端收发桥（缺省开着：云端事件要靠它
#                                               转成 rolebus 事件才进得了角色系统）
#   两侧都用:
#     --force                                   收掉上一套再起，而不是拒绝启动：在跑的角色由本脚本
#                                               收（整棵进程树，见 scripts/lib-starter.sh 的
#                                               starter_stop_running_role），在跑的组件由 start_agent.py
#                                               自己的 --force 收（它还会先停运动）
#   角色侧（本脚本自己用）:
#     --roles "<角色 …>"                        缺省 interactor orchestrator supervisor
#     --dsh-home <目录>                          缺省 $REPO_ROOT/.dsh-dev/system
#     --from-start                              watcher 连已记录的回合一起重放
#     --no-watch                                终端不打印逐轮记录（Ctrl-C 仍收全套）
#     --roles-only                              只拉角色，**根本不起 start_agent.py**（不碰硬件）；
#                                               与上面的 --no-* 互斥（--force 例外：它两侧都认）
#
# 日志
# ----
#   本脚本（含它拉起的 watcher）打在终端上的每一行 →
#     runtime_files/logs/<拉起时刻>.log（YYYYmmdd-HHMMSS，一次拉起一份；终端照常打印，实现见
#     scripts/lib-starter.sh 的 starter_start_terminal_log）。
#   组件（含 start_agent.py 的自述行与三个进程内组件的输出）→
#     runtime_files/logs/starter_components.log（上一轮留 .prev）；状态观测器仍是它自己的
#     runtime_files/logs/status_observer.log（.prev 由 start_agent.py 轮换）。
#     **例外**：云端下发给本智能体的每条事件（receiver 照原文打的那行，正文以 configs 的
#     CLOUD_EVENT_LOG_TAG 开头）另外挑到终端上——组件输出整份落上面那份日志不变，只是同一份
#     输出里带这个标记的行再走一遍终端，因此也进本脚本的 <拉起时刻>.log。实现见
#     scripts/lib-starter.sh 的 starter_start_components。
#   角色：终端上的每一行由 role-watch 打，同时进 $DSH_HOME/role-watch.log；每个角色的启动输出
#     与 dsh 逐行输出在 $DSH_HOME/start-<role>.out 与 daemon-<role>.log（后者的 tee 在
#     scripts/role-daemon.sh 里）。
#
# 已知取舍（本组合特有，都不是本脚本引入的）
# ---------------------------------------
#   1) 角色侧的工具是**独立进程**（dsh 每次调用经 `python -m plugin_tools.bridge` 起一个），
#      取帧则走**取帧服务**（start_agent.py 拉起的 frame_producer.py --serve 子进程）：
#      角色侧进程经 FRAME_PRODUCER_SOCKET 向它要帧，所以 look（mode=front）/ search_old /
#      bull_chase 照常能取到帧 —— 不再需要 `--no-frame-producer --no-vision-observer` 那种
#      绕法（那条路已作废：关掉取帧服务后角色侧只能自拉生产者，代价是 vision_obs.yaml 停更、
#      监督器看不到视觉那一格）。取不到帧时的报错会指出是「服务不可达」还是「有人在抓拍但
#      帧环空」（frame_producer.describe_source，见 skills/vision/scripts/vision_dispatch.py）。
#      排查用 `python3 skills/camera/frame_producer.py --status`。
#   2) motion_status.yaml 的 last_motion 段（谁下发的这笔运动）读的是 MotionManager 的进程内
#      记录，而角色侧的运动走 motion.sh 脚本、不经过 MotionManager，所以这一段在角色侧永远是
#      「没有记录」——不是观测器坏了。段里其余字段（动作状态、步态、位姿、定位）是 ROS 实时订阅
#      与服务现读的，照常有效。
#   3) interactor 的 speak / speak_listen 调的是机器人上的语音服务（plugin_tools/audio_tools.py
#      → skills/tts-speak、skills/asr-listener）：后台角色照样真出声、真开麦。代价是它们会占住
#      这一轮——speak_listen 一直占到 ASR 收音窗口结束（configs.ASR_LISTEN_WINDOW）。
#   4) 某个角色启动即退出时：打印它的日志尾巴、其余角色继续跑、本脚本最终以非零码退出。半个系统
#      在跑会静默丢事件，这一条必须看得见，所以不做「静默降级成还能跑的那几个」。
#
# 用法：
#   ./starter.sh                                            组件全起 + 三件套角色
#   ./starter.sh --no-frame-producer --no-vision-observer    让角色侧的视觉工具能用（见取舍 1）
#   ./starter.sh --roles "interactor orchestrator"            换角色组合
#   ./starter.sh --dsh-home /tmp/lv6 --from-start             换共享 DSH_HOME / 重放逐轮记录
#   ./starter.sh --force                                     收掉上一套（角色与组件，整棵树）后重起
#   ./starter.sh --roles-only --dsh-home /tmp/lv6             只拉角色（不起 start_agent.py）
#   ./starter.sh --roles-only --force                         只拉角色，且先收掉在跑的那几个
#
# 可覆盖环境变量：与各部分同一套（EMBODY / ENGINE / NODE_BIN_DIR / DSH_HOME / WATCH_LOG /
# WATCH_FULL / LOCAL_* / ROS_DOMAIN_ID …）。本脚本自己用到的四个节奏值 START_STAGGER /
# START_CHECK_SECONDS / KILL_GRACE_SECONDS / WATCH_INTERVAL 登记在 configs/scripts-env.sh，
# 不在这里另写默认值。另一个节奏值 SUPERVISOR_FIRST_TICK_DELAY_SEC（监督器推迟第一轮的秒数）
# 不给人手调、由三份观测文件的产出周期推出来，事实源在 configs/configs.py，经 scripts/role-daemon.sh
# 换算成毫秒过桥给 dsh；本脚本只在本轮**没有观测器可等**（--roles-only / --no-status-observer）时
# 把它顶成 0。
# =============================================================================

set -euo pipefail

STARTER_SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(dirname "$STARTER_SELF")"
cd "$REPO_ROOT"

# shellcheck source=configs/lib-shell-paths.sh
source "$REPO_ROOT/configs/lib-shell-paths.sh"   # cmcc_path / cmcc_conf / cmcc_agents：路径、参数、角色表只认 configs/
# shellcheck source=configs/scripts-env.sh
source "$REPO_ROOT/configs/scripts-env.sh"       # 节奏参数 + node 运行时 / 引擎产物自检
# shellcheck source=scripts/lib-local-env.sh
source "$REPO_ROOT/scripts/lib-local-env.sh"     # local_env_value：模型 key 的取值口径
# shellcheck source=scripts/lib-roles.sh
source "$REPO_ROOT/scripts/lib-roles.sh"         # role_system_dsh_home / role_dir / role_all
# shellcheck source=scripts/lib-starter.sh
source "$REPO_ROOT/scripts/lib-starter.sh"       # 本文件全部步骤的实现（顺序在上面的「启动顺序」）

# 启动流水：一句一步。每一步的实现都在 scripts/lib-starter.sh，改行为改那里。
starter_setup_ros_env              # 起 ROS / unitree / 机器人运行时的 setup.bash，导出 DDS 三件套
starter_setup_model_env            # 取 LOCAL_*（环境优先，其次 secret.env），自检 node 与引擎产物
starter_parse_args "$@"            # 解析参数：角色、DSH_HOME、各类开关；不认识或互斥就拒
starter_start_terminal_log         # 本次终端输出同时落 runtime_files/logs/<拉起时刻>.log
starter_check_layout               # 确认 start_agent.py 与 configs/ 在同一级
starter_validate_roles             # 角色名已登记、profile 层齐全、合并版与分立版不同时跑
starter_resolve_dsh_home           # 算共享 DSH_HOME 并导出（组件与角色必须用同一份 spool）
starter_guard_single_instance      # 同一 DSH_HOME 下同角色已在跑，就拒绝启动
starter_clear_role_inboxes         # 清「重开一局」角色的收件箱（只删没被领走的）
starter_interrupt_executing_tasks  # 上一轮还停在 executing 的任务标成 interrupted
starter_expire_stopped_tasks       # 停了太久的 stopped 任务移入历史存档
starter_init_teardown              # 备好计数器与路径、装 trap（收尾的唯一出口）
starter_start_components           # 后台起 start_agent.py 的全部组件（含云端收发桥）
starter_materialize_roles          # 串行物化角色 profile 与 settings
starter_print_banner               # 打印启动信息（DSH_HOME、日志、投事件命令）
starter_launch_roles               # 逐角色后台拉起 scripts/role-daemon.sh
starter_check_roles_health         # 等几秒，看各角色进程是否还活着
starter_start_watcher              # 起一个覆盖全部角色的 role-watch
starter_print_ready                # 打「就绪」块
starter_watch_loop                 # 守望：组件或角色一退出就收整套

# 收尾并回退出码：STATUS 是角色侧这一轮的结论（更早的步骤失败时，退出码已就地给出）。
starter_cleanup
trap - EXIT
exit "$STATUS"
