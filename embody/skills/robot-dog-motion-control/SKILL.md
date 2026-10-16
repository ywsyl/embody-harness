---
name: robot-dog-motion-control
description: 机器狗精确运动控制，支持时间控制、连续动作和无停顿串行动作执行。响应速度 ~10ms。当用户说"前进X米"、"转X度"、"走X秒"、"连续动作"、"不要停顿"、"连续执行多个动作"时使用此技能。支持精确时间控制、中断模式（无缝切换）、队列模式（顺序执行）、连续动作直连执行。
---

# 机器狗运动精确控制

机器狗精确运动控制系统。响应速度：**~10ms** ⚡

## 何时使用此技能

**✅ 适用场景：**
- 用户说"前进 X 米"、"后退 X 米"（需要精确距离控制）
- 用户说"左转 X 度"、"右转 X 度"（需要精确角度控制）
- 用户说"走 X 秒"、"转 X 秒"（需要时间控制）
- 用户说"连续动作"、"不要停顿"、"直接转"、"连续执行多个动作"（需要无缝切换或连续串行执行）
- 用户说"转一圈"、"转 360 度"（需要持续旋转）
- 需要快速响应的运动控制

**❌ 不适用场景：**
- 简单的预设动作（如"站立"、"趴下"、"跳舞"）→ 使用 `robot-dog-dance`
- SLAM 导航到指定位置 → 使用 `robot-dog-navigation`
- 室外 GPS 导航 → 使用 `robot-dog-outdoor`

## 核心优势

1. **精确时间控制** - 指定运动持续时间，自动停止
2. **无缝切换** - 中断模式，动作之间无停顿
3. **连续动作执行** - 支持多个动作连续串行执行
4. **快速响应** - ~10ms 响应时间
5. **灵活模式** - 支持中断模式和队列模式

## 快速开始

**启动服务（终端 1）：**
```bash
cd skills/robot-dog-motion-control/scripts
./start.sh
```

**发送命令（终端 2）：**
```bash
cd skills/robot-dog-motion-control/scripts

# Move forward 0.5 m/s for 10 seconds (interrupt mode, default)
# 该命令 10 秒后才返回（运动结束）
./motion.sh forward 0.5 10

# Turn left 0.3 rad/s for 5 seconds (interrupt mode)
./motion.sh turnleft 0.3 5 1

# Move backward 0.3 m/s for 5 seconds (queue mode)
# 注意：队列模式要让前一条命令后台跑（末尾 &），否则前一条返回后队列已无意义
./motion.sh forward 0.5 5 & ./motion.sh backward 0.3 5 0

# Stop immediately (clears queue, and wakes up the running command)
./motion.sh stop
```

## ⏳ 返回时机（阻塞语义，2026-09-15 改）

**`motion.sh` 不再"下发即返回"**：命令受理后会一直阻塞，直到**这次运动真正结束**才返回，
并在 stdout 给出结束原因。

结束（= 停止信号）的四种情况：

| 结束原因 | 回包文本 |
|---|---|
| 时长到达，自动停止 | `✅ 运动完成（时长到达，已自动停止）` |
| 收到 `stop` 命令 | `✅ 运动已停止：收到 stop 命令` |
| 被新的打断模式命令取代 | `✅ 运动已中断：被新命令打断` |
| 排队中的命令被 `stop` 清空 | `✅ 队列被 stop 命令清空，未执行` |

- `duration = 0`（持续运动）没有时长终点，会**一直等到 `stop`** 才返回。
- 未知命令、队列已满会立即返回失败，不阻塞。
- **调用方意义**：命令返回 == 运动结束，后面不需要再补 `sleep` 等待。
- **想中途打断**：把命令放到后台执行（末尾加 `&`），再从别处发 `stop` 或新的打断命令——
  前台执行时 shell 被占住，发不出第二条命令。

## 🐕 运动前站立检查（工具层策略，2026-09-16 增）

趴着直接下发速度指令会摔或动作异常，所以 `move` 工具在**每次运动前**先确认机器狗
处于**静止站立**，不在就自动站起来再执行。

