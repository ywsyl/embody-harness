# Interaction Agent 实现文档


## 1. 模块定位

Interaction Agent 负责整个具身智能体系统与用户之间的统一交互管理，包括：

-   任务追问与信息补全
-   信息确认
-   执行进度播报
-   异常告知
-   任务结果反馈
-   用户主动插话
-   被高优先级事件打断后的交互恢复

Interaction Agent
**不负责业务任务规划，也不负责工具异常恢复**。其核心职责是：

> 接收来自用户、编排智能体、执行监控模块等不同来源的交互事件，根据当前交互状态和事件优先级进行仲裁，并决定何时、以何种方式向用户输出信息，以及如何处理用户回复。

整体流程：

``` text
Event Receiver
      ↓
Priority / Policy Engine
      ↓
Interaction State Manager
      ↓
Need Semantic Reasoning?
     /        \
   NO          YES
   ↓            ↓
Template   Interaction LLM
     \          /
       ↓
  Output Manager
       ↓
    TTS / UI
```

Interaction Agent 整体采用**事件驱动架构**，不运行持续调用大模型的固定
Agent Loop。

------------------------------------------------------------------------

## 2. 输入与输出

Interaction Agent
不直接依赖其他模块的内部实现，而通过标准化事件接收信息。

### 2.1 输入来源（event_type 一律 snake_case，全量见 reference/events.md）

```text
用户 / 云端
 ↓
user_message

Orchestrator
 ↓
interaction_request            需要询问 / 确认 / 决策，等待 interaction_result
task_status                    任务状态 / 进度播报
task_replan / task_completed / task_failed / task_cancelled

Execution Supervisor
 ↓
execution_warning
execution_error
execution_recovered
safety_event
```

> 面向用户的最终结果与进度统一由 **Orchestrator** 经总线转达；**Programmatic Runtime 不直接面向交互器**——执行结果先回到 Orchestrator / Supervisor（两者职责分离、可同进程，见 reference/events.md §3.4）。

### 2.2 输出方向

``` text
Interaction Agent
      │
      ├──→ 用户
      │     TTS
      │     UI
      │
      └──→ 系统内部
            USER_REPLY
            INTERACTION_RESULT
            USER_CANCEL
            USER_MODIFICATION
```

Interaction Agent
将系统内部事件转化为用户交互，并将用户自然语言回复转化为结构化交互结果：

``` text
System Event
      ↓
Interaction Event
      ↓
User Interaction
      ↓
Structured Interaction Result
```

------------------------------------------------------------------------

## 3. Event Receiver

Event Receiver 是 Interaction Agent 的统一事件入口。

其主要职责：

1.  接收来自用户、Orchestration Agent、Execution
    Supervisor、Programmatic Runtime 等模块的事件；
2.  校验事件格式；
3.  补充时间戳等公共字段；
4.  根据 `task_id`、`interaction_id` 建立事件关联；
5.  将事件写入统一事件队列。

建议统一事件格式：

``` json
{
  "event_id": "evt_001",
  "event_type": "execution_error",
  "source": "execution_supervisor",
  "task_id": "task_001",
  "timestamp": 1788420000,
  "severity": "blocking",
  "payload": {
    "module": "vision",
    "error_code": "MODULE_OFFLINE",
    "summary": "视觉模块离线",
    "affects_current_task": true
  }
}
```

用户输入同样事件化：

``` json
{
  "event_id": "evt_002",
  "event_type": "user_message",
  "source": "user",
  "task_id": "task_001",
  "timestamp": 1788420010,
  "payload": {
    "text": "小王今天穿红色短袖"
  }
}
```

后续模块统一处理 Event，而不直接区分 ROS、HTTP、TTS、UI 等底层接口。

------------------------------------------------------------------------

## 4. 事件类型定义

第一版建议至少支持以下事件（规范 `event_type`，全量路由以 reference/events.md 为准）：

