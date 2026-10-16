/**
 * rolebus 的角色间事件目录 —— 本项目「谁发什么、发给谁、带哪些字段」的唯一事实源。
 *
 * 每条事件声明 `(source, event_type)` 身份、唯一 `target` 和模型可见的参数表。角色
 * profile 的 `rolebus.role` 选定 source，`apply` 据此为本角色注册
 * `rolebus_send_<event_type>` 工具。**目录是发送的唯一通道**：不在这张表里的事件
 * 没有任何工具可以发出去，参数形状由引擎的工具参数校验把关。
 *
 * 参数分两段落进信封：与信封可选头字段同名的键（`task_id` / `program_id` /
 * `correlation_id` / `severity`）写进事件头，其余全部进 `payload`；条目标了
 * `toolOnlyParameters` 的键则由发件工具自己消费、两处都不进。这个分区在
 * `index.ts` 的 `createEventTool` 里实现，是本文件与信封之间唯一的约定点。
 *
 * 每条还声明**送达时的优先级**（`priority`）：它是信封头字段，随事件过 wire，收件侧的常驻
 * 角色进程据此决定怎么叫醒它的 agent（`PRIORITY_WAKE_MODES` 是优先级→唤醒方式的唯一映射，
 * 分派实现见 `resident.ts` 的 `wakeResident`）。**目录里定义的事件，信封上一定带这个字段**：
 * 发件工具（`index.ts` 的 `createEventTool`）在模型没表态时就写条目声明的值，所以条目里的值
 * 是兜底而非常态缺省。有发件工具的事件还把可选范围声明在 `parameters.priority.enum` 里，由
 * 发件模型逐封选择；Python 侧的事件副本（`embody/plugin_tools/rolebus.py` 的 `CLOUD_EVENTS`）
 * 也逐条带 `priority`，与本表逐字一致。
 * 唯一可能不带它的是 `scripts/manual/rolebus-emit.sh` 手工投的临时事件——那个脚本**不查目录**，
 * 收件侧按 {@link priorityFor} 的回落处理。
 * 目录里查不到的事件（收件侧不按目录校验，手工 emit 的临时事件就是这种）用
 * {@link DEFAULT_PRIORITY}。
 *
 * 事件集覆盖任务清单通知、交互请求/回填、执行平面观察与云端下发四类。待做任务清单是
 * **一份共享文件**（`embody/runtime_files/tasks/`，见 `plugin_tools/task_board.py`）：它的
 * 唯一写者是 `embody/plugin_tools/task_manager.py`，两个角色手里都没有写工具——改动一律装进
 * 事件（interactor 的 `task_update_int` / orchestrator 的 `task_update_orc`）的 `items` 参数，
 * 由发件工具在**投递之前**同步调 task_manager 落盘（见
 * {@link RoleBusEventSpec.writesTaskBoard}），所以收件方被唤醒时读到的清单一定已含这次改动。
 * 读：interactor 按需调 `task_list` 工具 —— 不带号拿整份，带号只看那一条（清单里没有就到同目录
 * 按文件名翻已终结任务的存档），orchestrator 由 `dsh-plugins/task-context` 每轮
 * 收到**清单全文**的注入，都不占事件、也不进每轮上下文。`items` 照旧进信封留
 * 归档审计，但**不渲染进收件方的唤醒文本**（见 {@link RoleBusEventSpec.hiddenPayload}）。
 * `scripts/manual/rolebus-emit.sh` 手工投的临时事件绕过发件工具，因此不落盘。
 *
 * interactor+orchestrator 与 planner 是**两个并存的版本**，同一套第三方事件各有一份、
 * 各投各的角色（`planner_` 前缀即 planner 版）：目录主键是 `(source, event_type)`、
 * `target` 又是单值，一个名字不可能同时投两个目标角色，所以只能另起名。两个版本不同时
 * 启动，旧版的那 6 条与 planner 的 2 条互不影响（planner 那组当前整块注释着，放开后与旧版
 * 逐条对应）。planner 只收不发，因此它的 2 条 `source` 都是第三方（supervisor / cloud），
 * 目录里没有 `source: planner` 的条目。
 *
 * `cloud` 是**外部发送方**：云端不属于本项目的 dsh 进程，没有 profile 承载它，因此不会
 * 有 `rolebus_send_cloud_*` 工具。它的发送方是 `embody/plugin_tools/rolebus.py`（直写
 * spool），端侧由 `embody/Communicator/receiver.py` 先按 `CLOUD_ACTION_MAP` 把云端
 * `body.action` 归一成端侧动作，再交给它投；本表是这几条事件的形状事实源，两处必须一致。
 *
 * 端侧动作只有 `update` / `stop` 两个，两者都走 `cloud_update` 这一条事件（差别只在
 * `payload.action`，见该条目的 `parameters.action.enum`）；云端那套 `create / modify /
 * interrupt / answer / info`（+ 旧网关 `cancel` / `resume`）是**云端词**，只活在 receiver
 * 的映射表里，answer / info 端侧不处理。
 * @module @embody/dsh-rolebus/event-catalog
 */

