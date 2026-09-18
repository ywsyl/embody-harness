# Execution Supervisor 实现文档

## 1. 模块定位

Execution Supervisor 是程序化执行链路中的**运行监控与异常恢复模块**，位于 Programmatic Runtime 与 Robot Skills / Tools 之间。

其核心职责是：

> 对程序执行过程、工具调用状态和环境观测结果进行持续监控，识别工具级、执行级和观测级异常；对可确定性恢复的问题在本地处理，对无法恢复或已经影响业务闭环的问题生成结构化事件，分别交给 Interaction Agent 或 Orchestrator。

Execution Supervisor 原则上**不负责业务任务规划，也不负责与用户直接交互**。

它重点回答的问题是：

> 当前程序和工具是否正常执行？如果不正常，能否由运行时自行恢复？

可以概括为：

```text
Orchestrator
负责：
“业务上应该怎么完成任务”

Programmatic Runtime
负责：
“按照程序怎么执行”

Execution Supervisor
负责：
“程序和工具有没有正常执行，以及异常怎么恢复”

Interaction Agent
负责：
“系统当前应该如何与用户交互”
```

---

## 2. 系统位置

整体执行链路：

```text
User Task
    ↓
Orchestrator
    ↓
Program Generation
    ↓
Program Validator
    ↓
Programmatic Runtime
    ↓
Execution Supervisor
    ↓
Robot Skills / Tools
    ↓
Physical Environment
```

Execution Supervisor 同时接收：

```text
Tool Result
Runtime State
Sensor / Observation
Tool Health
Timeout / Error Code
Robot Motion State
World Model / Spatial State
```

并输出：

```text
Normalized Result
Recovery Action
Execution Event
Capability State
REPLAN_REQUIRED
Interaction Notification Event
```

---

## 3. 与 Programmatic Runtime 的边界

Execution Supervisor 与 Programmatic Runtime 必须严格区分。

### Programmatic Runtime 负责

```text
程序节点调度
依赖关系判断
if / else 条件执行
并发控制
程序变量传递
节点状态维护
程序继续 / 结束
```

例如：

```text
search_person()
↓
result.found == true ?
├─ YES → notify_person
└─ NO  → search_next_area
```

这是 Programmatic Runtime 的职责。

### Execution Supervisor 负责

```text
vision 是否超时
navigation 是否卡死
motion 控制是否卡死（关节 / 底盘位姿停滞）
运动控制分配序是否与 Program 意图一致
服务是否离线
结果格式是否合法
观测置信度是否过低
工具是否需要 retry
工具是否需要 reconnect
能力是否已经不可用
```

> 边界：Supervisor 只监控运动控制的健康、卡死与分配序，并做确定性恢复；**不分配运动、不下发运动指令**——运动由 Program 中的 motion / 导航工具在 Runtime 内调用完成，Supervisor 不接管分配。

例如：

```text
vision timeout
↓
retry
↓
仍然失败
↓
capability unavailable
```

这是 Execution Supervisor 的职责。

因此：

> **Runtime 负责执行“程序逻辑”，Supervisor 负责保障“程序执行质量”。**

---

## 4. 与 Orchestrator 的边界

Execution Supervisor 不直接决定业务层下一步应该做什么。

例如：

```text
vision 成功运行，但没有找到小王
```

这不是执行异常，而是一个正常的业务结果：

```json
{
  "execution_status": "success",
  "business_result": {
    "found": false
  }
}
```

此时应由 Programmatic Runtime 按 Orchestrator 预先生成的程序继续执行：

```text
if found:
    notify
else:
    search_next_area
```

只有当出现：

```text
当前程序已经无法继续
当前关键能力不可用
现有程序未覆盖该状态
所有确定性恢复策略耗尽
```

才需要触发：

```text
REPLAN_REQUIRED
↓
Orchestrator
```

---

## 5. 与 Interaction Agent 的边界

Execution Supervisor 不直接向用户播报信息。

禁止：

```text
Execution Supervisor
↓
TTS
```

正确方式：

```text
Execution Supervisor
↓
Structured Event
↓
Interaction Agent
↓
Output Manager
↓
TTS / UI
```

只有以下情况才需要通知 Interaction Agent：

```text
异常已经明显影响当前任务
任务需要暂停等待恢复
用户需要知道当前状态
需要用户做决定
安全相关事件
```

短暂且已经自动恢复的异常通常不需要告诉用户。

例如：

```text
navigation 第一次 timeout
↓
Runtime Monitor 自动 retry
↓
成功
```

这种情况不需要生成用户通知。

---