| event_type | 来源 | 含义 |
|---|---|---|
| `user_message` | 用户 / 云端 | 用户主动输入 |
| `interaction_request` | Orchestrator | 需要向用户询问 / 确认 / 决策（等 `interaction_result`） |
| `interaction_result` | 交互器 → Orchestrator | 用户输入解析后的结构化结果 |
| `task_status` | Orchestrator | 任务状态 / 进度播报 |
| `task_replan` | Orchestrator | 业务方案失效、重规划（告知用户） |
| `task_completed` / `task_failed` / `task_cancelled` | Orchestrator | 任务最终结果 |
| `execution_warning` | Supervisor | 非阻塞执行异常，用户应知情 |
| `execution_error` | Supervisor | 阻塞级执行异常 |
| `execution_recovered` | Supervisor | 阻塞异常恢复 |
| `safety_event` | Supervisor / 本地安全系统 | 安全事件（最高优先级） |

> 旧图 / 旧表内的大写枚举（`USER_MESSAGE`、`EXECUTION_ERROR`…）与"Programmatic Runtime → 交互器"的直连不再使用；统一走 snake_case + 编排器/监督器转达，见 reference/events.md §5 对照。

### 4.1 interaction_request


Orchestration Agent 不应直接向 Interaction Agent
发送已经组织好的自然语言，而应发送结构化交互需求。

例如，不建议：

``` text
“请问小王穿什么衣服？”
```

建议：

``` json
{
  "event_type": "interaction_request",
  "payload": {
    "purpose": "missing_information",
    "field": "target_description",
    "description": "需要获得小王的可识别外貌特征",
    "required": true
  }
}
```

最终如何向用户表达，由 Interaction Agent 决定。

------------------------------------------------------------------------

## 5. Priority / Policy Engine

Priority / Policy Engine 负责解决：

> 当前正在与用户交互时，又收到一个新的交互事件，应该立即处理、排队、忽略，还是打断当前交互？

### 5.1 事件优先级

第一版建议采用固定优先级：

  Priority   类型                   示例
  ---------- ---------------------- ----------------------------------
  P0         Safety                 急停、碰撞风险、严重安全问题
  P1         Blocking Error         视觉离线、定位丢失、任务无法继续
  P2         User Initiative        用户主动停止、修改任务、新指令
  P3         Required Interaction   必要信息追问、用户确认
  P4         Task Result            任务完成、失败结果
  P5         Progress               "正在前往518"
  P6         Informational          非关键提示

### 5.2 抢占策略

优先级并不直接等价于打断，还需要结合 Interrupt Policy。

例如当前正在：

``` text
P3：
“请描述一下小王的外貌特征。”
```

此时收到：

``` text
P1：
“视觉模块离线。”
```

处理方式：

``` text
P1 > P3
→ 抢占
→ 当前 P3 进入 SUSPENDED
→ 处理 P1
```

如果收到的是：

``` text
P5：
“已经移动了3米。”
```

则：

``` text
P5 < P3
→ 不抢占
→ QUEUE 或 SUPPRESS
```

因此实际仲裁逻辑为：

``` text
Priority
    +
Interrupt Policy
    ↓
INTERRUPT / HANDLE / QUEUE / SUPPRESS / IGNORE
```

------------------------------------------------------------------------

## 6. Interaction State Manager

Priority / Policy Engine 决定"哪个事件先处理"，Interaction State Manager
负责维护"当前交互进行到哪里"。

### 6.1 状态定义