import type { ParameterSchemaSpec } from '@deepseek-ai/dsh-tools'
import { PRIORITIES, type Priority, type RoleBusEnvelope, type RoleName } from './types.ts'

/**
 * 一条角色间事件：身份、路由、优先级与模型可见的参数表。
 * @property event_type - snake_case 事件类型；与 `source` 共同构成目录主键。
 * @property source - 发出该事件的角色身份（= 注册该工具的 profile 的 `rolebus.role`）。
 * @property target - 唯一接收角色；必须是发送方 profile 的 `peers` 之一。
 * @property priority - 该事件**固定**的优先级，也是发件方没表态时的回落值（唤醒方式见
 * {@link PRIORITY_WAKE_MODES}）。带发件工具的条目在 `parameters.priority.enum` 里声明
 * 模型可选的范围，该 enum 必须含这个值；不带工具的条目（如 cloud 两条）模型无从表态，
 * 这个值就是它唯一的优先级。
 * @property description - 模型可见的工具描述：这条事件是干什么的、什么时候发。
 * @property parameters - 平铺的工具参数；与信封可选头字段同名的进事件头，其余进 payload。
 * @property writesTaskBoard - 投递前先把 `items` 参数交给待做任务清单的唯一写者
 * （`embody/plugin_tools/task_manager.py`）落盘，拿它的回执当信封头的 `task_id`；落盘失败则
 * 整次调用失败、事件不发（清单改动不幂等，重试会多记一次改动）。标了它的条目必须有 `items` 参数。
 * @property skipPostParameter - 上面落盘之后仍能拦下投递的工具本地**布尔**参数名：它取 `false`
 * 时本工具只落盘、不发事件（收件方下一轮自己会看到清单变了）。`task_update_int` 的
 * `notify_orchestrator` 与 `task_update_orc` 的 `notify_interactor` 都是它，各叫各的收件方。
 * 只有落过盘的条目配它，名字必须同时出现在 `parameters`（类型 boolean）与 `toolOnlyParameters` 里。
 * @property toolOnlyParameters - 由发件工具自己消费、**两处都不进**（既不进信封头也不进
 * `payload`）的参数名，如 `task_update_int` 的 `notify_orchestrator`。名字必须真的在 `parameters` 里。
 * @property hiddenPayload - 进了信封 `payload`（留归档审计）但**不渲染进收件方唤醒文本**的
 * payload 键，如两条任务事件的 `items`（收件方只该看到人话说明）。名字必须真的在 `parameters`
 * 里且不是信封头字段。
 */
export interface RoleBusEventSpec {
  readonly event_type: string
  readonly source: RoleName
  readonly target: RoleName
  readonly priority: Priority
  readonly description: string
  readonly parameters: ParameterSchemaSpec
  readonly writesTaskBoard?: boolean
  readonly skipPostParameter?: string
  readonly toolOnlyParameters?: readonly string[]
  readonly hiddenPayload?: readonly string[]
}

/**
 * 端侧云端事件的全部动作，只有两个：`update` 是任务内容有交代（云端下发 / 改写），
 * `stop` 是停下该任务；两条都由 `cloud_update` 一条事件承载，它们只区别 `payload.action`
 * 的取值。云端那套 `create / modify / interrupt / answer / info`（+ 旧网关 `cancel` /
 * `resume`）是**云端词**，由 `embody/Communicator/receiver.py` 的 `CLOUD_ACTION_MAP` 归一成
 * 这两个词之后才投到这里。
 */
const CLOUD_ACTIONS = ['update', 'stop'] as const

/**
 * 事件送达本角色时的唤醒方式，由该封事件的优先级经 {@link PRIORITY_WAKE_MODES} 推出；
 * 取值逐字对应引擎 `Agent` 上的三个同名方法
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
 * 优先级 → 唤醒方式，全项目**唯一**的映射表（发件方只表态紧迫度，怎么唤醒由收件侧这张表定）。
 * 模型的可见说明逐条写在 `parameters.priority` 的描述里（见 {@link PRIORITY_HELP}）。
 */
