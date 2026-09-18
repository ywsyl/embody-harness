> **已弃用（2026-09-10）**：本文件只是草稿，不是代码的事实源，也不与代码做任何机械关联。
> 角色间事件的唯一事实源是 [`dsh-plugins/rolebus/src/event-catalog.ts`](../dsh-plugins/rolebus/src/event-catalog.ts)；
> 两者不一致时以代码为准。本文件待删除。

source	event_type	targets	载荷草案
interactor	task_content_update	orchestrator	event_type(create modify cancel stop)、description、task_id
interactor	interaction_result	orchestrator	task_id、description
orchestrator	interaction_request	interactor	task_id、description
orchestrator	task_status_update	interactor	task_id, status, description
supervisor	observe_event	interactor	source description
supervisor	robot_status_update	orchestrator	name status

1、待做任务清单
- 每条记录：
  - task_id 
  - description 
  - status
  - create_time
  - last_update_time
  - 执行记录
    - 工具&结果&attempts...

interactor - 全量读 & 写任务内容
orchestrator - 全量读 & 写任务状态


2、机器狗状态清单
- 系统状态
  - cpu
  - 内存
  - 电量
  - 网络
  - 当前地图
  - 运动姿态：运动中、站立、趴下
  ...

- 工具状态/可用性
  ...

interactor - 只读
orchestrator - 只读
supervisor - 读写