## 6. Execution Supervisor 内部组成

建议将 Execution Supervisor 拆为四个核心子模块：

```text
Execution Supervisor
│
├── Runtime Monitor
├── Result Normalizer
├── Error Classifier
└── Recovery Policy Engine
```

另外可包含：

```text
Health Checker
Observation Validator
Event Reporter
Capability State Manager
```

完整结构：

```text
Tool / Skill
    ↓
Raw Result
    ↓
Result Normalizer
    ↓
Runtime Monitor
    ↓
Error / Observation Evaluation
    ↓
Error Classifier
    ↓
Recovery Policy Engine
    ↓
┌───────────────┬────────────────┬─────────────────┐
Continue       Recover         Escalate
                                  ↓
                     Interaction / Replan Event
```

---

## 7. Runtime Monitor

Runtime Monitor 负责持续监控程序执行和工具运行状态，监控面覆盖具身两端：**感知端（视觉观察）** 与**控制端（运动控制）**。运动控制只被观察（健康、进度、分配序），Supervisor 依据观察结果做 L1 恢复，不从 Program 手里接管分配。

主要监控三类信息。

### 7.1 Tool Health

包括：

```text
service alive
camera alive
navigation server alive
motion controller alive
joint / wheel controller reachable
ROS node available
HTTP endpoint reachable
process running
connection healthy
```

例如：

```text
vision service unreachable
camera process exited
navigation server connection refused
```

### 7.2 Execution State

包括：

```text
tool timeout
navigation stuck
motion control stuck（关节 / 底盘反馈停滞）
process crash
retry exceeded
execution cancelled
tool execution duration abnormal
robot motion not progressing
motion allocation order mismatch（下发顺序与 Program 意图不符）
```

例如：

```text
navigation command issued
↓
robot pose 持续 10 秒无明显变化
↓
EXECUTION_STUCK
```

### 7.3 Observation Quality

Execution Supervisor 还需要判断工具虽然“调用成功”，但结果是否可靠。

包括：

```text
image invalid
vision confidence too low
sensor data stale
motion feedback stale（关节 / 底盘反馈超时）
expected observation missing
localization confidence low
control / tracking error 超出阈值
result inconsistent with world state
```

例如：

```text
vision:
confidence = 0.21
```

虽然 API 返回成功，但当前观测质量不足。

Execution Supervisor 可以先执行：

```text
reobserve
```

而不是直接把该结果交给业务层。

---

## 8. Tool Result 标准化

不同工具可能使用完全不同的返回格式，因此建议统一封装 Tool Result。

正常执行：

```json
{
  "tool_call_id": "call_037",
  "tool": "vision_search",
  "execution_status": "success",
  "business_result": {
    "found": false
  },
  "runtime": {
    "latency_ms": 3200,
    "retry_count": 0
  },
  "error": null
}
```

工具异常：

```json
{
  "tool_call_id": "call_038",
  "tool": "vision_search",
  "execution_status": "failed",
  "business_result": null,
  "runtime": {
    "latency_ms": 5000,
    "retry_count": 2
  },
  "error": {
    "category": "CAPABILITY_UNAVAILABLE",
    "code": "VISION_OFFLINE",
    "recoverable": false
  }
}
```

其中必须严格区分：

```text
execution_status
≠
business_result
```

例如：

```text
视觉工具正常执行
但没有找到小王
```

应该表示为：

```text
execution_status = success
business_result.found = false
```

而不是：

```text
execution_status = failed
```

这个区分是 Execution Supervisor 最重要的设计原则之一。

---

## 9. Error Classifier

Execution Supervisor 需要把不同工具的底层错误统一映射到稳定的错误类别。

第一版建议至少定义：

```text
TRANSIENT
CAPABILITY_UNAVAILABLE
INVALID_RESULT
EXECUTION_STUCK
OBSERVATION_LOW_QUALITY
LOCALIZATION_ERROR
PROGRAM_ERROR
SAFETY
UNKNOWN
```

典型映射：

| 原始异常 | 标准分类 |
|---|---|
| HTTP timeout | `TRANSIENT` |
| Connection reset | `TRANSIENT` |
| Vision service offline | `CAPABILITY_UNAVAILABLE` |
| Camera process exited | `CAPABILITY_UNAVAILABLE` |
| Tool 返回非法 JSON | `INVALID_RESULT` |
| Navigation 长时间无位移 | `EXECUTION_STUCK` |
| Motion 控制器下发后长时间无位姿 / 关节反馈 | `EXECUTION_STUCK` |
| 运动分配序与 Program 意图不符 | `EXECUTION_STUCK` |
| Vision confidence 过低 | `OBSERVATION_LOW_QUALITY` |
| Motion 反馈超时 / tracking error 超阈值 | `OBSERVATION_LOW_QUALITY` |
| Robot pose 长时间不更新 | `LOCALIZATION_ERROR` |
| Program 调用了不存在的 Tool | `PROGRAM_ERROR` |
| 碰撞风险 / 急停 | `SAFETY` |

