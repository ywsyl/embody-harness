---
name: robot-dog-actions
description: Control robot dog actions via ROS 2. Use when user says "站立", "坐下", "趴下", "握手", "跳舞", "表演", "比心", "拜年", "扑人", "前跳", "鞠躬", "扭身", "后空翻", "翻滚". Publishes RobdogAction to /catch_turtle/action_type. Supports both bash and python execution.
---

# Robot Dog Actions

Execute dance and action movements on robot dog via ROS 2 interface.

## Quick Start

**Bash (推荐):**
```bash
cd scripts/
./robot_action.sh dance
```

**Python:**
```bash
cd scripts/
python3 robot_action.py dance
```

## Available Actions

**🎵 舞蹈表演:**
- `dance` - 跳舞
- `danceV2` - 舞蹈2
- `happy` - 开心
- `stretch` - 伸懒腰
- `twistBody` - 扭身
- `twistAss` - 扭屁股
- `shakeBody` - 抖身（快抖）

**🐕 基础动作:**
- `standUp` - 站立
- `sitDown` - 坐下  
- `getDown` - 趴下
- `makeBow` - 鞠躬

**👋 互动动作:**
- `greeting` - 打招呼
- `handShake` - 握手
- `fingerHeart` - 比心
- `newYearCall` - 拜年
- `leap` - 扑人
- `jumpForward` - 前跳

**🤸 特技动作:**
- `walkUpsideDown` - 倒立行走
- `standErect` - 直立站立
- `backflip` - 后空翻
- `turnOver` - 翻滚
- `twistJump` - 扭身跳

### 动作清单的来源（2026-09-17 补全）

原清单只有 14 个（规格文档第 4 节那张表）。另外 8 个是从端侧两处独立线索查出来的，
**两条线索都命中**才算数：

1. **`speech_core` 的离线语音配置**
   `/usr/bin/cmcc_robot/install/homi_speech/share/homi_speech/launch/config/offline_asr.yaml`
   里的 `offline_parse_rule_config`（JSON）——每条离线指令给出
   `body.actionType: motorSkill` + `body.actionArgument: <动作名>`，即"说哪个词会下发哪个动作"：

   | 离线指令键 | 动作名 |
   |---|---|
   | `handShake` | `handShake` |
   | `shakeit` | `shakeBody` |
   | `twistedhisbuttoc…` | `twistAss` |
   | `compilation` | `makeBow` |
   | `backflip` | `backflip` |
   | `roll` | `turnOver` |
   | `twistjump` | `twistJump` |
   | `twistbody` | `twistBody` |

2. **`robdog_control_node` 二进制里的技能名**
   `strings …/robdog_control_node` 中这些名字作为独立字符串出现，
   且与已知技能名（`turnOver`/`twistJump`/`makeBow`/`backflip` 相邻成串，
   `twistAss`/`shakeBody`/`fastShakeBody` 相邻成串），并伴随
   `Unknown motorSkill: %s` 这类分派报错文案。

**验证程度**：上述 8 个动作名有"端侧配置 + 端侧二进制"双重证据，
但**没有逐条实发验证**（既有 14 个是实发过的）。首次使用建议空场地、留观察距离。

> ⚠️ 那份配置里**不是每条指令都走动作话题**：判据是顶层 `event` 字段，
> 只有 `event: "robot_action"` 才下发到 `/catch_turtle/action_type`。
> 两个容易踩的坑：
> - 指令键 `recovery` 的 event 是 **`unbind_notify_voice`**，body 里的
>   `actionType: "recovery"` 只是这条解绑通知自带字段，**不是马达技能**；
>   二进制里也查不到 `recovery` 分派键（只有 `recoveryPose` 等无关名字）。
> - `movewLeft`/`moveRight`/`turnBack`/`turnFront`/`turnCircle` 的 event 是
>   **`robot_move`**，body 形如 `{direction: {y: 2}, actionType: 2}`——
>   `actionType` 是**数字**，与 `RobdogAction.actiontype`（string）不是同一层语义。

