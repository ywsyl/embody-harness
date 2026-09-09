# Orchestrator 实现文档

## 1. 模块定位

Orchestrator 是 PACE 架构中的**任务业务编排与程序生成模块**。

其核心职责是：

> 根据用户长程任务、当前 Task State、可用能力、空间/环境状态以及已有交互信息，判断任务是否具备执行条件；在信息与能力满足要求后，生成可由 Programmatic Runtime 直接执行的闭环 Program；当现有 Program 因业务状态变化或运行异常失效时，负责修复、续规划或重规划。

Orchestrator **不是执行智能体**，不负责逐步调用 Tool。

其核心工作是：

```text
理解任务
↓
分析任务所需信息与能力
↓
Execution Readiness Check
↓
必要时请求 Interaction Agent 补充信息
↓
生成闭环 Program
↓
Program Validator
↓
提交给 Programmatic Runtime
↓
等待执行结果 / 重规划事件
```

一句话定义：

> **Orchestrator 负责“生成怎么完成任务的闭环程序”，Programmatic Runtime 负责“运行这个程序”。**

---

## 2. 系统位置

```text
                     User
                      ↕
              Interaction Agent
                      ↕
                 Orchestrator
                      ↓
               Program Generation
                      ↓
               Program Validator
                 ↓          ↑
               PASS       Repair
                 ↓          │
             Programmatic Runtime
                      ↓
              Execution Supervisor
                      ↓
               Robot Skills / Tools
                      ↓
               Physical Environment

              Spatial World Model
                 ↑            ↓
            Observation    Spatial Query
```

模块边界：

```text
Orchestrator
→ 业务任务闭环与 Program 生成

Program Validator
→ Program 执行前合法性、能力和逻辑校验

Programmatic Runtime
→ Program 调度、条件执行和 Tool 调用

Execution Supervisor
→ 执行监控、异常识别和确定性恢复

Interaction Agent
→ 用户交互与信息补充

Spatial World Model
→ 空间状态、对象记忆和空间查询
```

---

## 3. Orchestrator 不是什么

Orchestrator 不采用传统的逐 Tool Agent Loop：

```text
LLM → Tool → Result → LLM → Tool → ...
```

新的执行模式是：

```text
Orchestrator LLM
↓
一次生成当前任务阶段的闭环 Program
↓
Program Validator
↓
Programmatic Runtime
↓
连续执行多个 Tool
↓
当前 Program 能处理？
├─ YES → Runtime 继续
└─ NO  → 返回 Orchestrator
```

因此，传统的 Execution Agent 被 Programmatic Runtime 替代。Orchestrator 不应该成为高频 Tool 调用器。

---

## 4. 核心职责

Orchestrator 主要负责六类工作：

1. Task Understanding
2. Information / Capability Dependency Analysis
3. Execution Readiness Check
4. Program Generation
5. Program Repair
6. Replan / Continue Planning

总体流程：

```text
Task Intake
↓
Task Understanding
↓
Dependency Analysis
↓
Execution Readiness Check
↓
Program Generation
↓
Program Validation
↓
Runtime Execution
↓
Execution Feedback
↓
Continue / Repair / Replan / Complete
```

---

## 5. 输入

Orchestrator 不应只读取用户原始 Prompt，而应读取统一 Task State，包括：

```text
Task Goal
Task Constraints
Known Task Information
Pending Information Requirements
Interaction Results
Available Capabilities
Capability State
Spatial / World Context
Execution History
Current Program State
Business Results
Replan / Program Error Events
```

示例：

```yaml
task:
  task_id: task_001
  goal: 找到小王并通知他去518开会
  status: preparing

known_information:
  target:
    name: 小王
    expected_location: "517"
    description: null
  destination: "518"

capabilities:
  navigation:
    status: available
  vision_search:
    status: available
  speak:
    status: available

world:
  robot_region: "518"
  target_last_seen: null

program:
  current_program_id: null
  status: not_generated

interaction:
  pending_requirements: []
```

---

## 6. Task Understanding

Orchestrator 首先将用户长程任务解析成业务目标、约束和关键任务要素。

例如：

```text
用户：去517找小王，让他来518开会。
```