建议至少包括以下六种状态：

  -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
  状态              说明                                                                                                                                               典型进入条件                                       典型退出条件
  ----------------- -------------------------------------------------------------------------------------------------------------------------------------------------- -------------------------------------------------- -----------------------------------------------
  `IDLE`            当前没有正在处理的交互事务，也没有等待用户回复。Interaction Agent 可以直接接收并处理新的交互事件。                                                 初始化完成；一次交互完整结束；挂起事务均已清空     收到需要处理的系统事件或用户输入后进入
                                                                                                                                                                                                                          `PROCESSING`

  `PROCESSING`      正在处理一个或多个输入事件，包括优先级仲裁、上下文整合、判断是否需要语义推理、调用模板或 Interaction LLM                                           从 `IDLE` 收到事件；从 `WAITING_USER`              需要向用户输出时进入
                    等。该状态表示"正在决定怎么处理"，尚未向用户输出。                                                                                                 收到用户新输入；收到可抢占当前事务的高优先级事件   `OUTPUTTING`；无需输出且处理完成时进入 `IDLE`
                                                                                                                                                                                                                          或恢复原状态

  `OUTPUTTING`      Output Manager 正在通过 TTS/UI 向用户输出消息。该状态用于统一管理播报排队、抢占、去重和输出完成回调。                                              `PROCESSING` 产生需要向用户输出的内容；`RESUMING`  输出后需要用户回答则进入
                                                                                                                                                                       需要输出恢复提示                                   `WAITING_USER`；输出完成且无需等待则进入
                                                                                                                                                                                                                          `IDLE`；被更高优先级消息抢占时重新仲裁

  `WAITING_USER`    已向用户提出一个或多个待回应的信息需求，当前系统等待用户继续提供信息。等待对象不是单一 event，而是一组尚未满足的 `pending_requirements`。          `OUTPUTTING`                                       收到用户输入后进入
                                                                                                                                                                       完成追问/确认，且仍存在待满足的信息需求            `PROCESSING`；被高优先级事件抢占时进入
                                                                                                                                                                                                                          `SUSPENDED`；所有需求满足后结束当前交互

  `SUSPENDED`       当前交互事务因更高优先级事件而暂时挂起。挂起时保留未满足的信息需求、已有用户回答、上下文和任务关联，不应丢弃。                                     `WAITING_USER`、`PROCESSING` 或必要时 `OUTPUTTING` 抢占事件处理完成且允许恢复时进入
                                                                                                                                                                       被高优先级事件抢占                                 `RESUMING`；原任务取消/失效时直接结束挂起事务

  `RESUMING`        正在恢复之前被挂起的交互事务。恢复时需要重新检查原有信息需求是否仍然有效、挂起期间是否已经被用户输入部分或全部满足，以及是否需要重新向用户追问。   高优先级事件结束、故障恢复、任务恢复执行           仍需用户补充时经 `OUTPUTTING` 进入
                                                                                                                                                                                                                          `WAITING_USER`；需求已满足则进入 `PROCESSING`
                                                                                                                                                                                                                          并向上游提交结果；原事务已失效则进入 `IDLE`
  -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

状态之间的典型转换：

``` text
IDLE
  ↓ 新事件
PROCESSING
  ↓ 需要输出
OUTPUTTING
  ├─ 无需用户回复 ─────────────→ IDLE
  └─ 存在待满足信息需求
              ↓
        WAITING_USER
          ├─ 用户输入 ─────────→ PROCESSING
          └─ 高优先级事件
                  ↓
              SUSPENDED
                  ↓ 抢占事件结束
               RESUMING
                ├─ 仍需追问 → OUTPUTTING → WAITING_USER
                ├─ 信息已满足 → PROCESSING
                └─ 原事务失效 → IDLE
```

需要注意，`WAITING_USER`
不应被理解为"等待某一个问题的一次回答"，而应理解为：

> 当前存在一组尚未满足的交互信息需求，系统允许用户通过一次或多次自然语言输入逐步满足这些需求。

### 6.2 状态数据

建议将交互状态从"单一 active question"调整为"当前交互事务 +
待满足信息需求集合"：

``` yaml
interaction_state:
  status: WAITING_USER

  current_session:
    session_id: session_023
    task_id: task_001

  pending_requirements:
    - requirement_id: req_001
      type: missing_information
      field: target_description
      description: 小王的可识别外貌特征
      status: partially_filled
      value:
        clothes: 红色短袖

    - requirement_id: req_002
      type: missing_information
      field: meeting_room
      description: 通知小王前往的会议室
      status: unresolved
      value: null

  user_inputs:
    - message_id: msg_101
      text: "小王穿红色短袖。"
      processed: true

  suspended_sessions: []
  pending_events: []
```

这里不强制建立"一条用户回复 ↔ 一个事件"的一一绑定关系。Interaction State
Manager 维护的是：