> ⚠️ 那份离线配置里 `actionArgument` 的拼写**两种并存**：16 条写成 `actionArgument`、
> 19 条写成 `actionArguement`（拼错的）。本技能直连 ROS 话题，拼写与本技能无关；
> 但如果你照着那份配置改端侧离线语音，请以**运行验证**为准，不要照抄拼写。

## Usage Examples

```bash
# 跳舞
./robot_action.sh dance

# 站立
./robot_action.sh standUp

# 打招呼
./robot_action.sh greeting

# Python 方式
python3 robot_action.py dance
```

## Technical Details

接口以《机器人功能接口规格.md》第 4 节「动作执行」为准。

- **Topic**: `/catch_turtle/action_type`
- **Message**: `homi_speech_interface/msg/RobdogAction`（三个 `string` 字段）
- **Network**: eth0 (192.168.123.99)

| 字段 | 取值 | 说明 |
|---|---|---|
| `actiontype` | `motorSkill`（默认）/ `followMe` / `sportMode` / `robot_move` / `move_points` | 动作通道，见下 |
| `actionargument` | 动作名（`motorSkill`）或通道参数（其余通道） | 见上方动作清单 |
| `sourcetype` | `local-agent` | 调用来源，见下 |

### 动作通道（actiontype）

`robdog_control_node` 按 `actiontype` 分派（从二进制里可辨认的分派键 + 端侧离线配置的
`body.actionType` 交叉得到）：

| `actiontype` | 说明 | 参数（`actionargument`） | 本技能 |
|---|---|---|---|
| `motorSkill` | 马达技能，预设单个动作 | 动作名，见上方清单 | ✅ 默认通道，已实测 |
| `followMe` | 跟随开关 | `on` / `off` | ⚠️ 未实测，见下 |
| `sportMode` | 运动模式（步态） | `walk` / `AINimble` / `jumpRun` / `AIClassic` / `runSide` / `traction` | 不支持 |
| `off` | 关闭当前模式（与 `sportMode` 配对，无参数） | — | 不支持 |
| `robot_move` | 方向移动（端侧用 `direction`/数值参数，非本字段语义） | 数值 | 不支持 |
| `move_points` | 按点位移动 | 点位参数 | 不支持 |

脚本用第二个位置参数指定通道（缺省 `motorSkill`）：

```bash
./robot_action.sh dance                 # motorSkill/dance（工具层调的就是这种形式）
./robot_action.sh on followMe           # followMe/on —— 开跟随，未实测
./robot_action.sh off followMe          # followMe/off —— 关跟随，未实测
```

### ⚠️ `followMe` 通道未实测

证据只有两处，**没有从本话题实发验证过**：

- 端侧离线配置里 `openfollow`/`closefollow` 两条指令下发的是
  `event: "robot_action"` + `body.actionType: "followMe"` +
  `body.actionArgument: "on"` / `"off"`
  （注意：`openfollow`/`closefollow` 是**离线指令键**，不是参数值）；
  除这两条外，`elevatorIn`/`elevatorOut`/`stairUp`/`stairDown`/`throughGate`/
  `runOn`/`runOff` 共 7 条也下发 `followMe`/`on`——形状上是把「开跟随」
  当成特殊地形动作的前置开关，`closefollow` 才是配对的关闭；
- `robdog_control_node` 二进制里有 `followMe` 分派键与 `followMeon`/`followMeoff`、
  `robdog_control/src/followMe/followMeNode.cpp` 等符号。

同时端侧还有**一套独立的跟随话题**，才是跟随功能的正门（实测）：

| 话题 | 类型 | 发布方（实测订阅/发布关系） |
|---|---|---|
| `/follow_me/start` | `std_msgs/msg/String` | pub: `follow_start`；sub: `control_catch_turtle_v1`、`ros_control_node` |
| `/follow_me/end` | `std_msgs/msg/String` | 同上 |
| `/follow_me_status` | `std_msgs/msg/String` | pub: `sub_uwb_node` / `ros_control_node` / `accompany` / `uwb_prep`；sub: `lite_console` |
| `/follow_me_control` | `std_msgs/msg/String` | pub: `follow_node`；sub: `live_stream_node`、`lite_console` |