| 步骤 | 动作 |
|---|---|
| 1 | 读一帧 `/catch_turtle/action_state`（`std_msgs/msg/Int32`，20 Hz） |
| 2 | `=2`（静止站立）→ 直接下发运动 |
| 3 | 其它值（趴下/运动中/急停/切换步态）→ 先 `robot_action.sh standUp`，再轮询等它站起 |
| 4 | 站稳（等到 `2`）→ 继续原运动；等待超时 → 取消本次移动并返回中文提示 |

- 判据取值见《机器人功能接口规格.md》：`0` 未执行动作 / `1` 正在执行动作 /
  **`2` 静止站立** / `3` 静止趴下 / `4` 运动中 / `5` 切换步态中 / `6` 急停状态
- 用 `action_state`（20 Hz）而不是 `/robot_idle_state`（0.33 Hz）：下发前要的是
  实时值，低频那份可能滞后数秒
- **读不到状态时不下发运动**（话题无数据 / 环境异常）：姿态未知的情况下让四条腿动
  起来风险太大，宁可如实报错让调用方重试
- `stop` / 急停**不做**此检查——停下来不该被任何前置条件挡住
- 自动站立过的那次，`move` 的返回里会带「（执行前已自动站立）」

### 手动查询状态

```bash
cd skills/robot-dog-motion-control/scripts
./check_action_state.sh                    # 读一帧当前状态
./check_action_state.sh --wait-standing    # 轮询等到静止站立（=--wait-state 2）
./check_action_state.sh --wait-state 2,3   # 轮询等到 action_state 落在给定集合内
./check_action_state.sh --wait-state 2 --consecutive 2   # 连续 2 帧都是 2 才算等到
./check_action_state.sh -v                 # 打印每次状态变化
```

`--wait-state` 是通用等待（判「动作结束了」传终态，判「动作真的动起来了」传 `1,4,5`）。取值必须是
`0~6`（动作状态域），写别的会在参数校验阶段就报 `3`，不会白等到超时。

> **等待类模式现在是给人手动排查用的**。`Motion_Manager` 判「动作结束没」读的是**进程内那份
> 常驻订阅**（`Motion_Manager/motion_status.py` 的 20Hz 订阅，与 manager 同进程，判据是按时间
> 保持 `configs.MOTION_IDLE_HOLD_SEC` 秒静止），因为本脚本**从启动到收到第一帧要 2~3s**（bash +
> 读 configs + rclpy init + DDS 发现），而 `getDown` 这类短动作本体只要 2.2s（真机实测）——等它
> 张开眼动作已经做完，于是「已经成功却报未观察到启动」。**2026-10-08 起「取不到帧就现起本脚本来
> 等」那条兜底已删**；脚本仍以**读一帧**的形式服务于开机清理复核与 `_ensure_standing` 的站立闸门。
> 上面那两条 `--wait-state` 的用法、以及下面的退出码语义都**没有变**。

`--consecutive N` 是等待类模式的**消抖**：目标状态要连续收到 N 帧才算达成，中间任何一帧
落到集合外就清零重来（默认 `configs.MOTION_IDLE_HITS`；`1` = 见到一帧就算）。加它的原因：
动作通道没有完成信号，判结束只能看 `action_state`，而动作执行**中途会短暂闪回 `2`/`3`**，
只看一帧会把「还没做完」判成「已完成」。计数按**收到的帧**、不按检测循环的轮次——话题静默
时循环在空转，按轮次会把同一个旧值重复计数。**注意单位是帧**：主路径那条判据是按**时间**保持
`MOTION_IDLE_HOLD_SEC` 秒，`MOTION_IDLE_HITS` 只是把同样时长在当前话题频率下折算成的参考值，
两个口径别互相推。

退出码：`0` 等到了 / `1` 读到但不是站立（仅默认模式）/ `2` 超时未等到 / `3` 环境错误
（stdout 保持为空，诊断走 stderr）。等待上限、判据取值与消抖参考值都在 `configs.configs`
的 `MOVE_STAND_TIMEOUT` / `ACTION_STATE_STANDING` / `MOTION_IDLE_HITS`，
脚本不另设默认值。

