/**
 * rolebus 的角色间事件目录 —— 本项目「谁发什么、发给谁、带哪些字段」的唯一事实源。
 *
 * 每条事件声明 `(source, event_type)` 身份、唯一 `target` 和模型可见的参数表。角色
 * profile 的 `rolebus.role` 选定 source，`apply` 据此为本角色注册
 * `rolebus_send_<event_type>` 工具。**目录是发送的唯一通道**：不在这张表里的事件
 * 没有任何工具可以发出去，参数形状由引擎的工具参数校验把关。
 *
 * 参数分两段落进信封：与信封可选头字段同名的键（`task_id` / `program_id` /
 * `correlation_id` / `severity`）写进事件头，其余全部进 `payload`。这个分区在
 * `index.ts` 的 `createEventTool` 里实现，是本文件与信封之间唯一的约定点。
 *
 * 每条还声明**送达时的唤醒方式**（`wakeMode`）：收件侧的常驻角色进程按本角色这条条目
 * 决定怎么叫醒它的 agent（分派实现见 `resident.ts` 的 `wakeResident`）。这是**收件侧
 * 行为**——不是信封字段、不过 wire，所以 Python 侧的信封副本
 * （`embody/plugin_tools/rolebus.py`）与 `scripts/rolebus-emit.sh` 都不需要知道它。
 * 目录里查不到的事件（收件侧不按目录校验，手工 emit 的临时事件就是这种）用
 * {@link DEFAULT_WAKE_MODE}。
 *
 * 事件集覆盖任务清单通知、交互请求/回填、执行平面观察与本体状态四类；任务清单本身
 * 由 interactor 与 orchestrator 分别持有并每轮拼进上下文，因此没有查询类事件。
 *
 * interactor+orchestrator 与 planner 是**两个并存的版本**，同一套第三方事件各有一份、
 * 各投各的角色（`planner_` 前缀即 planner 版）：目录主键是 `(source, event_type)`、
 * `target` 又是单值，一个名字不可能同时投两个目标角色，所以只能另起名。两个版本不同时
 * 启动，旧版的那 8 条与 planner 的 4 条互不影响。planner 只收不发，因此它的 4 条
 * `source` 都是第三方（supervisor / cloud），目录里没有 `source: planner` 的条目。
 *
 * `cloud` 是**外部发送方**：云端不属于本项目的 dsh 进程，没有 profile 承载它，因此不会
 * 有 `rolebus_send_cloud_*` 工具。它的发送方是 `embody/plugin_tools/rolebus.py`（直写
 * spool）；本表是这几条事件的形状事实源，两处必须一致。
 * @module @embody/dsh-rolebus/event-catalog
 */

import type { ParameterSchemaSpec } from '@deepseek-ai/dsh-tools'
import type { RoleBusEnvelope, RoleName } from './types.ts'

/**
 * 一条角色间事件：身份、路由、唤醒方式与模型可见的参数表。
 * @property event_type - snake_case 事件类型；与 `source` 共同构成目录主键。
 * @property source - 发出该事件的角色身份（= 注册该工具的 profile 的 `rolebus.role`）。
 * @property target - 唯一接收角色；必须是发送方 profile 的 `peers` 之一。
 * @property wakeMode - 送达 `target` 角色时怎么唤醒它的常驻 agent；**必填**（取值域与三种
 * 方式的差别见 {@link WAKE_MODES}，收件侧分派见 `resident.ts`）。
 * @property description - 模型可见的工具描述：这条事件是干什么的、什么时候发。
 * @property parameters - 平铺的工具参数；与信封可选头字段同名的进事件头，其余进 payload。
 */
export interface RoleBusEventSpec {
  readonly event_type: string
  readonly source: RoleName
  readonly target: RoleName
  readonly wakeMode: WakeMode
  readonly description: string
  readonly parameters: ParameterSchemaSpec
}

