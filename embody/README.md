# cmcc_agent
端侧智能体（机器人）任务执行系统。

# 入口：
```bash
cd <仓库根>                     # 下列路径一律相对仓库根
bash starter.sh                 # 完整系统：后台组件（含云端桥 receiver）+ 前台多智能体角色系统
python3 start_agent.py cloud    # 只起组件 + 云端桥，不起角色系统（纯组件侧联调）
```
后一条的环境要自己先备好（`starter.sh` 那条由脚本自己备齐），见下方「组件侧入口」。
启动后唤醒机器人，对它口述一个长程任务。**收到 `"action":"update"` 的事件即代表链路通畅**（云端下发的 create / modify 由 receiver 归一成端侧动作 update），此后出现的问题均属于智能体（Agent）代码问题。
没有云端可发时，这条事件可以手工造一条，见下方「模拟云端下发」。

启动前会先做清理（拒绝重复启动 / `--force` 收掉旧那套 / 停运动后才允许启动），见下方「启动前清理」。

根目录另有 [stop_bull_chase.sh](stop_bull_chase.sh)——**不是启动入口**：一键向 `/bullfight_ctl` 下发结束信令（`status=01`）停掉正在进行的斗牛，环境由脚本自己备齐，随时可跑（用法见脚本头部）。

---

# 组件侧入口（start_agent.py）

组件侧（后台组件 + 云端桥 receiver）的入口是根目录的 [start_agent.py](start_agent.py)，
**它自己不做环境准备**——谁起它谁备环境：

- `bash starter.sh`（完整系统入口）：ROS 三件套、DDS 参数、`LOCAL_*` 都由脚本自己 source / export 好，
  备好后直接跑 `python3 start_agent.py cloud`（见 [starter.sh](starter.sh) 头部）；
- 手工单跑 `python3 start_agent.py cloud`：这套环境要自己先备，照抄下方「ROS2 环境」那几行即可。

两种起法下，参数怎么解析、组件怎么起停、旧那套怎么清，全在 `start_agent.py` 与
`Communicator/startup_cleanup.py` 里，没有第二份实现。

## 1. 启动前提

| 前提 | 内容 |
|---|---|
| cwd 在仓库根 | **相对路径起法只有 cwd 在仓库根时才被启动前的判定认出来**（`starter.sh` 第 1 步已自己 `cd`） |
| ROS 环境已备 | foxy → unitree_ros2 → `/usr/bin/cmcc_robot/install` 三件套（`setup.bash` 含未加保护的变量引用，source 时要临时关掉 nounset，见 `starter.sh`），外加 DDS 三项 `RMW_IMPLEMENTATION` / `ROS_DOMAIN_ID` / `CYCLONEDDS_URI`；手工单跑照抄「ROS2 环境」，已 source 过再 source 一遍不冲突 |
| `LOCAL_*` 已备 | `LOCAL_BASE_URL` / `LOCAL_API_KEY` / `LOCAL_MODEL_ds`：环境变量优先，其次 `install/secret.env`（`LOCAL_MODEL_ds=... python3 start_agent.py cloud` 这样临时覆盖也照旧生效）——哪一层都不编默认值 |
| python3 是 3.8 | 机器人 `.so` 绑定要求 |
| 移动服务在跑 | `robot-dog-motion.service` 先起来（`systemctl status` 看），否则动作/移动类调用失败 |

## 2. 日志

| 输出 | 去处 |
|---|---|
| status_observer / frame_producer（两个子进程） | `runtime_files/logs/status_observer.log`、`runtime_files/logs/frame_producer.log`；**每次启动把上一轮的改名成 `.log.prev`**，便于对照「这次之前」的现场 |
| start_agent.py 的全部组件（含云端收发桥 receiver） | `runtime_files/logs/starter_components.log`（**上一轮改名成 `.prev`**）：组件自己的输出整份落这里；其中**云端下发给本智能体的每条事件**（receiver 照原文打的那行，正文以 `configs/configs.py` 的 `CLOUD_EVENT_LOG_TAG` = `[Receiver] 📨 云端下发` 开头）**另外挑到终端**，所以也进下面那份 `<拉起时刻>.log`（筛法见 [scripts/lib-starter.sh](scripts/lib-starter.sh) 的 `starter_start_components`） |
| starter.sh 自己（含它拉起的 watcher） | `runtime_files/logs/<拉起时刻>.log`（`YYYYmmdd-HHMMSS`，**一次拉起一份**）：终端上打过的启动流水、守卫结论、收尾汇总都在里面，运行期间逐行落盘（终端照常打印）；实现见 [scripts/lib-starter.sh](scripts/lib-starter.sh) 的 `starter_start_terminal_log` |
| vision_observer / motion_status / 主循环 | 随本终端（输出量都很小，且出问题时要当场看到） |