Error Classifier 输出应尽可能屏蔽底层实现差异。

例如不要向 Orchestrator 直接抛：

```text
ConnectionRefusedError: [Errno 111]
```

而应转换成：

```json
{
  "category": "CAPABILITY_UNAVAILABLE",
  "capability": "navigation",
  "recoverable": false
}
```

---

## 10. Recovery Policy Engine

Recovery Policy Engine 根据错误类别执行确定性恢复策略。

建议第一版使用规则和状态机，不使用 LLM。

例如：

```yaml
policies:

  TRANSIENT:
    action: retry
    max_retry: 2

  INVALID_RESULT:
    action: retry
    max_retry: 1

  OBSERVATION_LOW_QUALITY:
    action: reobserve
    max_retry: 2

  EXECUTION_STUCK:
    action: cancel_and_retry
    max_retry: 1

  LOCALIZATION_ERROR:
    action: relocalize
    max_retry: 1

  CAPABILITY_UNAVAILABLE:
    action: health_check

  SAFETY:
    action: stop_immediately
```

执行逻辑：

```text
Error
↓
Classify
↓
Recovery Policy
↓
当前错误可以确定性恢复？
├─ YES
│   ↓
│ local recovery
│   ↓
│ success → continue
│
└─ NO
    ↓
    escalate
```

---

## 11. 异常处理层级

建议将异常处理分成三级。

### Level 1：Local Recovery

Supervisor 自己可以处理。

例如：

```text
timeout
↓
retry
↓
success
```

不需要通知任何 Agent。

### Level 2：Interaction Notification

错误无法立即恢复，且明显影响用户任务体验。

例如：

```text
vision module offline
↓
当前任务暂停
↓
Interaction Agent
```

Supervisor 发送：

```json
{
  "event_type": "execution_error",
  "task_id": "task_001",
  "severity": "blocking",
  "capability": "vision",
  "reason": "module_offline",
  "affects_current_task": true
}
```

由 Interaction Agent 决定是否打断当前交互并向用户说明。

### Level 3：Replan

异常使当前程序的业务假设或执行路径失效。

例如：

```text
当前任务必须使用 vision
↓
vision 长期不可用
↓
现有程序无法继续
```

生成：

```json
{
  "event_type": "replan_required",
  "task_id": "task_001",
  "reason": "required_capability_unavailable",
  "context": {
    "capability": "vision"
  }
}
```

然后交给 Orchestrator。

---

## 12. Capability State

Execution Supervisor 建议维护统一的能力状态，而不仅仅是单次 Tool Result。

例如：

```yaml
capabilities:

  vision:
    status: unavailable
    last_success: 1788420000
    consecutive_failures: 3
    recovery_attempts: 2

  navigation:
    status: available
    last_success: 1788420015
    consecutive_failures: 0

  tts:
    status: degraded
    last_success: 1788420005

  motion:
    status: available
    last_success: 1788420018
    consecutive_failures: 0
```

能力按具身两端分别记录：`vision` 代表**感知端**（视觉观察），`motion` / `navigation` 代表**控制端**（运动控制）；motion 异常统一归入 `motion` 能力状态上报。

能力状态可定义为：

```text
AVAILABLE
DEGRADED
UNAVAILABLE
RECOVERING
UNKNOWN
```

Programmatic Runtime 和 Orchestrator 都可以读取 Capability State。

这样避免每个模块分别维护工具健康状态。

---

## 13. Observation Consistency Check

Execution Supervisor 不应只判断 API 是否报错，还需要判断观测与当前世界状态是否一致。

例如：

```text
navigation result:
reached = true
```

但 Spatial World Model 显示：

```text
target_region = bedroom
robot_region = corridor
```

这应该被识别为：

```text
EXECUTION_ANOMALY
```

而不是直接相信：

```text
reached = true
```

另一个例子：

```text
Vision:
bed_found = false
```

但机器人刚刚在相邻视角中高置信度看到床，而且当前位姿变化很小。

Supervisor 可以判断：

```text
observation potentially inconsistent
```

然后优先：

```text
reobserve
```

而不是立即把“床不存在”作为业务事实。

---

## 14. Execution Supervisor 状态机