/** 任务清单条目的动作：新建、改描述、用户取消、用户叫停。 */
const TASK_ACTIONS = ['create', 'modify', 'cancel', 'stop'] as const

/**
 * 云端下发的动作词表，逐字取自 `embody/Communicator/文档/端侧智能体接口文档.md`
 * （云端 `robot_agent_task` 的 `action`）。导出是给 Python 侧副本比对用的。
 */
export const CLOUD_ACTIONS = ['create', 'modify', 'interrupt', 'answer', 'info'] as const

/**
 * 事件送达本角色时的唤醒方式，取值逐字对应引擎 `Agent` 上的三个同名方法
 * （`packages/core/agent/src/runtime-types.ts`），投递时一对一调用，不留隐式改写：
 *   - `followup`：排进下一轮。角色空闲时立刻开一轮；忙碌时等这一轮收敛后独占新的一轮，
 *     每封事件多花一次模型请求。
 *   - `steer`：交给最近的一个步骤边界。角色空闲时同样开一轮；忙碌时搭在这一轮已有的
 *     请求上（同一轮内、没有轮边界），代价是模型可能中途改道。
 *   - `inject`：只投进队、不唤醒。忙碌时跟随本来就会发生的那一步，最省；**空闲时没人
 *     会取走它**——常驻角色空闲是常态，这封事件会一直停在收件箱里，直到别的唤醒事件顺带
 *     把它带出，或进程退出时随取消清空；而收件侧已把它写进归档、不会重投，所以是丢件。
 *     只有确定本角色总有活跃轮次时才用。
 */
export const WAKE_MODES = ['followup', 'steer', 'inject'] as const
/** 唤醒方式；取值域与三种方式的差别见 {@link WAKE_MODES}。 */
export type WakeMode = (typeof WAKE_MODES)[number]

/**
 * 目录外事件的唤醒方式。收件侧不按目录校验（见 {@link findEventSpec}），手工 emit 的临时
 * 事件、以及本进程目录副本较旧时别的角色新加的事件都落在这条路径上；已投递的事件不能丢，
 * 所以给定值而不是静默丢弃或抛错。取值与目录内 12 条当前一致。
 */
export const DEFAULT_WAKE_MODE: WakeMode = 'steer'

/** 全量事件目录。按 source 分组排列（interactor / orchestrator / supervisor / cloud），
 * 末尾是 planner 版的 4 条第三方事件（source 仍是 supervisor / cloud，见文件头注释）。 */