```bash
tail -f runtime_files/logs/status_observer.log
```

`runtime_files/` 的三个子目录（`logs/`、`tasks/`、`photos/`）由**监督器每轮清一次**：启动脚本
不再管这件事，清理的判据与实现都在 [Observer/cleanup.py](Observer/cleanup.py) 的 `--runtime-sweep`，
由 [dsh-plugins/supervisor-loop](dsh-plugins/supervisor-loop/) 每轮旁路起一个子进程跑它
（**不经过大模型**——插件只把结论记成一行日志，交给模型的观测输入不受影响）。

每个目录都是**两档水位**（[configs/configs.py](configs/configs.py) 的 `RUNTIME_*_TRIGGER_MB` /
`_TARGET_MB`）：体积超过触发线才动手，一动手就删最旧的直到不超目标线。只删该删的——轮转下来的
`*.log.prev`、starter.sh 每次拉起一份的 `<时间戳>.log`（它不轮转，不删就没有上限）、已终结任务的
`<task_id>.yaml` 存档、frame_producer 命名格式的受管图；进程正在写的固定名 `*.log` 与活的
`pending_tasks.yaml` 一律不碰。注意它**不是备份**：删掉的历史不还原。

## 3. 退出

`Ctrl+C` 与 `SIGTERM`（`kill <pid>`）走同一条收摊路径：**停** `motion_status` 线程 → **收** 子进程
（状态观测器、**取帧服务**；SIGTERM → 等 5s → SIGKILL）→ **关** 取帧服务的客户端连接。

| 退出码 | 含义 |
|---|---|
| `0` | 正常（cloud 被中断后收摊完毕） |
| `1` | 启动前就被挡下：开关组合非法、已有另一套在跑（无 `--force`）、入口模块导入失败、旧进程收不干净 |
| `130` | 收到 `Ctrl+C` / `SIGTERM` |

---

# 启动参数

**第一个位置参数是模式**（必填；只剩 `cloud` 一个取值 —— 旧代的 `local`（主循环
`Agents/task/task_loop.py`）随 `Agents/` 整棵删除一并取消，现在给 `local` 会被 argparse 直接拒掉）：

| 模式 | 进哪个主循环 | 什么时候用 |
|---|---|---|
| `cloud` | `Communicator/receiver.py` —— 把云端下发的事件投给角色系统，长驻到被中断 | 正常跑的机器人 |

**start_agent 自己的开关**（写在模式名之后；其余参数原样透传给入口脚本）。组件**默认全起**（含云端桥 receiver），关哪个加哪个 `--no-*`：

| 开关 | 作用 |
|---|---|
| `--no-receiver` | 不起 receiver（只把组件拉起来长驻 —— 此时云端事件没人投进 rolebus） |
| `--no-frame-producer` | 不起取帧服务（视觉观测与视觉工具随之不可用） |
| `--no-vision-observer` | 不起视觉观测器（`vision_obs.yaml` 停更） |
| `--no-status-observer` | 不起状态观测器（`robot_status.yaml` 停更） |
| `--no-motion-status` | 不起运动状态观测器（`motion_status.yaml` 停更） |
| `--force` | 默认直接拒绝重复启动；加了这个才允许先停运动、再收掉旧那套的整棵进程树后重起（见「启动前清理」） |

**组件周期不是启动参数**：`robot_status.yaml` 写盘、运动状态心跳的节奏都是 `configs/configs.py` 里的常量（`STATUS_WRITE_INTERVAL` / `MOTION_STATUS_*`），改节奏就改那里——一处改、下次启动生效，不必每次启动各调一遍参数。

**例外：视觉观测器的两个参数可以运行中改，不用重启**。观测周期（隔多久看一眼帧环）与每轮拼接帧数放在覆盖层 `runtime_files/vision_tunables.yaml`，观测器在主循环里**每轮**读一次，所以改完下一轮就生效（这是刻意留的口子——重启 agent 会打断正在跑的任务）：

```bash
python3 -m Orchestrator.vision_tunables                          # 看当前生效值（含来源：覆盖层 / 出厂默认）
python3 -m Orchestrator.vision_tunables --observe-interval 6     # 每 6 秒看一次（可调区间 = 档位表的跨度，现 2 ~ 60 秒）
python3 -m Orchestrator.vision_tunables --stack-frames 5         # 每轮拼 5 帧（1 ~ 帧环深度）
python3 -m Orchestrator.vision_tunables --reset                  # 删掉覆盖层 = 两项都回出厂默认
```