-   当前有哪些尚未满足的信息需求；
-   每个需求已经获得了哪些信息；
-   哪些需求已经满足、部分满足或仍未满足；
-   用户的每次输入对哪些需求产生了更新；
-   是否产生了新的任务修改、取消或其他意图。

------------------------------------------------------------------------

## 7. Interaction Stack 与交互抢占

Interaction Stack 用于保存被高优先级事件打断的交互上下文。

例如当前：

``` text
Interaction Agent：
“请描述一下小王的外貌特征。”

state = WAITING_USER
active_interaction = int_001
```

Execution Supervisor 上报：

``` text
vision_module_offline
priority = P1
```

Interaction Agent 应执行：

``` text
1. active interaction 压入 suspended stack
2. 创建新的异常 interaction
3. 向用户告知视觉模块暂时不可用
4. 等待恢复或进一步处理
```

状态变为：

``` yaml
active:
  id: int_002
  type: execution_error

suspended:
  - id: int_001
    type: missing_information
    field: target_description
```

视觉恢复并收到 `EXECUTION_RECOVERED` 后：

``` text
结束 int_002
↓
pop int_001
↓
恢复之前的问题
```

可向用户输出：

> 视觉模块已经恢复。刚才还需要您补充一下小王的外貌特征。

该机制可以概括为：

``` text
Suspend → Interrupt → Resume
```

------------------------------------------------------------------------

## 8. Interaction Resolution：用户输入与交互需求解析

不建议将用户回答设计成严格的 **Reply
Binding（用户回复绑定到某一个事件）**。

自然交互中，用户输入与系统交互需求通常不是一一对应关系，而是**多对多关系**：

``` text
一个用户输入
    ↓
可以同时满足多个 pending requirements

多个用户输入
    ↓
也可以共同补全同一个 pending requirement
```

例如系统当前存在两个待补充需求：

``` text
req_001：小王的外貌特征
req_002：会议地点
```

用户一次回答：

> 小王今天穿红色短袖、戴黑框眼镜，让他去518。

这一条输入应同时更新：

``` text
req_001 → target_description = 红色短袖 + 黑框眼镜
req_002 → meeting_room = 518
```

反过来，同一个需求也可能通过多轮输入逐步补齐：

``` text
System:
请描述一下小王的外貌特征。

User #1:
他穿红色短袖。

→ req_001 = partially_filled

User #2:
哦对了，还戴着黑框眼镜。

→ req_001 = satisfied
```

因此这里更适合实现为：

> **Interaction Resolution / Requirement Resolution**

而不是 Reply Binding。

### 8.1 处理逻辑

每次收到 `USER_MESSAGE` 后，Interaction Agent 应读取：

``` text
当前用户输入
+
pending_requirements
+
suspended_sessions 中仍有效的 requirements
+
当前任务上下文
+
近期交互历史
```

然后判断该输入产生了哪些语义更新：

``` text
USER_MESSAGE
      ↓
Interaction Resolution
      ↓
┌─────────────────────────────────┐
│ 更新 req_001                    │
│ 更新 req_002                    │
│ 新增 task modification          │
│ 新增 cancel / stop intent       │
│ 仍有未解析内容                  │
└─────────────────────────────────┘
      ↓
更新 Interaction State
```

建议 Interaction LLM 输出"更新集合"，而不是单一 `interaction_id`：

``` json
{
  "requirement_updates": [
    {
      "requirement_id": "req_001",
      "status": "satisfied",
      "value": {
        "clothes": "红色短袖",
        "glasses": "黑框眼镜"
      }
    },
    {
      "requirement_id": "req_002",
      "status": "satisfied",
      "value": "518"
    }
  ],
  "task_action": null,
  "unresolved_content": null
}
```

如果用户只提供了部分信息：

``` json
{
  "requirement_updates": [
    {
      "requirement_id": "req_001",
      "status": "partially_filled",
      "value": {
        "clothes": "红色短袖"
      }
    }
  ]
}
```

系统保留已有值，继续等待后续输入。

### 8.2 与异常抢占的关系

例如：