> **速度校验不在此列**：工具调用时速度是写死的（`MOVE_LINEAR_SPEED` /
> `MOVE_ANGULAR_SPEED`），不对上层暴露，因此不做范围校验。

## 命令语法

```bash
./motion.sh <command> [speed] [duration] [interrupt]
```

**参数说明：**
- `command`: Motion command (required)
- `speed`: Speed value (optional, default 0.5)
- `duration`: Duration in seconds (optional, default 0)
  - `> 0`: Move for specified seconds then auto-stop
  - `= 0`: Move until next command
- `interrupt`: Interrupt mode (optional, default 1)
  - `1` or `true`: Interrupt current action immediately
  - `0` or `false`: Queue mode, wait for current action to complete

**返回：** 命令阻塞到本次运动结束才返回（见上文「返回时机」），无 source 之类的附加字段。

**连续动作约定：**
- 当用户一次给出多个动作且明确要求“连续执行 / 不停顿 / 无缝衔接”时，应按动作顺序连续下发，不在动作之间插入额外等待
- 若每个动作都带有明确时长，则前一个动作自然结束后立即执行下一个动作
- 若需要“立刻打断并切到下一个动作”，使用中断模式
- 若需要“当前动作做完再接下一个动作”，使用队列模式

## 可用命令

**线性运动：**
- `forward [speed] [time] [interrupt]` - Move forward (0.1-1.5 m/s)
- `backward [speed] [time] [interrupt]` - Move backward (0.1-1.5 m/s)
- `left [speed] [time] [interrupt]` - Move left (0.1-1.0 m/s)
- `right [speed] [time] [interrupt]` - Move right (0.1-1.0 m/s)

**旋转运动：**
- `turnleft [speed] [time] [interrupt]` - Turn left (0.1-0.5 rad/s)
- `turnright [speed] [time] [interrupt]` - Turn right (0.1-0.5 rad/s)

**控制命令：**
- `stop` - Stop all motion and clear queue

## 连续动作执行规则

当用户一次描述多个动作时，按以下规则处理：

1. **默认按顺序连续执行**
   - 例如："先前进 1 米，再右转 90 度，再前进 2 米"
   - 应拆成多个动作，按顺序依次执行

2. **动作之间不额外停顿**
   - 如果用户强调"不要停顿"、"连续执行"、"无缝衔接"
   - 则前一个动作结束后立即发送下一个动作
   - 不插入 `sleep`、等待确认或额外 stop

3. **区分两类连续性**
   - **顺序连续执行**：前一动作完成后，立刻执行下一动作
   - **中断切换执行**：当前动作未完成时，立即切换到下一动作

4. **推荐映射**
   - 已知每段动作时长，且要求按顺序完成：优先使用队列语义
   - 当用户给出多个已知时长动作，且要求连续执行但不是提前打断时：默认使用队列模式，不插入 `sleep`，不额外发送 `stop`
   - 用户明确要求"立刻切换"、"打断当前动作"：使用中断语义
   - 用户明确要求"不要停顿"但不是提前打断：按顺序连续执行，不额外等待

## 执行模式

### 中断模式（默认）

Immediately stops current action and executes new command.

> ⚠️ 阻塞语义下，前一条命令会占住终端直到运动结束。要**中途打断**，必须让它在后台跑。

```bash
# Move forward for 10 seconds —— 后台执行（&），否则前台会一直阻塞到 10 秒结束
./motion.sh forward 0.5 10 &

# After 2 seconds, immediately switch to turn left (interrupts forward)
sleep 2
./motion.sh turnleft 0.3 5 1
```

**Use cases:**
- Emergency stop
- Obstacle avoidance
- Dynamic path adjustment

### 队列模式

Waits for current action to complete before executing new command. Queue capacity: 1 command.

```bash
# Move forward for 5 seconds（后台执行，否则 5 秒后才轮得到第二条）
./motion.sh forward 0.5 5 &

# Queue turn left (executes after forward completes)
./motion.sh turnleft 0.3 3 0
```

> 如果前一条命令**不需要**在中途接第二条，直接前台顺序执行即可，不必用队列模式——
> 前一条返回时就代表它已经运动结束了。

**Use cases:**
- Sequential action sequences
- Planned movements
- Smooth transitions