**相机抓拍节奏不在这个口子里，也不该在**：帧生产者（`skills/camera/frame_producer.py`，由 start_agent 作 **`--serve` 子进程**拉起）是全体消费者共享的常驻取帧服务（其它进程经 `runtime_files/frame_producer.sock` 取帧），一直以固定的高频率抓帧把帧环攒满（`FRAME_PRODUCER_INTERVAL`，只在构造时定一次）；调它受害的是所有从帧环取帧的人（观测器、视觉工具、任务主循环）。这个口子只管**观测器自己的取舍**——多久看一次、看的时候拼几帧。

出厂默认与可调区间只登记在 `configs/configs.py`（`DEFAULT_INTERVAL` / `VISION_STACK_FRAMES` / `VISION_OBSERVE_RATE_LEVELS`——观测周期的可调区间就是那张档位表的跨度，不另立上下限常量；出厂默认 `DEFAULT_INTERVAL` 也**取档位表里的一档**（`low`），不写秒数，所以它不会与那张表脱钩），覆盖层不抄一份边界；手改该文件时字段名拼错会被当作「不认识的字段」回显告警且不生效。智能体侧有 `vision_tunables` 工具，**只认低/中/高三档档位名**（档位→秒数查 `VISION_OBSERVE_RATE_LEVELS`），**拿不到拼接帧数**。
判活阈值不是常量：vision 心跳的存活判定 = `3 × (观测周期 + VISION_TIMEOUT)`，由 `configs.configs.vision_alive_max_age_sec()` 现算、`status_observer` 每次判活现读（所以把周期调慢不会让健康的心跳被判成 `unavailable`）。

两条组合硬约束（违反直接拒绝启动）：**视觉观测必须有取帧服务**；`cloud --no-receiver` 且四个组件全关（没有事情可做）。

**模式名之后的参数原样透传给入口脚本**（入口的 argparse 是唯一事实源，start_agent 不抄一份它们的默认值）。receiver 现在不收任何参数（`--test-send` 那套随任务池一起删了），乱给参数会被它自己拒掉：

```bash
python3 start_agent.py cloud
python3 start_agent.py cloud --no-receiver     # 只跑组件，不接云端事件
```

**本入口没有 `--dry-run`**（2026-09-21 删除）。只想看看「现在有哪些 agent 进程、会不会被拒」而不碰硬件：

```bash
python3 Communicator/startup_cleanup.py --inspect      # 只查不杀
```

---

# 模拟云端下发

没有云端、或只想单独验「云端事件 → receiver 转投角色系统」这一段时，用
[Communicator/cloud_event_sim.py](Communicator/cloud_event_sim.py)：传 event 与 description，
**发一次 = 触发一次** receiver 的转发。

```bash
cd <仓库根>   # cloud_event_sim.py 从自己所在目录导入 configs/，必须在仓库根跑
python3 Communicator/cloud_event_sim.py create "去厨房看看冰箱里有什么"
python3 Communicator/cloud_event_sim.py modify "改去厨房，别开冰箱了"   # 端侧与 create 同义
python3 Communicator/cloud_event_sim.py answer "在519"     # 回答端侧的主动发问（当前链路已停用，见下）
python3 Communicator/cloud_event_sim.py interrupt "用户改主意了"
python3 Communicator/cloud_event_sim.py create "去门口" --task-id cloud_20260921153000
python3 Communicator/cloud_event_sim.py create "去门口" --dry-run         # 只打印事件 JSON
```

| 参数 | 说明 |
|---|---|
| `create` / `modify` / `answer` / `interrupt` | 必填。这里给的是**云端词**（模拟器发的是云端原话，归一在 receiver）。create / modify 会**真的执行任务**（规划、导航、动作、播报），两者在端侧同义（都归一成 `update`）；answer 只喂云端回答（**端侧不处理：属 `UNHANDLED_ACTIONS`，发了只有一条日志，没有消费方**）；interrupt 会**真的停运动**，端侧归一成 `stop` |
| `description` | create / modify 的任务描述、answer 的回答原文（三者必填）/ interrupt 的取消原因（可省，省了用 receiver 自带的默认文案） |
| `--task-id` | 云端任务 id；省略时由 `plugin_tools/rolebus.py` 补 `cloud-<Unix 秒>` 占位 |
| `--wait` | 等话题订阅者出现的最长秒数，默认 5 |
| `--dry-run` | 只打印将下发的事件 JSON（离线核对字段，不碰机器人） |