export const PRIORITY_WAKE_MODES: Readonly<Record<Priority, WakeMode>> = {
  P1: 'steer',
  P2: 'followup',
  P3: 'inject',
}

/**
 * 目录外事件的优先级。收件侧不按目录校验（见 {@link findEventSpec}），手工 emit 的临时
 * 事件、以及本进程目录副本较旧时别的角色新加的事件都落在这条路径上；已投递的事件不能丢，
 * 所以给定值而不是静默丢弃或抛错。目录内的事件不走这条路——它们的发件方一定把条目声明的
 * 值写进信封（见 {@link ROLEBUS_EVENTS} 的 `priority`）。取值经 {@link PRIORITY_WAKE_MODES}
 * 映射成 `steer`，与拆出优先级之前那条默认唤醒方式一致。
 */
export const DEFAULT_PRIORITY: Priority = 'P1'

/**
 * 三级优先级的模型可见说明，逐条按**收件方会发生什么**写；带 priority 参数的条目共用这段
 * 文字，各自在末尾补一句自己的可选范围（见 {@link ROLEBUS_EVENTS}）。
 */
const PRIORITY_HELP = [
  '这封事件的优先级，决定收件角色怎么被叫醒：',
  'P1 最急——它正忙就打断当前这一步，这封立刻进它当前这一轮；它空闲则立刻起一轮。',
  'P2 中等优先级——不打断，等它把当前这一轮走完再单独起一轮处理这封；它空闲则立刻起一轮。',
  'P3 最低——只投进它的收件箱、不叫醒它，等它下次本来就要跑的时候顺带看到；它一直空闲就没人取走，别用它送要紧的事。',
  '省略则用这条事件自带的优先级。',
].join('\n')

/** 全量事件目录。按 source 分组排列（interactor / orchestrator / supervisor / cloud），
 * 末尾是 planner 版的 2 条第三方事件（source 仍是 supervisor / cloud，见文件头注释）。 */