**Queue rules:**
- Maximum 1 pending command
- Queue full: new queue commands rejected
- `stop` command: clears queue
- Interrupt mode command: clears queue

## Examples

**基础运动：**
```bash
./motion.sh forward 0.5 10        # 前进 10 秒
./motion.sh turnleft 0.3 5        # 左转 5 秒
./motion.sh stop                  # 停止
```

**精确距离/角度控制（需要计算时间）：**
```bash
# 前进 5 米（速度 0.5 m/s，需要 10 秒）
./motion.sh forward 0.5 10

# 右转 90 度（速度 0.5 rad/s，90° = π/2 ≈ 1.57 rad，需要 3.14 秒）
./motion.sh turnright 0.5 3.14

# 左转 360 度（速度 0.5 rad/s，360° = 2π ≈ 6.28 rad，需要 12.56 秒）
./motion.sh turnleft 0.5 12.56
```

**连续动作（无停顿，顺序执行）：**
```bash
# 场景：左转 360 度后直接前进 5 米
./motion.sh turnleft 0.5 12.56    # 左转一圈
./motion.sh forward 0.5 10 0      # 转完立即前进（连续执行，不额外停顿）

# 场景：前进 1 米后右转 90 度
./motion.sh forward 0.5 2         # 前进 1 米
./motion.sh turnright 0.5 3.14 0  # 走完立即右转（连续执行）
```

**多动作连续执行（整段动作链）：**
```bash
# 前进 1 米 → 右转 90° → 前进 2 米 → 左转 180°
./motion.sh forward 0.5 2 0
./motion.sh turnright 0.5 3.14 0
./motion.sh forward 0.5 4 0
./motion.sh turnleft 0.5 6.28 0
```

**中断模式（立即切换动作）：**
```bash
./motion.sh forward 0.5 10 &      # 前进 10 秒（后台执行，否则前台阻塞 10 秒）
sleep 2
./motion.sh turnleft 0.3 5 1      # 2 秒后立即切换到左转（打断前进）
```

**队列模式（顺序执行）：**
```bash
./motion.sh forward 0.5 5 &       # 前进 5 秒（后台执行，才能紧接着下发第二条）
./motion.sh turnleft 0.3 3 0      # 排队：前进完成后再左转
```

**持续运动（直到收到停止信号）：**
```bash
./motion.sh forward 0.5 0 &       # 持续前进（duration=0 会一直阻塞到 stop，必须后台）
./motion.sh stop                  # 停止：让上面那条命令返回
```

## 速度建议

**安全速度范围：**
- 前进/后退：0.1 - 1.5 m/s（推荐 0.3-0.8）
- 左移/右移：0.1 - 1.0 m/s（推荐 0.2-0.5）
- 左转/右转：0.1 - 0.5 rad/s（推荐 0.2-0.3）

**速度参考：**
- 0.1-0.3 m/s：慢速，精细调整
- 0.3-0.8 m/s：正常行走速度
- 0.8-1.5 m/s：快速移动

## 距离/角度计算

**距离控制公式：**
```
时间(秒) = 距离(米) / 速度(m/s)
```

**示例：**
- 前进 5 米，速度 0.5 m/s → 时间 = 5 / 0.5 = 10 秒
- 后退 2 米，速度 0.4 m/s → 时间 = 2 / 0.4 = 5 秒

**角度控制公式：**
```
时间(秒) = 角度(弧度) / 角速度(rad/s)
角度(弧度) = 角度(度) × π / 180
```

**常用角度转换：**
- 90° = π/2 ≈ 1.57 rad
- 180° = π ≈ 3.14 rad
- 360° = 2π ≈ 6.28 rad

**示例：**
- 右转 90°，速度 0.5 rad/s → 时间 = 1.57 / 0.5 = 3.14 秒
- 左转 180°，速度 0.3 rad/s → 时间 = 3.14 / 0.3 = 10.47 秒
- 左转 360°，速度 0.5 rad/s → 时间 = 6.28 / 0.5 = 12.56 秒

## 常见使用场景