**走的是真链路**：把一条 `SIGCEvent` 发到 receiver 订阅的那条话题（`configs.paths.ROS_SIGC_EVENT_TOPIC`），
完整经过 `_on_sigc_event`（去重/过滤/凭证分流）→ `on_speech_event`（按 `body.action` 路由）→
`_forward_action`（投 rolebus）。receiver 一行没改，也不在进程内直调转发函数——直调会跳过订阅与
路由这两段，恰恰是「云端下发到端侧」最该验的部分。
协议常量（事件名/动作名/域）从 `Communicator/receiver.py` import，不重抄一份字符串。
脚本自带 ROS 环境自举（经 `configs/lib-shell-paths.sh` 取 setup 与 domain，不写死路径），`ROS_DOMAIN_ID` 已导出则沿用。

**验到哪为止**：本脚本验的是**组件侧**那半条链路——事件有没有进 spool，
看 `$DSH_HOME/rolebus/inbox/`（`answer` 那条腿——写 `runtime_files/pending_answer.json`
单槽——已随 answer 事件停用，现在不动了）。角色系统真收到并起了什么反应，看它自己的会话输出与
`$DSH_HOME/rolebus/archive/<角色>/`。想跳过 ROS 直接往角色系统投一封 cloud 事件，用
`scripts/manual/cloud-emit.sh`（本机手工工具，不入库）。

**三条注意**：

- **receiver 得在跑**，否则话题没人订阅，事件发出去就没了。两条启动路都起它：
  手工单跑的 `python3 start_agent.py cloud`（组件 + receiver 同进程）与 `starter.sh`（后台组件含
  receiver、前台角色系统，`DSH_HOME` 由它 export 给两边）。脚本会打印订阅者数量：0 就是没接上
  （没起 / 不在同一个 ROS domain / 用 `--no-receiver` 起了），退回码 `1`。
- **多个订阅者 = 各执行一遍**：ROS2 同话题的订阅者是各收一份（不是队列）。同一条话题上还挂着
  别的部署树的 receiver（如 `/root/cmcc_harness_agent_receiver`，见「启动前清理 · 收不到什么」）时，
  一条 create 会被两边各跑一次（两次导航、两次播报）——脚本打印的订阅者数量 > 1 就是这个情况。
- **连发两次就是两次真实触发**：每次下发都带新的 `eventId`，receiver 的去重只挡「同一条事件被重复投递」，
  挡不住「我又发了一条」。

退出码：`0`=已下发且当时有订阅者 / `1`=已发布但无人订阅（等于丢了）/ `2`=用法错误 / `3`=环境或依赖缺失。

## `exe` 事件：云端点名直接执行一个工具

上面那些 `robot_agent_task` 下发的都是**任务内容**——要经角色系统叫模型去理解。另有一条更直接的：
`exe` 事件让云端**点名执行一个工具**，receiver 当场按
[plugin_tools/available_tools_cloud.yaml](plugin_tools/available_tools_cloud.yaml)（`configs/paths.py`
的 `AVAILABLE_TOOLS_CLOUD_FILE`；与角色系统那份白名单是**两份**，互不影响，写进那份文件什么就能执行什么）
执行，不投 rolebus、不叫模型。

```json
{"event": "exe", "eventId": "…", "body": {"tool_name": "speak", "parameters": {"text": "你好"}}}
```

`tool_name` 必填、且必须在上面那份清单里（表外一律拒绝并告警）；`parameters` 可省（缺省空参数），
给 JSON 字符串也认。工具在**独立线程**里跑：这条腿上的工具都是阻塞的整段流程（speak 等说完、
waypoint_nav 轮询到抵达，几十秒到几分钟），就地跑会把 ROS 回调线程占住，心跳与后续云端事件一起停。
结果只记 receiver 日志、**不回云端**。

`cloud_event_sim.py` 目前只会发 `robot_agent_task` 的那几种动作。要离线核对这条腿的拆参 / 过滤 /
执行，跑 [test/test_cloud_exe_event.py](test/test_cloud_exe_event.py)（不起 ROS 订阅，直接把事件 JSON
喂给 receiver 的处理函数；跑的工具只有只读的 `task_list`，不出声不动）；要在机器人上真验，就手工往
`configs.paths.ROS_SIGC_EVENT_TOPIC` 上 pub 一条上面那样的 `SIGCEvent`。