export const ROLEBUS_EVENTS: readonly RoleBusEventSpec[] = [
  {
    event_type: 'task_content_update',
    source: 'interactor',
    target: 'orchestrator',
    wakeMode: 'steer',
    description: '当用户新增/修改/取消/叫停一个具身任务时，实时通知 orchestrator 任务清单改变了。',
    parameters: {
      task_id: {
        type: 'string',
        required: true,
        description: '任务的编号。',
      },
      action: {
        type: 'string',
        required: true,
        enum: [...TASK_ACTIONS],
        description: '任务的改动类型。',
      },
      task_description: {
        type: 'string',
        required: true,
        description: '任务的描述，以自然语言形式表示，转述用户的全部要求。'
      },
    },
  },
  {
    event_type: 'interaction_result',
    source: 'interactor',
    target: 'orchestrator',
    wakeMode: 'steer',
    description: '回答一个早期的 interaction_request。',
    parameters: {
      task_id: {
        type: 'string',
        required: true,
        description: '涉及的任务编号。'
      },
      answer: {
        type: 'string',
        required: true,
        description: '包含编排器的提问，和用户回复的答案。',
      },
    },
  },
  {
    event_type: 'interaction_request',
    source: 'orchestrator',
    target: 'interactor',
    wakeMode: 'steer',
    description: '请求用户补充/回答执行必须但尚未明确、或无法获取的任务信息，通常是编排器在执行任务前发现了不明确信息，或执行任务时需要用户指示下一步怎么做。',
    parameters: {
      task_id: {
        type: 'string',
        required: true,
        description: '涉及的任务编号。',
      },
      type: {
        type: 'string',
        required: true,
        description: '明确请求类型：请求补全信息 / 系统级通知。'
      },
      content: {
        type: 'string',
        required: true,
        description: '说明请求内容。'
      },
    },
  },
  {
    event_type: 'task_status_update',
    source: 'orchestrator',
    target: 'interactor',
    wakeMode: 'steer',
    description: '告诉 interactor 某个任务的状态改变了，如任务阶段性完成或遇到了问题。',
    parameters: {
      task_id: {
        type: 'string',
        required: true,
        description: '任务的编号。',
      },
      status: {
        type: 'string',
        required: true,
        description: '任务的新状态。',
      },
      reason: {
        type: 'string',
        required: true,
        description: '任务状态改变的原因。'
      },
    },
  },
  {
    event_type: 'observe_event',
    source: 'supervisor',
    target: 'interactor',
    wakeMode: 'steer',
    description: '报告一个观察到的事件，通常是本体或环境的状态改变、异常或警告。',
    parameters: {
      component: {
        type: 'string',
        required: true,
        description: '观察到该事件的能力或子系统，例如 camera、navigation、body。',
      },
      description: {
        type: 'string',
        required: true,
        description: '说明事件的类型、观察的内容等。',
      },
    },
  },
  {
    event_type: 'robot_status_update',
    source: 'supervisor',
    target: 'orchestrator',
    wakeMode: 'steer',
    description: '报告本体状态改变或异常，通常是工具的上下线、本体或环境的状态异常等。',
    parameters: {
      name: {
        type: 'string',
        required: true,
        description: '组件或能力的名称。',
      },
      status: {
        type: 'string',
        required: true,
        description: '新的状态。'
      },
    },
  },
  // cloud 两条由 Python 侧直写 spool（见文件头注释），不注册发件工具。
  {
    event_type: 'cloud_content_update',
    source: 'cloud',
    target: 'interactor',
    wakeMode: 'steer',
    description: '通知 interactor ：云端下发了一个智能体任务事件。',
    parameters: {
      task_id: {
        type: 'string',
        required: true,
        description: '云端下发的任务 ID。',
      },
      action: {
        type: 'string',
        required: true,
        enum: [...CLOUD_ACTIONS],
        description: '云端任务事件类型： create | modify | interrupt | answer | info.',
      },
      description: {
        type: 'string',
        required: true,
        description: '云端下发的任务内容，以自然语言描述。',
      },
    },
  },
  {
    event_type: 'cloud_status_update',
    source: 'cloud',
    target: 'orchestrator',
    wakeMode: 'steer',
    description: '告诉编排器云端改变了某个任务的状态。',
    parameters: {
      task_id: {
        type: 'string',
        required: true,
        description: '任务编号。',
      },
      action: {
        type: 'string',
        required: true,
        enum: [...CLOUD_ACTIONS],
        description: '云端任务事件类型： resume | stop | cancel.',
      },
      description: {
        type: 'string',
        required: true,
        description: '任务内容，以自然语言描述。'
      },
    },
  },
  // planner 版：与上面 4 条第三方事件一一对应，只换 target 与名字（见文件头注释）。
  // planner 只收不发，这批条目的 source 仍是第三方，故不注册任何发件工具。
  {
    event_type: 'planner_observe_event',
    source: 'supervisor',
    target: 'planner',
    wakeMode: 'steer',
    description: '报告一个观察到的事件，通常是本体或环境的状态改变、异常或警告。',
    parameters: {
      component: {
        type: 'string',
        required: true,
        description: '观察到该事件的能力或子系统，例如 camera、navigation、body。',
      },
      description: {
        type: 'string',
        required: true,
        description: '说明事件的类型、观察的内容等。',
      },
    },
  },
  {
    event_type: 'planner_robot_status_update',
    source: 'supervisor',
    target: 'planner',
    wakeMode: 'steer',
    description: '报告本体状态改变或异常，通常是工具的上下线、本体或环境的状态异常等。',
    parameters: {
      name: {
        type: 'string',
        required: true,
        description: '组件或能力的名称。',
      },
      status: {
        type: 'string',
        required: true,
        description: '新的状态。'
      },
    },
  },
  {
    event_type: 'planner_cloud_content_update',
    source: 'cloud',
    target: 'planner',
    wakeMode: 'steer',
    description: '通知 planner ：云端下发了一个智能体任务事件。',
    parameters: {
      task_id: {
        type: 'string',
        required: true,
        description: '云端下发的任务 ID。',
      },
      action: {
        type: 'string',
        required: true,
        enum: [...CLOUD_ACTIONS],
        description: '云端任务事件类型： create | modify | interrupt | answer | info.',
      },
      description: {
        type: 'string',
        required: true,
        description: '云端下发的任务内容，以自然语言描述。',
      },
    },
  },
  {
    event_type: 'planner_cloud_status_update',
    source: 'cloud',
    target: 'planner',
    wakeMode: 'steer',
    description: '告诉 planner 云端改变了某个任务的状态。',
    parameters: {
      task_id: {
        type: 'string',
        required: true,
        description: '任务编号。',
      },
      action: {
        type: 'string',
        required: true,
        enum: [...CLOUD_ACTIONS],
        description: '云端任务事件类型： resume | stop | cancel.',
      },
      description: {
        type: 'string',
        required: true,
        description: '任务内容，以自然语言描述。'
      },
    },
  },
]