得到：

```yaml
goal:
  type: find_and_notify_person

target:
  name: 小王
  expected_location: "517"

notification:
  destination: "518"
  purpose: 开会
```

这一阶段只描述任务要完成什么，不立即执行 Tool。

---

## 7. Information / Capability Dependency Analysis

任务理解完成后，需要分析后续执行依赖哪些信息和能力。

例如业务链路：

```text
navigate_to_517
↓
identify_and_find_wang
↓
notify_wang
```

对应依赖：

```text
navigate_to_517
需要：517 可导航

identify_and_find_wang
需要：vision_search 可用、小王身份可识别

notify_wang
需要：speak 可用、通知内容明确
```

Orchestrator 应区分：已知信息、可由环境/工具自主获取的信息、可延迟获取的信息、执行前必须由用户补充的信息。

建议未知信息分类：

```text
UNKNOWN_RESOLVABLE
UNKNOWN_DEFERABLE
UNKNOWN_BLOCKING
```

---

## 8. Execution Readiness Check

Execution Readiness Check 必须发生在真正执行 Program 之前。它回答：

> **当前信息、能力和环境条件是否足以支持后续任务形成闭环？**

不能只检查当前第一步是否能执行。

例如“去517找小王”，即使 517 已知且 navigation 可用，也不能立刻执行导航，还必须检查：到达517后是否有能力找到小王、是否知道如何识别小王、缺失身份信息能否自主获得，以及是否应该在机器人离开用户前询问。

### 8.1 Pre-execution Blocking Information

如果某项信息满足：

```text
后续关键步骤必需
AND 当前未知
AND 机器人无法自主可靠获取
AND 必须在物理 Commitment Point 前获得
```

则定义为：

```text
PRE_EXECUTION_BLOCKING
```

形式化：

```text
NeedAskUser =
Required
AND NotKnown
AND NotAutonomouslyResolvable
AND NeededBeforeCommitment
```

例如 agent 不认识小王，而 vision_search 需要外貌特征，则 `target_description` 属于 PRE_EXECUTION_BLOCKING。此时禁止先执行 `navigation("517")`，必须先请求 Interaction Agent 补充信息，再重新进行 Readiness Check。

### 8.2 Commitment Point

Commitment Point 指执行某个物理动作之后，再向用户补充关键信息会明显增加成本、延迟，或者已经无法自然完成交互的节点，例如机器人离开用户、进入另一房间、开始长距离导航、进入电梯或开始不可逆操作。

因此 Readiness Check 不只是“下一步能不能执行”，而是：

> **跨过下一个 Commitment Point 后，后续关键步骤是否已经具备闭环条件？**

---

## 9. 与 Interaction Agent 的接口

发现阻塞信息时，Orchestrator 不直接向用户说话，而是生成结构化 Interaction Request：

```json
{
  "event_type": "interaction_request",
  "task_id": "task_001",
  "purpose": "missing_information",
  "requirements": [
    {
      "requirement_id": "req_target_description",
      "field": "target_description",
      "description": "需要能够识别小王的外貌特征",
      "reason": "required_before_person_search"
    }
  ]
}
```

Interaction Agent 负责组织自然语言、询问用户、解析输入，并更新一个或多个 Requirement。

返回示例：

```json
{
  "event_type": "interaction_result",
  "task_id": "task_001",
  "requirement_updates": [
    {
      "requirement_id": "req_target_description",
      "status": "satisfied",
      "value": {
        "clothes": "红色短袖",
        "glasses": "黑框眼镜"
      }
    }
  ]
}
```

Orchestrator 更新 Task State 后重新进行 Readiness Check。

---

## 10. Program Generation

Readiness Check 通过后，Orchestrator 生成当前任务阶段的闭环 Program。

Program 应覆盖：Action、Observation / Result、Condition、Success Branch、Business Failure Branch 和 Unhandled State。

示例：