---

# 启动前清理

`start_agent` 启动时保证**同时只有一套**智能体系统在跑、且**本体是静止的**。实现全在
[Communicator/startup_cleanup.py](Communicator/startup_cleanup.py)（与运行中的任务终止
`Communicator/task_exit.py` 是两条线，互不调用）。

**什么算「另一套系统」**：两个主循环入口 —— `start_agent.py` / `Communicator/receiver.py`
（旧代那个 `Agents/task/task_loop.py` 随 `Agents/` 整棵删除一并取消，不再登记）。
**含 `start_agent.py` 自己**，因为 cloud 模式下 receiver 是被 start_agent 导入后
**在同一进程里**跑 `main` 的，那套系统的根进程命令行是 `python3 start_agent.py cloud`、
根本不含 `receiver.py`——只扫 receiver 会漏掉最常见的重复启动。`--no-receiver` 也算（只跑组件
同样是长驻的一套）。组件（status_observer / vision_observer / frame_producer）**不算另一套**，
只是上一轮残留。

| 情形 | 行为 |
|---|---|
| 已有另一套在跑，**没有** `--force` | **直接拒绝**、退出码 1。这一步零副作用：只读 `/proc`，不发信号、不导入入口、机器人一条指令都收不到 |
| 已有另一套在跑，**有** `--force` | 先**停运动**，再收掉旧那套的**整棵进程树**（入口 + 全部子孙），复核通过后才继续启动 |
| 有残留组件 / 技能脚本残骸 | **无条件收掉**（含手动单独跑起来的、含本次不打算起该组件时）：帧锁与两个状态 yaml 都是全仓唯一写手，两方并存不是合法状态 |
| 进程收不干净 | 拒绝启动 —— 两套并跑等于重复认领云端任务、抢帧环 |

**怎么收**：按 pid 逐棵收（不能按进程组 —— 本仓没有任何一处自起进程组（`start_new_session`），
入口的进程组由**起它的那个终端或脚本**决定，对它 `killpg` 会打到用户的 shell 和同终端里别的命令）。
名单 = 命中入口的进程 + **沿 ppid 链枚举的全部子孙**：这些子孙的命令行写的是**它们自己的**路径
（组件、技能脚本），不含入口路径，按入口扫只认得出根进程。

- 技能脚本链（`capture.sh` → `capture_direct.py` → `gst-launch`、`asr_monitor.py`、
  `motion.sh` / `robot_action.sh`、`run_tts_speak.sh` …）；
- 另外按命令行兜一层：**cmdline 里出现本仓 `skills/` 下脚本路径**的 python/bash 进程也收
  （父链已断的孤儿只能这样认）。

终止顺序：**一次性广播 SIGTERM** → 等 `PROC_TERM_WAIT_SEC`(5s) → 对幸存者**重新拉一遍子孙**
（垂死的进程会在 `finally` 里新起 `motion.sh stop` / `pkill` / `check_action_state.sh`，父一死
它们就成孤儿）再 SIGTERM → 仍不退就广播 SIGKILL → 等 `PROC_KILL_WAIT_SEC`(2s) → 复核，仍在则
拒绝启动。

**停运动**（在收进程**之前**做——`motion.sh move` 这类指令是发给本体执行的，控制系统死掉不会让
本体停下来）：走 `task_exit.stop_all_motion()` 一个入口 → `MotionManager.stop_all()`，
move 停止指令 + 导航 cancel + VLN 清理，**绝不含急停**。之后复核导航状态与本体动作态
（`check_action_state.sh` 读一帧，不解析状态数字）。停不干净**只告警、不拦启动**——动作类没有
非急停停止接口，正在跳舞时就是停不下来；这套指令都是幂等的，典型耗时 < 2s，本体无响应时最坏
几十秒。

**线程随承载进程一起没**：`vision-observer` / `motion-status` / `robotdog-frame-producer` /
`motion-reader`（都在起组件的那一个进程内）与角色侧工具进程里的 `bull-chase-ros` 都是 daemon，
跨进程杀不掉线程，只能收掉承载它们的进程。receiver 改桥后**不再长线程**（旧代的 `task-exec-*` /
`task-resume-watcher` 已随任务池一起删了）。

**收不到什么**（设计边界，不是 bug）：

- **父链已断、重挂到 init 的孤儿**（`ppid=1`）：轮 2 能捞回父还活着的那部分，其余无法归属。
  剩余风险：还占着 ASR 会话的 `asr_monitor.py`、还在抓图的 `capture_direct.py`。
