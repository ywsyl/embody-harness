# 事件（角色间消息总线）——规范契约

> **本文是角色间结构化事件的唯一事实源。** 事件命名一律 `snake_case`；`event_type` 与各角色实现文档对齐。本版相对旧版做了大改，修订说明见文末「变更记录」。

## 1. 参与者（Actor）

| 角色 | 中文 | 类型 | 说明 |
|---|---|---|---|
| `user` / `cloud` | 用户 / 云端 | 人/外部 UI | 一切用户输入经交互器进入系统，不直接驱动其他角色 |
| `interactor` | 交互器 | **agent** | Interaction Agent，见 [Interactor/interaction_agent_implementation_v2.md](../Interactor/interaction_agent_implementation_v2.md) |
| `orchestrator` | 编排器 | **agent** | Orchestrator（含 Program Validator，同一进程同一角色），见 [Orchestrator/orchestrator_implementation_latest.md](../Orchestrator/orchestrator_implementation_latest.md) |
| `executor` | 执行器 | **code** | Programmatic Runtime：确定性解释执行 Program、调用具身工具；不决策业务 |
| `supervisor` | 监督器 | **agent** | Execution Supervisor：监控执行、确定性恢复、分级升级（L1 本地 / L2 交互器 / L3 编排器），见 [Supervisor/execution_supervisor_implementation.md](../Supervisor/execution_supervisor_implementation.md) |
| `world` / `skills` | 空间世界模型 / 具身工具层 | 非 agent 资源 | Orchestrator / Supervisor 只读或经工具调用，不参与总线 |

角色职责边界（避免旧版歧义）：
- **orchestrator 不直接调用工具**——只产出并校验 Program，交 executor。
- **supervisor 不是工具执行闸门**——它旁观执行，不替 orchestrator 派活，也不把每个工具结果回给 orchestrator。
- **executor 的业务分支由 Program 控制流决定**，不进总线；只有升级事件才出执行平面。

## 2. 统一事件头（Header）

```json
{
  "event_id": "evt_0001",
  "event_type": "execution_error",
  "source": "supervisor",
  "target": "interactor",
  "timestamp": 1788420000,
  "task_id": "task_001",
  "program_id": "program_001",
  "correlation_id": null,
  "severity": "blocking",
  "payload": {}
}
```

- `event_type`、`source`、`target`、`timestamp` 必填；`task_id` 有任务上下文时必填。
- `correlation_id`：请求-应答关联（如交互器收到 `interaction_request`，其 `interaction_id` 会原样带回 `interaction_result`）。
- `severity` 取值（仅面向用户的事件有意义）：`info` / `warning` / `blocking` / `safety`。
- 事件落盘/日志可复用 dsh 的类型化事件与会话日志，本文只定义领域层语义。

## 3. 事件目录（按 event_type）

### 3.1 面向用户的入口与任务控制

| event_type | source → target | 触发 | 关键 payload | 应答 |
|---|---|---|---|---|
| `user_message` | user → interactor | 用户自然语言输入（含云端 UI 消息归一化） | `text`, `intent?` | — |
| `user_task_create` | interactor → orchestrator | 用户下发新任务 | `goal`, `constraints?`, `task_id` | `task_status` |
| `user_task_update` | interactor → orchestrator | 用户修改任务（换目标/换人/改约束） | `task_id`, `changes` | `task_status` |
| `user_task_pause` / `user_task_resume` | interactor → orchestrator | 任务暂停/继续 | `task_id` | `task_status` |
| `user_task_cancel` | interactor → orchestrator | 任务取消 | `task_id`, `reason?` | `task_status` / `task_cancelled` |
| `task_status_request` | interactor → orchestrator | 用户询问进度 | `task_id` | `task_status` |

> 说明：`user_message` 经交互器解析后，交互语义结果走 `interaction_result`，任务级意图（新建/修改/停止/继续/取消）落成上表的任务控制事件——旧版 events.md 的"任务下发/修改/停止/继续/进度查询"即对应于此。

### 3.2 编排器 ↔ 交互器（业务/交互语义）

| event_type | source → target | 触发 | 关键 payload | 应答 |
|---|---|---|---|---|
| `interaction_request` | orchestrator → interactor | 需要向用户询问/确认/决策（缺信息、确认执行、任务阻塞需用户决定） | `interaction_id`, `purpose`: `missing_information` / `confirm` / `decision` / `task_blocked`, `requirements[]`, `context` | `interaction_result` |
| `interaction_result` | interactor → orchestrator | 用户输入解析完成（一次可满足多需求；多轮可补全同一需求） | `correlation_id`, `requirement_updates[]`(`requirement_id`,`status`,`value`), `task_action`(`update`/`cancel`/`pause`/`resume`/`continue`/`null`), `unresolved_content?` | — |
| `task_status` | orchestrator → interactor | 任务状态/进度需播报（任务已理解、program 已提交、阶段变化） | `task_id`, `phase`, `message?` | — |
| `task_replan` | orchestrator → interactor | 业务方案失效需重规划（告知用户） | `task_id`, `reason` | 通常需用户决定时转 `interaction_request` |
| `task_completed` / `task_failed` / `task_cancelled` | orchestrator → interactor | 任务最终结果 | `task_id`, `result?`, `reason?` | — |

### 3.3 执行异常升级（supervisor 对外）