建议至少维护以下状态：

```text
IDLE
MONITORING
RECOVERING
DEGRADED
BLOCKED
STOPPED
```

### IDLE

当前没有正在执行的 Tool Call 或 Program Node。

典型进入：

```text
任务未开始
上一 Tool 已完成
Program 已结束
```

典型退出：

```text
收到新的 Tool Execution
↓
MONITORING
```

### MONITORING

当前工具正在正常执行，Supervisor 持续监控：

```text
timeout
health
progress
observation quality
robot state
```

若执行成功：

```text
MONITORING
↓
IDLE / next tool
```

若发现异常：

```text
MONITORING
↓
RECOVERING / DEGRADED / BLOCKED / STOPPED
```

### RECOVERING

当前检测到可恢复异常，正在执行确定性恢复策略。

例如：

```text
retry
reconnect
reobserve
cancel_and_retry
relocalize
```

恢复成功：

```text
RECOVERING
↓
MONITORING
```

恢复失败：

```text
RECOVERING
↓
DEGRADED / BLOCKED
```

### DEGRADED

部分能力异常，但任务仍有可能继续。

例如：

```text
后置摄像头不可用
前置摄像头仍可工作
```

或者：

```text
定位精度下降
但仍高于最低执行阈值
```

Supervisor 可以继续允许执行，但需要：

```text
标记 capability degraded
提高监控等级
必要时通知 Runtime / Orchestrator
```

### BLOCKED

当前异常导致 Program 无法继续。

例如：

```text
任务必须使用 vision
vision unavailable
且恢复失败
```

进入：

```text
BLOCKED
```

此时必须至少生成：

```text
execution_error
```

必要时同时生成：

```text
replan_required
```

### STOPPED

由于安全、用户取消或系统级停止要求，执行已经终止。

例如：

```text
emergency stop
collision risk
user cancel
system shutdown
```

该状态优先级最高。

---

## 15. 典型执行流程

以视觉搜索为例：

```text
Programmatic Runtime
↓
execute vision_search
↓
Execution Supervisor
↓
MONITORING
↓
vision timeout
↓
Error Classifier
TRANSIENT
↓
Recovery Policy
retry
↓
vision timeout again
↓
retry_count = 2
↓
health_check
↓
vision service offline
↓
CAPABILITY_UNAVAILABLE
↓
BLOCKED
```

Supervisor 此时生成：

```text
1. capability state update
2. execution_error
3. replan_required（如果当前程序依赖 vision）
```

然后：

```text
Interaction Agent
↓
向用户告知任务暂时无法继续

Orchestrator
↓
判断是否存在替代业务方案
```

以运动控制分配为例：

```text
Programmatic Runtime
↓
allocate motion（导航 / 关节运动）
↓
Execution Supervisor（MONITORING）
↓
motion allocation order mismatch
│
├─ 可确定性修正（按 Program 意图重排）
│   ↓
│   L1 本地恢复 → continue
│
└─ motion control stuck / feedback stale
    ↓
    EXECUTION_STUCK
    ↓
    cancel_and_retry
    ↓
    retry 失败
    ↓
    motion: degraded / unavailable
```

Supervisor 只判定并做确定性恢复，不自行重新分配运动；业务无法闭环时仍走 `replan_required`。

---

## 16. 正常业务失败与执行失败的区分

这是整个模块最重要的边界之一。

### 情况一：没有找到小王

```text
vision tool = success
found = false
```

属于：

```text
正常业务结果
```

交给 Programmatic Runtime：

```text
if not found:
    search_next_area
```

### 情况二：Vision 服务不可用

```text
vision tool = failed
service = offline
```

属于：

```text
执行异常
```

交给 Execution Supervisor。

### 情况三：Vision 返回低置信度结果

```text
vision tool = success
confidence = 0.18
```

属于：

```text
观测质量异常
```

Execution Supervisor 优先：

```text
reobserve
```

若仍然无法获得可靠观测，再判断是否：

```text
degraded
blocked
replan_required
```

---

## 17. 与 Spatial World Model 的关系

Execution Supervisor 可以读取 Spatial World Model，但不负责维护完整空间语义。

它主要利用 Spatial World Model 做：

```text
执行一致性校验
目标到达检查
机器人位姿变化检查
观测合理性检查
navigation stuck 判断
```

例如：

```text
navigation command issued
↓
持续读取 robot pose
↓
连续 N 秒位姿变化低于阈值
↓
EXECUTION_STUCK
```

或者：

```text
navigation says reached
↓
query current region
↓
current region != target region
↓
execution anomaly
```