- **别的部署树**：`/root/cmcc_harness_agent_receiver`、`/root/RobotAlgorithmGen1` 的节点。判定用
  「入口路径 + 进程 cwd」双锚定就是为了不误伤；代价是那棵树占着相机/帧锁时本进程启动失败。
- **harness 角色系统那一棵**（缺省 interactor / orchestrator / supervisor，可加 planner）：不在本仓、也不在
  start_agent 树下，`--force` 不收。
- **systemd 与共享设施**：`robot-dog-motion.service` 的 server、导航 app、ros2 daemon、ROS 域内
  其它节点——一律不碰。
- **云端活跃任务不会复位**：强收时旧 receiver 来不及补报，云端可能挂着一个活跃任务直到超时，
  需在云端控制台人工取消。
- **别的 uid 的进程**（sudo 起的旧实例）：能看到、杀不动，于是如实报错并**拒绝启动**
  （宁可拒绝也不并跑）。

---

# 启动后产生哪些进程 / 线程

环境先备好（`starter.sh` 备的，或手工单跑时你自己 source 的——见「组件侧入口（start_agent.py）」），
此后进程形态完全由 `start_agent.py` 决定：**拒绝判定**（已有另一套就拒，零副作用）→ **先导入主循环模块**
（失败就此打住，不碰硬件）→ **启动前清理**（停运动 → 收掉旧那套的整棵进程树 → 收残留组件）→ 按固定顺序拉
组件 → 等首帧 → 进主循环；退出时统一收摊（停运动状态线程 → 收子进程 → 关帧生产者）。各组件都可用 `--no-*`
单独关闭。清理的细节见上方「启动前清理」。

| 组件 | 形态 | 代码 | 产物 |
|---|---|---|---|
| status_observer | **独立子进程** | `Observer/status_observer.py` | `runtime_files/robot_status.yaml`（60s 整份覆盖）；日志 `runtime_files/logs/status_observer.log` |
| motion_status | 本进程 daemon 线程 | `Motion_Manager/motion_status.py` | `runtime_files/motion_status.yaml`（状态一变就写 + 2s 心跳） |
| frame_producer | **独立子进程**（`--serve`） | `skills/camera/frame_producer.py` | 内存帧环 + 取帧服务 socket（`runtime_files/frame_producer.sock`，深度 `FRAME_PRODUCER_MAX_FRAMES`，改它要重启）；抓拍节奏固定 `FRAME_PRODUCER_INTERVAL`，**不参与运行时调参**；`save=True` 时落 `runtime_files/photos/`；日志 `runtime_files/logs/frame_producer.log` |
| vision_observer | 本进程 daemon 线程 | `Observer/vision_observer.py` | `runtime_files/vision_obs.yaml` + `vision_observer.heartbeat`；观测周期与每轮拼几帧都由 `runtime_files/vision_tunables.yaml` 定（**可运行中调**，见上文「例外」） |
| receiver | **本进程主循环**（`cloud` 模式） | `Communicator/receiver.py` | `receiver.heartbeat`（rclpy 定时器写）；转发出去的云端事件落在 `$DSH_HOME/rolebus/inbox/`（云端动作先按 `CLOUD_ACTION_MAP` 归一成 update / stop；`answer` / `info` 端侧不处理，其中 `answer` 那条写 `runtime_files/pending_answer.json` 的腿自 2026-09-28 起停用） |

（旧代的 `local` 模式主循环 `Agents/task/task_loop.py` 随 `Agents/` 整棵删除一并取消，`local` 模式已不认。）

运行中还会动态长出：组件侧 `motion-reader`（动作状态的只读常驻订阅，`MotionManager` 第一次
判动作时才懒起，daemon 线程）；角色侧**每调一次工具就是一个 `plugin_tools.bridge` 子进程**，
它自己再起叶子子进程（技能脚本、运动脚本、`bull-chase` 的 ROS 节点）——这些不在 start_agent
的进程树下，`--force` 收不到（见「启动前清理」）。receiver 不再长线程。

**哪些不能拉成子进程**：`motion_status` —— `Motion_Manager` 的运动记录只活在 manager 内存里，
拉成子进程会**静默**读到「没有这一笔」。帧环那道限制已经解除：帧环仍在生产者进程内存里，但
`frame_producer --serve` 把它经 `runtime_files/frame_producer.sock` 开放出来（unix socket，原始
JPEG 字节走 blob），消费者无论跑在哪个进程都找它取帧。反之 status_observer 必须独立进程——
它 import 期就拉 `homi_speech_interface`，失败即退出，同进程会把整个 agent 拖死。