``` text
t0 Interaction Agent：
“请描述一下小王的外貌特征。”

t1 Execution Supervisor：
视觉模块离线。

t2 Interaction Agent：
“视觉模块暂时不可用……”

t3 User：
“小王穿红色短袖，戴眼镜，另外让他去518。”
```

即使原交互当前处于 `SUSPENDED`，Interaction Resolution
仍可以读取其中有效的 `pending_requirements`，并解析出：

``` text
target_description → 红色短袖 + 戴眼镜
meeting_room       → 518
```

因此挂起的是**交互流程**，不是把用户语义输入锁死到当前 active event。

### 8.3 Requirement 状态

建议每个待补充信息需求维护：

``` text
UNRESOLVED
PARTIALLY_FILLED
SATISFIED
INVALIDATED
```

这样可以自然支持：

``` text
一次回答 → 多个需求
多次回答 → 一个需求
用户修改已有答案
任务变化 → 原需求失效
```

Interaction Agent 的目标不是判断"这句话在回答哪个事件"，而是：

> **判断这次用户输入对当前任务和所有有效交互需求造成了哪些状态更新。**

## 9. Need Semantic Reasoning

Interaction Agent 不应对所有事件调用 LLM。

建议实现：

``` text
NeedSemanticReasoning(event, interaction_state)
```

用于判断当前事件是否需要语义推理。

### 9.1 不需要 LLM 的场景

确定性事件优先使用 Template Engine，例如：

``` text
TASK_COMPLETED
VISION_OFFLINE
NAVIGATION_TIMEOUT
EXECUTION_RECOVERED
PROGRESS_UPDATE
```

例如：

``` text
VISION_OFFLINE
+
blocking
→
“视觉模块当前不可用，任务暂时无法继续。”
```

这类信息无需调用模型。

### 9.2 需要 LLM 的场景

主要包括：

-   用户自由文本回复；
-   用户回复存在歧义；
-   用户修改原任务；
-   一个回答可能对应多个交互；
-   需要结合上下文生成追问；
-   多个异常需要合并解释；
-   恢复交互时需要自然衔接；
-   无法通过规则完成 Interaction Resolution。

例如：

> 算了，不去找小王了，看看小张在不在吧。

Interaction LLM 可以解析为：

``` json
{
  "intent": "modify_task",
  "modifications": {
    "target_person": "小张"
  }
}
```

随后 Interaction Agent 将结构化任务修改请求发送给 Orchestration Agent。

------------------------------------------------------------------------

## 10. Interaction LLM

Interaction LLM 只负责交互层面的语义处理，不负责规划机器人动作。

### 10.1 职责

包括：

-   理解用户回复；
-   判断用户输入对哪些待满足信息需求产生更新；
-   提取用户补充信息；
-   识别任务修改；
-   识别任务取消；
-   生成自然语言追问；
-   生成需要上下文组织的用户反馈。

### 10.2 能力边界

Interaction LLM 不应拥有：

``` text
navigation
vision
motion
```

等机器人执行工具。

可允许其产生的系统动作包括：

``` text
submit_interaction_result
modify_task_request
cancel_task_request
```

例如：

输入：

``` text
Active question:
需要知道小王外貌特征

User:
他今天应该穿的是红色短袖，戴个黑框眼镜。
```

输出：

``` json
{
  "action": "interaction_result",
  "interaction_id": "int_001",
  "result": {
    "target_description": {
      "clothes": "红色短袖",
      "glasses": "黑框眼镜"
    }
  },
  "reply": "好的，我知道了。"
}
```

Interaction Agent 将结构化 `result` 返回给 Orchestration Agent。

------------------------------------------------------------------------

## 11. Template Engine

简单、确定、结构化程度高的消息应优先使用模板，而不是 LLM。

例如：

``` yaml
vision_offline:
  blocking:
    "视觉模块当前不可用，任务暂时无法继续。"

vision_recovered:
  "视觉模块已经恢复。"

navigation_retry:
  "导航出现异常，我正在重新尝试。"

task_completed:
  "任务已经完成。"
```

模板也可以参数化：