---

## 18. Event Reporter

Execution Supervisor 对外输出统一结构化事件。

### execution_warning

```json
{
  "event_type": "execution_warning",
  "task_id": "task_001",
  "severity": "warning",
  "capability": "navigation",
  "reason": "retrying"
}
```

### execution_error

```json
{
  "event_type": "execution_error",
  "task_id": "task_001",
  "severity": "blocking",
  "capability": "vision",
  "reason": "module_offline",
  "recovery_attempts": 3,
  "affects_current_task": true
}
```

### execution_recovered

```json
{
  "event_type": "execution_recovered",
  "task_id": "task_001",
  "capability": "vision",
  "previous_error": "module_offline"
}
```

### replan_required

```json
{
  "event_type": "replan_required",
  "task_id": "task_001",
  "reason": "required_capability_unavailable",
  "context": {
    "capability": "vision"
  }
}
```

---

## 19. Supervisor 主流程

第一版可采用确定性事件循环。

参考伪代码：

```python
async def supervise_tool_call(tool_call):

    set_state("MONITORING")

    try:
        raw_result = await execute_tool(tool_call)

    except Exception as error:
        normalized_error = classify_runtime_exception(error)

        return await handle_error(
            tool_call,
            normalized_error
        )

    result = normalize_result(raw_result)

    quality_issue = observation_validator.check(result)

    if quality_issue:
        return await handle_error(
            tool_call,
            quality_issue
        )

    consistency_issue = consistency_checker.check(
        result,
        spatial_world_model
    )

    if consistency_issue:
        return await handle_error(
            tool_call,
            consistency_issue
        )

    capability_state.mark_success(tool_call.tool)

    set_state("IDLE")

    return result
```

异常处理：

```python
async def handle_error(tool_call, error):

    policy = recovery_policy.get(error.category)

    if policy.can_recover():
        set_state("RECOVERING")

        result = await execute_recovery(
            tool_call,
            policy
        )

        if result.success:
            set_state("MONITORING")
            return result

    capability_state.update_failure(
        tool_call.tool,
        error
    )

    if error.requires_stop:
        set_state("STOPPED")
        emit_safety_event(error)
        return

    if current_program_can_continue_without(tool_call.tool):
        set_state("DEGRADED")
        emit_warning(error)
        return

    set_state("BLOCKED")

    emit_execution_error(error)

    if current_program_invalidated(error):
        emit_replan_required(error)
```

整个流程不需要持续调用 LLM。

---

## 20. 第一版实现范围

第一版建议优先实现：

1. Tool Result Normalizer
2. Runtime Monitor
3. Timeout Detection
4. Retry
5. Health Check
6. Error Classifier
7. Recovery Policy Engine
8. Capability State
9. Retry Exhausted
10. Observation Confidence Check
11. Navigation Stuck Detection
12. Motion Controller Health / Stuck Detection
13. Motion Allocation Order Check
14. `execution_warning`
15. `execution_error`
16. `execution_recovered`
17. `replan_required`
18. 与 Interaction Agent 的异常通知接口
19. 与 Orchestrator 的重规划触发接口
20. 与 Programmatic Runtime 的执行结果接口

第一阶段建议优先跑通：

```text
Tool Call
↓
Monitor
↓
Failure
↓
Classify
↓
Retry
↓
Retry Success
↓
Continue Program
```

以及：

```text
Tool Call
↓
Failure
↓
Retry Exhausted
↓
Capability Unavailable
↓
BLOCKED
↓
Interaction Event
+
REPLAN_REQUIRED
```

---

## 21. 设计原则总结

Execution Supervisor 可以概括为：

> **Execution Supervisor = Runtime Monitoring + Result Normalization + Error Classification + Deterministic Recovery + Capability Management + Event Escalation**

核心原则：

- **不负责业务任务规划；**
- **不直接与用户交互；**
- **不对每个错误调用 LLM；**
- **可确定性恢复的问题优先在本地处理；**
- **严格区分业务失败与执行失败；**
- **严格区分 Tool Success 与 Business Success；**
- **工具底层错误先标准化，再向上层暴露；**
- **只有当前程序无法继续时才触发 `REPLAN_REQUIRED`；**
- **影响用户的异常通过 Interaction Agent 统一输出；**
- **Programmatic Runtime 执行程序逻辑，Execution Supervisor 保障执行过程。**

一句话定义：

> **Execution Supervisor 是 PACE 框架中的程序执行保障层，负责监控、校验和恢复程序化工具执行过程，并将无法确定性处理的异常升级为交互事件或重规划事件。**