**场景 1：前进 X 米后转 Y 度**
```bash
# 前进 3 米后右转 90 度
./motion.sh forward 0.5 6         # 3米 ÷ 0.5m/s = 6秒
./motion.sh turnright 0.5 3.14    # 90° = 1.57rad ÷ 0.5rad/s = 3.14秒
```

**场景 2：转一圈后前进**
```bash
# 左转 360 度后前进 5 米
./motion.sh turnleft 0.5 12.56    # 360° = 6.28rad ÷ 0.5rad/s = 12.56秒
./motion.sh forward 0.5 10        # 5米 ÷ 0.5m/s = 10秒
```

**场景 3：连续转向（无停顿）**
```bash
# 前进 1 米 → 右转 90° → 前进 2 米 → 左转 180°
./motion.sh forward 0.5 2
./motion.sh turnright 0.5 3.14
./motion.sh forward 0.5 4
./motion.sh turnleft 0.5 6.28
```

**场景 4：避障（立即切换方向）**
```bash
# 前进中发现障碍物，立即左转
./motion.sh forward 0.5 10 &      # 开始前进（后台，否则发不出下面的打断命令）
# ... 检测到障碍物 ...
./motion.sh turnleft 0.5 3.14 1   # 立即打断，左转 90°；被它打断的前进命令随即返回
```

## 模型执行指令模板

当用户提出运动请求时，按以下模板执行：

1. **先识别动作序列**
   - 提取每个动作的类型、速度、距离/角度/时长、先后顺序
   - 若用户给的是距离或角度，先换算为时长

2. **再判定执行语义**
   - 包含“连续执行 / 不要停顿 / 无缝衔接” → 顺序连续执行
   - 包含“立刻切换 / 打断 / 马上改为” → 中断切换执行
   - 未明确说明时，优先按用户自然语义选择最少惊扰的模式

3. **生成命令时遵循以下约束**
   - 多个已知时长动作连续执行：优先使用队列模式 `0`
   - 不要在动作之间插入 `sleep` —— 命令返回即代表这段运动已经结束
   - 不要在动作之间额外发送 `stop`
   - 只有用户明确要求打断当前动作时，才使用中断模式 `1`
   - 需要在前一条运动**进行中**下发后续命令时，把前一条放到后台（`&`）执行

4. **输出命令应可直接执行**
   - 一行一个动作命令
   - 参数完整、顺序固定
   - 默认优先使用安全速度范围

5. **存在歧义时才追问**
   - 例如速度未给且距离/角度精度要求高
   - 或“连续执行”与“立即打断”语义冲突


**Communication:** Unix Domain Socket
**Socket Path:** `/tmp/robot_dog_motion.sock`
**Response Time:** ~4ms
**Topic:** `/catch_turtle/ctrl_instruct`
**Message Type:** `geometry_msgs/msg/Twist`
**Network:** eth0 (192.168.123.99)

**Message Structure:**
```
linear.x  - Forward/Backward (-1.5 to 1.5 m/s)
linear.y  - Left/Right (-1.0 to 1.0 m/s)
linear.z  - 0 (not used)
angular.x - 0 (not used)
angular.y - 0 (not used)
angular.z - Turn Left/Right (-0.5 to 0.5 rad/s)
```

## 运行要求

- Robot dog powered on
- ROS 2 Foxy environment
- CycloneDDS configured
- Network interface: eth0

## 安全说明

- Start with low speeds (0.2-0.5)
- Always test in open space
- Keep emergency stop ready (`./motion.sh stop`)
- Monitor battery level
- Ensure robot is standing before movement

## 故障排查

**Service not running:**
```bash
ps aux | grep server
./start.sh
```

**Connection failed:**
```bash
rm -f /tmp/robot_dog_motion.sock
./start.sh
```

**Test the system:**
```bash
./test.sh
./test_interrupt.sh  # Test interrupt and queue modes
```

## 相关文档

- `README.md` - Project overview
- `GUIDE.md` - Detailed usage guide
- `INTERRUPT.md` - Interrupt and queue mode details

## 相关技能

- `robot-dog-motion` - Discrete motion commands (motorSkill)
- `robot-dog-dance` - Dance and action performances
- `robot-dog-navigation` - SLAM-based autonomous navigation