export const ROLEBUS_EVENTS: readonly RoleBusEventSpec[] = [
  {
    event_type: 'task_update_int',
    source: 'interactor',
    target: 'orchestrator',
    priority: 'P1',
    description: '给 orchestrator 下达指令：开始或继续执行某任务；或某任务内容被修改',
    writesTaskBoard: true,
    skipPostParameter: 'notify_orchestrator',
    toolOnlyParameters: ['notify_orchestrator'],
    hiddenPayload: ['items'],
    parameters: {
      task_id: {
        type: 'string',
        description: `任务的编号，**从已有数据里抄或省略该参数，不要自己编**`,
      },
      description: {
        type: 'string',
        required: true,
        description: '对该任务的通知/指令，如对该任务的修改或开始/继续/暂停该任务。收件方只看得到这一句（items 不进它的唤醒文本）。'
      },
      items: {
        type: 'array',
        required: true,
        description: `需要写入任务清单的字段和内容，可以多条。点分字段路径。你可写的字段：
- 新建任务时的所有字段。
- owner=interactor的任务的 description / status / owner 字段：任务级字段（status 只有任务级这一处，执行记录没有状态）。
- owner=interactor的任务的 step 字段：新建一条执行记录的整段内容，value 给一个对象，如 {"description": "走到客厅", "tool": "waypoint_nav"}。
step1.result / step1.description：改某条已有执行记录的字段（编号看 task_list）。
无需你修改、模块自己记账的字段：create_time、last_update_time、stepN.attempts、stepN.update_time（时间戳传了也不作数，模块一律盖成当前时刻）。
你无权修改的字段：
- 已有任务的task_id和owner字段
- owner=orchestrator的任务：只有 status 一个字段、且只能改成 executing（叫起它、让它接着做——这是唯一一个你能写进它的字段与取值）。其余字段、以及 status 的其它取值都写不了：要它暂停 / 取消，在 description 里说清，状态由它自己改`,
        items: {
          type: 'object',
          additionalProperties: false,
          properties: {
            field: {
              type: 'string',
              required: true,
              description: '点分字段路径，如 description / status / owner / step1.result；写裸 step 会自动编号成下一条执行记录。',
            },
            value: {
              type: 'json',
              required: true,
              description: '该字段要写入的值，任意 JSON。',
            },
          },
        },
      },
      notify_orchestrator: {
        type: 'boolean',
        description: '是否把这条事件投给 orchestrator。省略即投；false = 只把这些改动落进清单、**不叫醒它**（它下一轮自己会看到清单变了）——改自己名下的任务用 false。这个参数只给发件工具看，不进事件。',
      },
      priority: {
        type: 'string',
        enum: ['P1'],
        description: `${PRIORITY_HELP}\n这条事件固定用 P1。`,
      },
    },
  },
  {
    event_type: 'interaction_result',
    source: 'interactor',
    target: 'orchestrator',
    priority: 'P1',
    description: '回答一个早期的 orchestrator 的提问',
    parameters: {
      task_id: {
        type: 'string',
        required: true,
        description: '涉及的任务编号。'
      },
      answer: {
        type: 'string',
        required: true,
        description: '总结编排器的提问，并明确回复的答案。',
      },
      priority: {
        type: 'string',
        enum: ['P1'],
        description: `${PRIORITY_HELP}\n这条事件固定用 P1。`,
      },
    },
  },
  {
    event_type: 'interaction_request',
    source: 'orchestrator',
    target: 'interactor',
    priority: 'P1',
    description: '请求用户补充/回答执行必须但尚未明确、或无法获取的任务信息，通常是编排器在执行任务前发现了不明确信息，或执行任务时需要用户指示下一步怎么做。只能用于需要回答的信息提问，如不需回答、只需通知请使用 task_update_orc。',
    parameters: {
      task_id: {
        type: 'string',
        required: true,
        description: '涉及的任务编号。',
      },
      content: {
        type: 'string',
        required: true,
        description: '说明请求内容。'
      },
      priority: {
        type: 'string',
        enum: ['P1'],
        description: `${PRIORITY_HELP}\n这条事件固定用 P1。`,
      },
    },
  },
  {
    event_type: 'task_update_orc',
    source: 'orchestrator',
    target: 'interactor',
    priority: 'P2',
    description: '与交互器同步任务的执行情况：更新任务的阶段性进展，或修改任务的状态。',
    writesTaskBoard: true,
    skipPostParameter: 'notify_interactor',
    toolOnlyParameters: ['notify_interactor'],
    hiddenPayload: ['items'],
    parameters: {
      task_id: {
        type: 'string',
        required: true,
        description: '任务的编号，从清单或事件头原样抄，不得编造；**必须是清单里已有的那一条**——编排器只能改任务、不能新建，号在清单里找不到就写不进去（整次调用报错）。',
      },
      description: {
        type: 'string',
        required: true,
        description: '用一句话说清这次的改动情况',
      },
      items: {
        type: 'array',
        required: true,
        description: `需要写入任务清单的字段和内容，可以多条。点分字段路径。你可写的字段：
- owner=orchestrator的任务的 description / status 字段：任务级字段（status 只有任务级这一处，执行记录没有状态）。
- owner=orchestrator的任务的 step 字段：新建一条执行记录的整段内容，value 给一个对象，如 {"description": "走到客厅", "tool": "waypoint_nav"}。
step1.result / step1.description：改某条已有执行记录的字段（编号是键名里的序号，如清单里那条任务的 step1）。
无需你修改的字段：task_id、owner、create_time、last_update_time、stepN.attempts、stepN.update_time（时间戳传了也不作数，模块一律盖成当前时刻）。
你无权修改的字段：交互器任务（owner=interactor）的所有字段。`,
        items: {
          type: 'object',
          additionalProperties: false,
          properties: {
            field: {
              type: 'string',
              required: true,
              description: '点分字段路径，如 description / status / step1.result；写裸 step 会自动编号成下一条执行记录。owner / task_id / create_time 不可写。',
            },
            value: {
              type: 'json',
              required: true,
              description: '该字段要写入的值，任意 JSON。',
            },
          },
        },
      },
      notify_interactor: {
        type: 'boolean',
        description: '是否把这条事件投给 interactor。省略即投；false = 只把这些改动落进清单、**不叫醒它**（它下一轮自己会看到清单变了）——不值得让用户听到的变动（如只补了一条执行记录、改了任务描述）用 false。这个参数只给发件工具看，不进事件。',
      },
      priority: {
        type: 'string',
        enum: ['P2'],
        description: `${PRIORITY_HELP}\n这条事件固定用 P2。`,
      },
    },
  },
  {
    event_type: 'observe_event',
    source: 'supervisor',
    target: 'interactor',
    priority: 'P1',
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
      priority: {
        type: 'string',
        enum: [...PRIORITIES],
        description: `${PRIORITY_HELP}\n按观测到的严重程度选：要它立刻处理的用 P1，一般的用 P2，纯记录用 P3。`,
      },
    },
  },
  
  
  
  // cloud 由 Python 侧直写 spool（见文件头注释），不注册发件工具。
  {
    event_type: 'cloud_update',
    source: 'cloud',
    target: 'interactor',
    priority: 'P1',
    description: '通知 interactor ：云端下发或改写了某个智能体任务，或要求停下某个任务（看 `action`）。',
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
        description: '端侧动作：update（任务下发或改写）/ stop（停下该任务）。',
      },
      description: {
        type: 'string',
        required: true,
        description: '云端下发的任务内容（`action=stop` 时是停下它的理由），以自然语言描述。',
      },
    },
  },

  // // planner 版：与上面 2 条第三方事件一一对应，只换 target 与名字（见文件头注释）。
  // // planner 只收不发，这批条目的 source 仍是第三方，故不注册任何发件工具。
  // {
  //   event_type: 'planner_observe_event',
  //   source: 'supervisor',
  //   target: 'planner',
  //   priority: 'P1',
  //   description: '报告一个观察到的事件，通常是本体或环境的状态改变、异常或警告。',
  //   parameters: {
  //     component: {
  //       type: 'string',
  //       required: true,
  //       description: '观察到该事件的能力或子系统，例如 camera、navigation、body。',
  //     },
  //     description: {
  //       type: 'string',
  //       required: true,
  //       description: '说明事件的类型、观察的内容等。',
  //     },
  //     priority: {
  //       type: 'string',
  //       enum: [...PRIORITIES],
  //       description: `${PRIORITY_HELP}\n按观测到的严重程度选：要它立刻处理的用 P1，一般的用 P2，纯记录用 P3。`,
  //     },
  //   },
  // },
  // {
  //   event_type: 'planner_cloud_update',
  //   source: 'cloud',
  //   target: 'planner',
  //   priority: 'P1',
  //   description: '通知 planner ：云端下发或改写了某个智能体任务，或要求停下某个任务（看 `action`）。',
  //   parameters: {
  //     task_id: {
  //       type: 'string',
  //       required: true,
  //       description: '云端下发的任务 ID。',
  //     },
  //     action: {
  //       type: 'string',
  //       required: true,
  //       enum: [...CLOUD_ACTIONS],
  //       description: '端侧动作：update（任务下发或改写）/ stop（停下该任务）。',
  //     },
  //     description: {
  //       type: 'string',
  //       required: true,
  //       description: '云端下发的任务内容（`action=stop` 时是停下它的理由），以自然语言描述。',
  //     },
  //   },
  // },
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
 * 取一封信封生效的优先级：信封头上发件方选定的那个优先，缺省（或取值不是 {@link PRIORITIES}
 * 成员）退到目录条目的固定值（按主键 `source` + `event_type` 查，同 {@link findEventSpec}），
 * 目录外退到 {@link DEFAULT_PRIORITY}。
 *
 * 三种退路都是正常路径，一律不抛错：目录外事件与手工 emit 的信封可以不带它、收件侧不按目录
 * 校验、本进程的目录副本可能比发件方旧。信封来自磁盘 JSON（`transport.ts` 的 `validateEnvelope`
 * 只查类型不查成员），所以这里的成员判定是实测而不是多余检查。
 * @param envelope - 本角色收到的一封事件。
 * @returns 该封事件生效的优先级。
 */
export function priorityFor(envelope: RoleBusEnvelope): Priority {
  const chosen = envelope.priority
  if (chosen !== undefined && chosen !== null && (PRIORITIES as readonly string[]).includes(chosen)) {
    return chosen
  }
  return findEventSpec(envelope.source, envelope.event_type)?.priority ?? DEFAULT_PRIORITY
}

/**
 * 取一封信封该用的唤醒方式：优先级经 {@link PRIORITY_WAKE_MODES} 映射。查不到目录条目、
 * 发件方没表态都是正常情况而非错误（见 {@link priorityFor}），所以返回值一定是可取用的方式，
 * 调用方不需要处理「没有方式」这个分支。
 * @param envelope - 本角色收到的一封事件。
 * @returns 该封事件生效的唤醒方式。
 */
export function wakeModeFor(envelope: RoleBusEnvelope): WakeMode {
  return PRIORITY_WAKE_MODES[priorityFor(envelope)]
}