```yaml
program_id: program_001

nodes:
  - id: navigate_517
    tool: navigation
    args:
      location: "517"
    preconditions:
      - capability.navigation == AVAILABLE
      - target_identity_resolvable == true
    on_success: search_wang
    on_failure: REPLAN_REQUIRED

  - id: search_wang
    tool: vision_search
    args:
      target:
        name: 小王
        description: "${task.target.description}"
    success_condition:
      expression: "${result.found == true}"
    on_success: notify_wang
    on_business_failure: search_next_area
    on_unhandled: REPLAN_REQUIRED

  - id: search_next_area
    tool: vision_search
    args:
      target:
        name: 小王
        description: "${task.target.description}"
      region: nearby_area
    success_condition:
      expression: "${result.found == true}"
    on_success: notify_wang
    on_business_failure: report_not_found
    on_unhandled: REPLAN_REQUIRED

  - id: notify_wang
    tool: speak
    args:
      text: "请去518开会。"
    on_success: COMPLETED
    on_failure: REPLAN_REQUIRED

  - id: report_not_found
    action: interaction_request
    args:
      purpose: task_blocked
      reason: target_not_found
```

---

## 11. 受约束 Program DSL

第一版不建议让 Orchestrator 生成任意 Python / TypeScript。任意代码会增加安全风险、静态校验难度、不可控循环、非法系统调用、状态污染、工具权限绕过和调试难度。

更推荐受约束 DSL，只允许：

```text
Tool Call
Condition
Variable Reference
Dependency
Branch
Retry Policy Reference
Success / Failure Exit
Interaction Request
Replan Request
```

由 Programmatic Runtime 解释执行。

---

## 12. Programmatic Runtime 如何执行 Program

Program 通过 Validator 后，直接交给 Programmatic Runtime，而不是交给另一个 LLM Agent 一步步调用 Tool。

```text
Program
↓
Programmatic Runtime
↓
解释控制流
↓
直接调用 Tool
```

例如：

```text
navigate_517
↓
navigation result
↓
Runtime 判断 on_success
↓
search_wang
↓
vision result
↓
Runtime 判断 result.found
↓
notify_wang / search_next_area
```

中间不需要再次调用 Orchestrator LLM。

> **Orchestrator 负责写 Program，Runtime 负责跑 Program。**

---

## 13. Program Validator

Orchestrator 生成 Program 后禁止直接进入机器人执行，必须经过 Program Validator / Program Guard。

```text
Orchestrator
↓
Program Generation
↓
Program Validator
↓
是否合法、可执行？
├─ YES → Runtime
└─ NO  → Program Repair
```

Program Validator 至少执行三层校验。

### 13.1 Schema / Syntax Validation

检查 Program 格式、必要字段、字段类型、Condition 格式和 DSL 是否能够解析。

### 13.2 Tool / Capability Validation

根据 Tool Registry 检查 Tool 是否存在、是否允许调用、参数名和类型、必填参数、返回字段、能力状态和权限策略。

### 13.3 Static Logic Validation

检查变量是否先定义后引用、Condition 引用是否存在、goto/branch 目标、depends_on、不可达节点、无出口节点、非法循环、关键结果是否被处理、Precondition 以及 Success / Failure 出口。

---

## 14. Program Validation Result

通过：

```json
{
  "validation_status": "passed",
  "program_id": "program_001",
  "errors": []
}
```

失败：

```json
{
  "validation_status": "failed",
  "program_id": "program_001",
  "errors": [
    {
      "type": "UNDEFINED_VARIABLE",
      "node_id": "search_wang",
      "field": "condition",
      "detail": "result.person is not defined by any upstream node"
    }
  ]
}
```

---

## 15. Program Repair

Program Validator 校验失败后，应返回 Orchestrator 修复，而不是交给 Execution Supervisor。

```text
Generate
↓
Validate
↓
Failed
↓
Orchestrator Repair
↓
Validate
↓
Passed
↓
Runtime
```

Repair 应保持原任务目标、已经确认的信息和无需修改的节点，只针对 Validation Error 修改必要部分。

建议设置：

```text
MAX_PROGRAM_REPAIR = 2 或 3
```

超过限制后进入 `PROGRAM_GENERATION_FAILED`，再决定完整重规划或通过 Interaction Agent 告知任务暂时无法执行。

---

## 16. Runtime Program Error