``` text
“{module_name} 当前不可用，正在进行第 {retry_count} 次重试。”
```

Template Engine 的主要价值：

-   低延迟；
-   行为稳定；
-   内容可控；
-   减少 LLM 调用；
-   避免模型对确定性系统状态产生错误解释。

------------------------------------------------------------------------

## 12. Output Manager

Output Manager 是整个系统唯一真正向用户输出信息的组件。

负责：

``` text
TTS
UI
文本输出
播报打断
消息排队
消息去重
消息合并
```

Orchestrator、Execution Supervisor、Programmatic Runtime 等其他模块原则上禁止直接调用
TTS/UI，而应统一通过：

``` text
Orchestrator / Execution Supervisor / Programmatic Runtime
      ↓
Interaction Agent
      ↓
Output Manager
      ↓
TTS / UI
```

建议维护输出状态：

``` yaml
output_state:
  tts_status: playing
  current_message_id: msg_001
  interruptible: true
  queue: []
```

例如当前正在播报：

> 我正在前往518。

此时收到 P1：

> 视觉模块下线。

则：

``` text
interrupt current TTS
→ 播报视觉异常
```

但普通 P5 进度事件不应随意打断当前播报。

------------------------------------------------------------------------

## 13. 模块接口设计

### 13.1 Orchestration Agent → Interaction Agent：交互请求

``` json
{
  "type": "interaction_request",
  "task_id": "task_001",
  "purpose": "missing_information",
  "required_fields": [
    "target_description"
  ],
  "context": {
    "target": "小王"
  }
}
```

### 13.2 Interaction Agent → Orchestration Agent：交互结果

``` json
{
  "type": "interaction_result",
  "task_id": "task_001",
  "interaction_id": "int_001",
  "result": {
    "target_description": "红色短袖，黑框眼镜"
  }
}
```

### 13.3 Execution Supervisor → Interaction Agent：执行异常

``` json
{
  "type": "execution_error",
  "task_id": "task_001",
  "module": "vision",
  "severity": "blocking",
  "reason": "module_offline",
  "affects_current_task": true
}
```

模块间优先传递结构化语义，而不是直接传递最终用户话术。

------------------------------------------------------------------------

## 14. Interaction Agent 主流程

Interaction Agent 主体采用 Event Loop，而不是持续调用大模型的 Agent
Loop。

参考实现：

``` python
async def handle_event(event):
    # 1. 标准化事件
    event = event_receiver.normalize(event)

    # 2. 根据优先级、策略和当前交互状态进行仲裁
    decision = policy_engine.evaluate(
        event,
        interaction_state
    )

    if decision.action == "IGNORE":
        return

    if decision.action == "QUEUE":
        event_queue.put(event)
        return

    if decision.action == "INTERRUPT":
        state_manager.suspend_current()

    # 3. 构造当前交互上下文
    context = state_manager.prepare(event)

    # 4. 判断是否需要语义推理
    if need_semantic_reasoning(event, context):
        result = await interaction_llm.process(
            event,
            context
        )
    else:
        result = template_engine.render(
            event,
            context
        )

    # 5. 统一输出
    await output_manager.output(result)

    # 6. 更新交互状态
    state_manager.update(result)
```

外层可以存在事件循环：

``` python
while running:
    event = await event_queue.get()
    await handle_event(event)
```

但该循环是 **Event Loop**，不是 **Agent LLM Loop**。

即：

> `handle_event()` 持续运行，但 LLM 仅在需要语义推理时调用。

------------------------------------------------------------------------

## 15. 典型案例：交互过程中视觉模块离线

### 15.1 发起追问

Orchestration Agent 发现需要补充小王的外貌信息：

``` text
Orchestration Agent
↓
INTERACTION_REQUEST
“需要获取小王的外貌特征”
↓
Interaction Agent / Event Receiver
↓
Priority = P3
↓
Interaction State = WAITING_USER
↓
Interaction LLM / Template
↓
“请描述一下小王的外貌特征。”
↓
Output Manager
↓
TTS / UI
```

### 15.2 异常抢占