/**
 * 取某个角色发出的事件条目。
 * @param role - 角色身份（= `rolebus.role`）。
 * @returns 目录中 `source` 为该角色的条目，保持目录顺序。
 */
export function eventsForRole(role: RoleName): readonly RoleBusEventSpec[] {
  return ROLEBUS_EVENTS.filter(spec => spec.source === role)
}

/**
 * 判断一个角色名是否出现在目录里（作为任一事件的 source 或 target）。
 *
 * 装配时用它区分「角色名拼错」与「只收不发」：只收角色（如 planner）在目录里没有任何
 * `source` 为自己的条目，但它是别人事件的 target，因此仍是一个已登记的角色。
 * @param role - 待判定的角色身份。
 * @returns 该角色在目录里出现过则为 `true`。
 */
export function isKnownRole(role: RoleName): boolean {
  return ROLEBUS_EVENTS.some(spec => spec.source === role || spec.target === role)
}

/**
 * 按目录主键取一条事件条目。收件侧渲染模型消息时用它取回事件语义；收件侧不按目录校验，
 * 因此查不到是正常情况而非错误。
 * @param source - 发出该事件的角色。
 * @param eventType - 事件类型。
 * @returns 命中的条目，目录外为 `undefined`。
 */
export function findEventSpec(source: RoleName, eventType: string): RoleBusEventSpec | undefined {
  return ROLEBUS_EVENTS.find(spec => spec.source === source && spec.event_type === eventType)
}

/**
 * 取一封信封该用的唤醒方式，按目录主键（`source` + `event_type`，同 {@link findEventSpec}）
 * 查；查不到用 {@link DEFAULT_WAKE_MODE}。查不到是正常情况而非错误：收件侧不按目录校验，
 * 所以返回值一定是可取用的方式，调用方不需要处理「没有方式」这个分支。
 * @param envelope - 本角色收到的一封事件。
 * @returns 该条目声明的唤醒方式，目录外为 {@link DEFAULT_WAKE_MODE}。
 */
export function wakeModeFor(envelope: RoleBusEnvelope): WakeMode {
  return findEventSpec(envelope.source, envelope.event_type)?.wakeMode ?? DEFAULT_WAKE_MODE
}