| event_type | source → target | 触发 | 关键 payload | 应答 |
|---|---|---|---|---|
| `execution_warning` | supervisor → interactor | 已影响任务但非阻塞，用户应知情（L2 低限） | `task_id`, `capability`, `reason`, `severity: warning`, `affects_current_task` | — |
| `execution_error` | supervisor → interactor | 阻塞级异常，任务需暂停/用户需决定（L2） | `task_id`, `capability`, `reason`, `severity: blocking`, `recovery_attempts`, `affects_current_task` | 恢复或用户决策 |
| `execution_recovered` | supervisor → interactor | 阻塞异常已恢复（可结束 L2、恢复被打断交互） | `task_id`, `capability`, `previous_reason` | — |
| `replan_required` | supervisor → orchestrator | 当前 Program 无法继续，需重规划（L3） | `task_id`, `reason`, `context` | 编排器处理 |
| `program_error` | supervisor → orchestrator | Runtime 程序级错误且无法局部修复，需 Repair/Replan（L3） | `task_id`, `program_id`, `node_id`, `error` | 编排器修复后重校验 |
| `capability_changed` | supervisor → orchestrator | 能力状态变化（可用/降级/不可用） | `capability`, `status`(`available`/`degraded`/`unavailable`/`recovering`), `task_id?` | — |

> 上报过滤（supervisor 文档 §5）：**短暂且已自动恢复的异常不上报**；L1 本地恢复成功不产生任何事件。

### 3.4 执行平面内（executor / supervisor / 工具层，本地耦合不进总线）

executor 与 supervisor 同处执行平面，二者关系是**本地观察 + 钩子**，不是跨进程总线消息：

- executor 调用工具；supervisor 通过工具层钩子**旁观** `tool_call` / `tool_result` / 超时 / 健康 / 观测质量。
- executor 的**业务分支**（如 `found=false → search_next_area`）由 Program 控制流决定，supervisor 不干预。
- 两者由本地事件/投影共享 `capability`（能力状态）与 Program 运行态。

### 3.5 停止与安全控制面

| event_type | source → target | 触发 | 说明 |
|---|---|---|---|
| `safety_event` | supervisor → interactor | 安全事件（急停、碰撞风险、本体状态监测报警） | `severity: safety`；优先级最高，可抢占任何交互 |
| 停止链路（不落总线，按职责链传递） | user → interactor → orchestrator → executor | 用户取消/暂停 | orchestrator 下发 `program_cancel`/`program_pause`；supervisor 进入 `STOPPED` 由本地安全信号（本地急停/系统关机）直接触发 |

## 4. 路由速览

```text
用户/云端 ──user_message────────────────────────────► 交互器
交互器 ──user_task_create/update/pause/resume/cancel──► 编排器
交互器 ──interaction_result / task_status_request─────► 编排器
编排器 ──interaction_request─────────────────────────► 交互器（等 interaction_result）
编排器 ──task_status / task_replan / task_*结果───────► 交互器
编排器 ──program_submit / program_cancel──────────────► 执行器（Programmatic Runtime）
执行器 ──tool_call / tool_result（本地观察）────────────► 监督器
监督器 ──execution_warning/error/recovered──────────► 交互器
监督器 ──replan_required / program_error / capability_changed ► 编排器
监督器 ──safety_event───────────────────────────────► 交互器
```

## 5. 与角色实现文档的对应

| 本文 event_type | 交互器实现文档 §4 | 编排器实现文档 | 监督器实现文档 |
|---|---|---|---|
| `interaction_request` | `INTERACTION_REQUEST` / §13.1 | §9 接口 | — |
| `interaction_result` | `INTERACTION_RESULT` / §13.2 | §9 接口 | — |
| `execution_warning/error/recovered` | `EXECUTION_WARNING/ERROR/RECOVERED` | — | §18 `execution_warning/error/recovered` |
| `replan_required` | `TASK_REPLAN` | §22 `replan_required` | §18 `replan_required` |
| `program_error` | — | §16 `program_error` | §9 `PROGRAM_ERROR` |
| `task_completed/failed` | `TASK_COMPLETED/FAILED` | §22 `program_completed` | — |
| `user_message` | `USER_MESSAGE` | — | — |

## 6. 变更记录（相对旧版 events.md）

1. **删除「编排器发起 → 监督器 工具执行(wait_reply=True)」**：与 Orchestrator/Supervisor 文档矛盾——编排器不直接调工具，监督器不是执行闸门。工具调用由执行器发起，监督器旁观。
2. **删除「监督器发起 → 编排器 工具结果/错误报告(工具出错)」**：工具业务结果由 Runtime 按 Program 分支消费；只有升级事件（`replan_required` / `program_error` / `capability_changed`）才到编排器。
3. **新增 Programmatic Runtime（执行器）与停止/安全链路**：旧版总线只有三参与者，无法表达 program 提交、任务取消到执行的完整链路。
4. **事件命名统一 snake_case**：废弃大写枚举与中文标签（`请求信息`/`进度报告`/`err_type=客观无法达成` 等），统一映射见 §5。
5. **异常统一用 `capability` + `reason` + `severity` + `affects_current_task`**：废弃 `affects_task` / `affects_current_program` / `err_type` 等不一致写法；错误类别以监督器文档 §9 分类为准。