等待用户回复期间，Execution Supervisor 检测到视觉模块离线：

``` text
Execution Supervisor
↓
EXECUTION_ERROR
vision_offline
severity = blocking
↓
Event Receiver
↓
Policy Engine
P1 > 当前 P3
↓
Suspend current interaction
↓
Template
“视觉模块当前不可用，任务暂时无法继续。”
↓
Output Manager
↓
TTS / UI
```

此时：

``` text
active = vision_error
suspended = target_description_question
```

### 15.3 模块恢复

Execution Supervisor 上报：

``` text
EXECUTION_RECOVERED
```

Interaction Agent 执行：

``` text
结束异常 interaction
↓
State Manager pop suspended
↓
RESUMING
↓
“视觉模块已经恢复。刚才还需要您描述一下小王的外貌特征。”
↓
WAITING_USER
```

### 15.4 用户回答

用户：

> 红色短袖，黑框眼镜。

Interaction Agent：

``` text
USER_MESSAGE
↓
Interaction Resolution
↓
Interaction LLM
↓
target_description =
“红色短袖，黑框眼镜”
↓
INTERACTION_RESULT
↓
Orchestration Agent
↓
继续 Program
```

整个过程中：

-   A 的业务任务不会因工具异常丢失；
-   Interaction Agent 的原始追问不会因异常播报丢失；
-   Execution Supervisor 不直接与用户交互；
-   TTS/UI 始终只有一个统一出口。

------------------------------------------------------------------------

## 16. 第一版实现范围

第一版建议优先实现以下能力：

1.  **Event Queue**\
    所有交互相关事件统一进入队列。

2.  **Priority Policy**\
    建立 P0～P6 优先级以及
    `INTERRUPT / HANDLE / QUEUE / SUPPRESS / IGNORE` 策略。

3.  **Interaction State**\
    优先跑通 `IDLE / WAITING_USER / SUSPENDED` 三个核心状态。

4.  **Interaction Stack**\
    支持当前问题被异常打断后恢复。

5.  **Interaction Resolution**\
    支持一次用户输入更新多个信息需求，也支持多次输入逐步补全同一信息需求。

6.  **Template Engine**\
    工具异常、恢复、进度、完成等确定性事件不调用 LLM。

7.  **Interaction LLM**\
    只处理用户自由输入、复杂追问、任务修改、交互需求解析等语义问题。

8.  **Output Manager**\
    成为整个系统唯一 TTS/UI 输出出口，统一负责排队、抢占和去重。

9.  **Structured Result**\
    Interaction Agent 与 Orchestration Agent / Execution Supervisor
    之间传递结构化结果，而不是依赖自然语言。

第一阶段建议优先打通以下闭环：

``` text
任务追问
↓
等待用户
↓
工具异常抢占
↓
异常告知
↓
工具恢复
↓
恢复原追问
↓
用户一次或多次补充信息
↓
Interaction Resolution 更新一个或多个需求
↓
结构化结果返回 Orchestration Agent
```

该链路跑通后，Interaction Agent 的核心架构即基本成立。

------------------------------------------------------------------------

## 17. 设计原则总结

Interaction Agent 可以概括为：

> **Interaction Agent = Event-driven Interaction Orchestrator + State
> Machine + Priority Arbitration + On-demand LLM + Unified Output
> Manager**

核心原则：

-   **事件驱动，而非固定 LLM Agent Loop；**
-   **系统始终维护确定性的交互状态；**
-   **高优先级事件允许抢占，原交互可挂起并恢复；**
-   **确定性事件使用模板，语义问题才调用 LLM；**
-   **用户输入应解析为对一个或多个有效交互需求及任务状态的结构化更新；**
-   **Orchestrator、Execution Supervisor、Programmatic Runtime 不直接与用户交互；**
-   **Output Manager 是 TTS/UI 的唯一出口；**
-   **Interaction LLM 负责交互语义，不参与机器人业务执行规划。**

从系统定位上，Interaction Agent 并不是一个持续"思考下一句话"的聊天
Agent，而是整个具身智能体系统的**统一交互控制面（Interaction Control
Plane）**。