**要正式做跟随功能，应先走 `/follow_me/start` / `/follow_me/end` 核实报文格式**，
而不是从 `action_type` 发 `followMe`。本技能因此**只提供通道、不把 `followMe` 暴露成
LLM 可调的动作值**——`action` 工具的枚举里没有它，避免模型自己触发一个未验证的物理行为。

### 调用来源（sourcetype）

`RobdogAction` 有**专门的来源字段** `sourcetype`，不需要另造字段。

- 端上既有取值：`speech_offline`（离线语音）/ `speech_online`（在线语音）/
  `app` / `device`
- 本机智能体发起的一律填 **`local-agent`**，该取值已写进规格文档第 4 节
- 调用方可用环境变量 **`ACTION_CALLER`** 覆盖

### 消费方

`/catch_turtle/action_type` 的**唯一订阅方（消费方）是 `robdog_control_node`**。
实测（2026-09-16）该话题的发布方有 6 个，全是端上节点：`auto_charge_docking_node`、
`robot_ota_node`、`follow_node`、`rhythm_master_node`、`robdog_control_node`×2 ——
**说明它本来就是设备原生的动作接口**，各节点都直接发它。

`robdog_control_node` 同时也是 SIGC 事件总线（`/homi_speech/sigc_event_topic`）
到该话题的中转方 —— 走 SIGC 时 `RobdogAction.sourcetype` 由 SIGCEvent 顶层的
`source` 填充。

**因此本技能直连 `action_type` 之后，动作不再经 SIGC 事件总线上报**，
`websocket_node` / `live_stream_node` / `uploadlog_node` 等 SIGC 订阅方看不到
这次动作。这是按"以规格文档为准"取舍的结果：驱动链路不变（同一个
`robdog_control_node`），只是少了一份总线上报。

## Implementation

Both bash and python versions:
1. Initialize ROS 2 Foxy environment
2. Configure CycloneDDS for eth0 interface
3. Publish one `RobdogAction{<通道>, <动作名/参数>, local-agent}` to `/catch_turtle/action_type`
   （通道缺省 `motorSkill`）
4. Exit non-zero if the publish fails (工具层据此报错)

Python 版在发布前会校验：通道必须在 `ACTION_CHANNELS` 内；
通道为 `motorSkill` 时动作名还必须在 `VALID_ACTIONS` 内（其余通道的参数是另一套取值，
不拿马达技能清单去卡）。

**动作清单四处同源，改一处要改四处**：
本文件上方清单、
`scripts/robot_action.py` 的 `VALID_ACTIONS`（技能脚本校验）、
`plugin_tools/tool_executor.py` 的 `_VALID_ACTIONS`（工具层白名单，拦截无效动作名，
它从 `configs.MOTION_ACTION_TIMEOUT_BY_ACTION` 派生）、
`plugin_tools/available_tools.yaml` 里 `action` 工具的 `action_type` 枚举（给 LLM 看的）。

这四处清单现已交叉核对一致（22/22）——注意技能脚本那份是 23：它多一个 `emergencyStop`，
那个动作**刻意**不下发给 LLM（急停不暴露给模型），工具层与 yaml 都不该出现它。
（旧树还要求同步 `reference/skill_validation.md` 与 `Agents/chat/chat_loop.py` 的提示词，
那份文件与那套循环在本树都已不存在。）
`skills/机器人功能接口规格.md` 与 `reference/机器人功能接口规格.md` 是同一份文档的两份拷贝，
第 4 节动作表也应保持同步。

## Requirements

- Robot dog powered on
- ROS 2 Foxy environment
- CycloneDDS configured
- Network interface: eth0
- Source paths:
  - `/opt/ros/foxy/setup.bash`
  - `/opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash`
  - `/usr/bin/cmcc_robot/install/setup.bash` ← **运行中的安装**
    （`RobdogAction` 的消息定义以运行版为准；`/root/cmcc_robot` 是陈旧副本）