即使 Program Validator 通过，也可能在真实运行时出现 Program Error，例如 condition evaluation error、unexpected tool return structure、runtime variable missing 或 argument construction error。

此时：

```text
Programmatic Runtime
↓
Execution Supervisor
↓
PROGRAM_ERROR
↓
停止当前 Program Segment
↓
结构化上报
↓
Orchestrator
```

示例：

```json
{
  "event_type": "program_error",
  "task_id": "task_001",
  "program_id": "program_001",
  "node_id": "search_wang",
  "error": {
    "type": "RUNTIME_VARIABLE_ERROR",
    "detail": "result.found is unavailable"
  }
}
```

Orchestrator 决定 Repair Program 或 Replan。修复后仍必须重新经过 Program Validator。

Execution Supervisor **不负责修改 LLM 生成的 Program**。

---

## 17. Business Failure、Program Error 与 Tool Error

### Business Failure

例如视觉正常执行但 `found=false`，属于正常业务结果。如果 Program 已定义 `found == false → search_next_area`，Runtime 直接继续，不调用 Orchestrator。

### Program Error

例如 Program 引用了不存在的变量，属于程序错误，需要 Execution Supervisor 上报 `PROGRAM_ERROR`，由 Orchestrator Repair / Replan。

### Tool / Runtime Error

例如 vision timeout、camera offline、navigation stuck，属于执行异常，优先由 Execution Supervisor 确定性恢复。只有恢复失败且当前 Program 无法继续时，才触发 `REPLAN_REQUIRED`。

---

## 18. Replan 触发条件

Programmatic Runtime 不应该每执行一个 Tool 就返回 Orchestrator。只有当前 Program 无法覆盖新的业务状态时，才触发重规划。

典型情况：

```text
现有 Program 未覆盖当前业务状态
原计划关键业务假设失效
用户修改任务目标
关键能力长期不可用
环境变化导致原业务路径不可行
Program fallback 全部耗尽
Runtime Program Error 无法局部 Repair
```

例如卧室门锁住，而当前 Program 没有 `door_locked` 分支，则 Runtime / Supervisor 触发 `REPLAN_REQUIRED` 返回 Orchestrator。

---

## 19. Replan 与 Repair 的区别

Program Repair：业务目标和整体策略仍然正确，只是 Program 本身存在技术性错误，例如变量引用、Tool 参数、Condition 或节点引用错误。

Replan：原有业务方案已经无法成立，例如目标地点变化、关键能力不可用、门锁住、用户修改任务或目标拒绝请求。

```text
Program 本身写错 → Repair
现实世界让原方案失效 → Replan
```

---

## 20. Program Segment

Orchestrator 不一定需要一次生成整个超长任务的完整程序。更推荐：

> **One LLM Call per Decision Segment**

```text
任务整体目标
↓
生成当前可稳定执行的 Program Segment
↓
Runtime 连续执行
↓
到达新的高层决策点
↓
必要时 Orchestrator 再规划
```

Program Segment 应尽量覆盖当前阶段正常路径、可预见业务异常、必要 fallback，以及明确的完成 / 重规划出口。

---

## 21. Orchestrator 状态

建议维护：

```text
IDLE
ANALYZING
WAITING_INTERACTION
GENERATING
VALIDATING
REPAIRING
SUBMITTED
REPLANNING
COMPLETED
FAILED
```

### IDLE
当前没有待处理的任务规划请求。

### ANALYZING
进行 Task Understanding、Dependency Analysis 和 Execution Readiness Check。信息不足进入 WAITING_INTERACTION；信息充分进入 GENERATING。

### WAITING_INTERACTION
存在 Pre-execution Blocking Information，已向 Interaction Agent 提交 Requirement。收到 Interaction Result 后回到 ANALYZING 重新执行 Readiness Check。

### GENERATING
Orchestrator LLM 正在生成 Program / Program Segment，完成后进入 VALIDATING。

### VALIDATING
Program Validator 正在进行 Schema、Tool 和 Static Logic Validation。通过进入 SUBMITTED，失败进入 REPAIRING。

### REPAIRING
根据 Validator 或 Runtime 返回的 Program Error 修复 Program。修复完成后重新 VALIDATING；超过最大修复次数进入 FAILED。