receiver 与角色系统的关系是**跨进程**的：它俩唯一的通路是 rolebus spool（云端事件）与
`pending_answer.json`（用户回答原文，该腿现停用），没有任何共享内存——正是为此 receiver 才被
改成薄桥，不再在本进程里跑任务。

各组件自己写自己的心跳/状态文件，`status_observer` 只 `stat` 这些文件的 mtime 判活（不 pgrep、不重启），
所以**杀掉某个组件不会自动重启**，需要在启动横幅/`robot_status.yaml` 里看是哪一格不新鲜。

上表的三份状态文件（`vision_obs.yaml` / `robot_status.yaml` / `motion_status.yaml`）有两个读法：
Supervisor 角色的常驻循环每轮整份读进上下文当观测源（`Supervisor/resident.patch.yml` 的 `sources`），
智能体侧的 `robot_state` 工具则按需读单份（`section` 取 `vision` / `system` / `motion`），**原样返回
YAML 原文**——不做失效回落、也不替读方标过期，判新鲜度看文件里自带的时间戳。两边读的是同一批文件，
差别只在「每轮自动进上下文」还是「模型按需调」。

---

# 更换终端需修改的配置
换到另一台机器人终端时，需按该终端实际环境调整的参数集中在两处：随代码走的 `configs/`，
与本机私有的 `install/secret.env`（不入库）。

## install/secret.env
模型服务参数与密钥：端点 / API key / 模型名。**每台终端各一份、不入库**（`embody/.gitignore`
拦 `secret.env`），换终端改这一个文件就够——`LOCAL_*` 没有第二处。

```sh
LOCAL_BASE_URL=...       # OpenAI 兼容端点：展开进 $DSH_HOME/settings.yaml 的 baseURL
LOCAL_API_KEY=...        # 带进角色进程环境；settings.yaml 里只有变量名（apiKeyEnv）
LOCAL_MODEL_ds=...       # 大模型档（复杂推理 / 任务规划）：agent-default-model 用它
DEEPSEEK_API_KEY=...     # 可选：web_search 的 key（缺了只是 web_search 调不通）
```

请求头 `X-Device-ID` **不在这里**：那是设备 SN，唯一来源是 `configs/paths.py` 的 `DEVICE_ID`
（`/etc/cmcc_robot/cmcc_dev.ini` 的 `factory/devSn`，与云端 `device_id` 同一个值）；读不到时
`settings-seed.sh` 当场拒，不写一份假设备号。

取值口径是「环境变量优先，其次本文件」，所以 `LOCAL_MODEL_ds=… bash starter.sh` 这类临时覆盖
照旧生效；读取走 `scripts/lib-local-env.sh`（按行取值，**不 source 这个文件**）。
`LOCAL_BASE_URL` / `LOCAL_API_KEY` / `LOCAL_MODEL_ds` 缺一项角色就起不来，
`starter.sh` 会在起进程之前当场拒。

## configs/paths.py
**模型服务参数不在这里**：`LOCAL_*` 写在 `install/secret.env`（见上一节）。

**URL 类常量都在这里**：`NAVIGATION_BASE_URL`（本机 `127.0.0.1:8000`，导航那几条 URL 都由它派生）。
**通常无需改**：`START_AGENT_MODULE`（启动入口脚本自身，判「有没有另一套 agent 在跑」用）。

## configs/configs.py
**通常无需改**：`VISION_LOCAL_VQA_URL`（云端视觉问答地址）。各项超时（含导航的 `NAVIGATION_PROBE_TIMEOUT` / `_CONTROL_TIMEOUT` / `_REQUEST_TIMEOUT` 三条、云端上报的 `CLOUD_REPORT_TIMEOUT`）、周期、档位表也都在这里 —— **地址在 `configs/paths.py`，参数在这里**（任务状态上报走本体信令的 ROS 服务，服务名见那边的 `ROS_SIGC_DATA_SERVICE`）。

**工具暴露控制**：`configs/agents.py` 的 `ROLE_TOOLS`（角色 → 工具白名单，取值经 `role_tools()`）定义了每个角色能看到的工具清单。**注释掉某一行即可屏蔽对应工具**——该角色将无法调用它。工具的详细定义与提示词来自 `plugin_tools/available_tools.yaml`，新增工具时需同步维护。