### SUBMITTED
Program 已提交给 Programmatic Runtime。Orchestrator 不参与逐 Tool 执行，只等待 COMPLETED、REPLAN_REQUIRED、PROGRAM_ERROR 或 INTERACTION_REQUIRED 等事件。

### REPLANNING
当前 Program 因业务条件变化无法继续，基于最新 Task State 和 World State 重新规划。新 Program 仍需经过 Validator 后提交。

### COMPLETED
任务业务目标已经完成。

### FAILED
经过规定的规划、修复或恢复策略后仍无法生成可执行 Program，或者任务已经确定无法完成。

---

## 22. 与其他模块的接口

### → Interaction Agent

用于缺失执行前必要信息、业务执行需要用户确认、任务阻塞需要用户决定，输出 `interaction_request`。

### ← Interaction Agent

接收用户补充信息、任务修改、确认/拒绝，典型事件为 `interaction_result`、`user_task_update`。

### → Program Validator

输出 Program、Task Constraints、Tool Registry Snapshot、Capability Requirements。

### ← Program Validator

接收 `validation_passed`、`validation_failed` 和 `validation_errors`。

### → Programmatic Runtime

只提交 **Validated Program**，禁止提交未经 Validator 通过的 Program。

### ← Execution Supervisor / Runtime

可能接收：

```text
program_completed
program_error
replan_required
business_state_update
capability_changed
```

Orchestrator 据此决定 continue、repair、replan、complete 或 fail。

---

## 23. 第一版实现流程

```text
User Task
↓
Task Understanding
↓
Dependency Analysis
↓
Readiness Check
↓
Missing Information?
├─ YES
│   ↓
│ Interaction Agent
│   ↓
│ User Information
│   ↓
│ Readiness Check
│
└─ NO
    ↓
Program Generation
↓
Program Validator
├─ FAIL → Repair → Validate
└─ PASS
    ↓
Programmatic Runtime
↓
Execution Supervisor
↓
Program 能处理？
├─ YES → Runtime继续
└─ NO
    ↓
REPLAN_REQUIRED
↓
Orchestrator
```

---

## 24. 第一版实现范围

1. Task Context Builder
2. Task Understanding
3. Dependency Analysis
4. Execution Readiness Check
5. Pre-execution Blocking Information
6. Commitment Point 检查
7. Interaction Request
8. Program DSL Generation
9. Program Validator
10. Schema Validation
11. Tool / Parameter Validation
12. Static Logic Validation
13. Program Repair
14. Repair 次数限制
15. Program Submission
16. Program Error 接收
17. `REPLAN_REQUIRED`
18. Replan
19. Task State 更新
20. Program Segment 管理

---

## 25. 核心设计原则

- **先检查任务是否具备闭环执行条件，再生成 Program。**
- **关键缺失信息必须在 Commitment Point 之前补齐。**
- **不能等机器人已经离开用户后，才发现缺少后续关键任务信息。**
- **Orchestrator 不逐步调用 Tool。**
- **Orchestrator 生成 Program，Programmatic Runtime 直接执行 Program。**
- **Runtime 执行 Program 时不需要每一步重新调用 Orchestrator LLM。**
- **Program 在进入 Runtime 前必须经过 Program Validator。**
- **Program Validator 负责发现语法、接口和静态逻辑问题。**
- **Program 写错由 Orchestrator Repair，不由 Execution Supervisor 修改。**
- **Tool / Runtime 异常优先由 Execution Supervisor 确定性恢复。**
- **业务结果由 Program 控制流处理。**
- **当前 Program 无法覆盖新的业务状态时才触发 Replan。**
- **优先生成受约束 DSL，而不是任意 Python / TypeScript。**
- **优先生成当前稳定决策阶段的 Program Segment，而不是一次规划整个超长任务。**

---

## 26. 一句话总结

> **Orchestrator 是 PACE 框架中的业务任务编排与程序生成模块：它在执行前完成信息与能力就绪检查，在条件满足后生成经过校验的闭环 Program，并仅在 Program 错误或现实业务状态使当前方案失效时进行修复或重规划；实际 Tool 调用由 Programmatic Runtime 直接执行。**