**ROS2 环境**：
需配齐 Foxy + CycloneDDS + 机器人运行时包，DDS 必须能够发现发布端并收到语音唤醒事件。
下面这份就是 `starter.sh` 启动时 source / export 的那一套（见 [starter.sh](starter.sh) 头部），
也照抄给手工单跑的 `python3 start_agent.py cloud` 用。
**它只是说明性清单，不是第二份事实源**：路径以 `configs/paths.py` 的 `ROS_SETUP` /
`UNITREE_ROS_SETUP` / `CMCC_ROBOT_INSTALL` 为准（`starter.sh` 经 `cmcc_path` 取），
参数以 `configs/configs.py` 的 `RMW_IMPLEMENTATION` / `ROS_DOMAIN_ID` / `CYCLONEDDS_URI`
为准（经 `cmcc_conf` 取）——换终端改那两个文件。
`LOCAL_*` 则**只有一处**：写进 `install/secret.env`（或本终端 shell，环境变量优先），
角色侧经 `scripts/lib-local-env.sh` 从同一处取。
```bash
# ROS2 环境（Foxy + CycloneDDS + 机器人运行时包，缺一不可）
source /opt/ros/foxy/setup.bash
source /opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash
source /usr/bin/cmcc_robot/install/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID=2
export CYCLONEDDS_URI='<CycloneDDS><Domain><General><Interfaces>
                        <NetworkInterface name="eth0" priority="default" multicast="default" />
                    </Interfaces><AllowMulticast>spdp</AllowMulticast></General></Domain></CycloneDDS>'
# LLM 服务参数另写一处（install/secret.env，不入库）：
#   LOCAL_BASE_URL=...  LOCAL_API_KEY=...  LOCAL_MODEL_ds=...
```

---

# 环境配置

## 库
```bash
# 在 Python 3.8 环境下 `pip install`
apt install -y imagemagick   # vision 技能图像处理
pip install openai==2.2.0 fastapi==0.124.4 uvicorn==0.33.0 \
    requests==2.32.4 PyYAML==5.3.1 jsonschema==4.23.0
```

**ROS2 运行时包**（由下方 ROS2 环境提供，无需单独 pip）：
- `rclpy`（Foxy）、`homi_speech_interface`、`claw_client`

## 移动服务
```bash
# unit 里的 ExecStart / WorkingDirectory 必须是绝对路径（systemd 要求），
# 换终端时按本机技能路径改 skills/robot-dog-motion-control/scripts/robot-dog-motion.service 后再拷
sudo cp skills/robot-dog-motion-control/scripts/robot-dog-motion.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now robot-dog-motion.service   # 开机自启 + 立即启动
# systemctl status robot-dog-motion.service
# journalctl -u robot-dog-motion.service -f             
```

## 定点导航
```bash
SKILL_DIR="$(pwd)/skills/waypoint_nav"   # 绝对路径：systemd 的 unit 只认绝对路径
# 左值是要替换掉的原文，必须与 skills/waypoint_nav/navigation_api.service 里写的一致
sed "s#/root/cmcc_agent/skills/waypoint_nav/codes#${SKILL_DIR}#g" \
    "${SKILL_DIR}/navigation_api.service" \
    | sudo tee /etc/systemd/system/navigation_api.service > /dev/null   # 用 # 作分隔符，路径里的 / 无需转义
sudo systemctl daemon-reload
sudo systemctl enable --now navigation_api.service   # 开机自启 + 立即启动
```

### 校验
`/health` 通即服务在跑
```bash
systemctl status navigation_api.service
curl -s http://127.0.0.1:8000/health
```

### 重启
```bash
sudo systemctl restart navigation_api.service
journalctl -u navigation_api.service -f
```
unit 里是 `Restart=on-failure` + `RestartSec=3`：进程自己崩了会自动拉起，改完代码要手工 `restart` 才生效。


## 其它
- 链接`python3`和`python`解释器： `sudo ln -s /usr/bin/python3 /usr/bin/python`
- 技能依赖：需给予脚本文件权限。角色侧工具执行依赖仓库内 `skills/` 下的技能脚本（TTS / 动作 / 视觉 / 导航等），对应技能缺失时该工具调用会失败。（收声类工具都走 `skills/asr-listener/` 的 `run_asr.sh`：`listen` / `speak_listen` 拉它收音；`audio_interact` 先发 TTS（等播完）、再静默唤醒设备，从状态话题取设备侧 ASR 的最终结果。云端 answer 那条路自 2026-09-28 起停用，工具层已无消费方。）
- 创建导航功能 landmarks 软链接：见上方「定点导航」→「目录布局与软链接」。
- bash权限：`sudo chmod -R 777 skills`（在仓库根执行）