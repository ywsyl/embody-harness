/**
 * @embody/dsh-rolebus —— 角色间文件传输事件总线（里程碑 1：真总线）。
 *
 * 本项目里各角色（interactor / orchestrator / planner / supervisor）是各自独立的 dsh
 * profile/进程；它们之间的结构化事件经本插件在共享 spool（缺省 $DSH_HOME/rolebus）
 * 上传送。事件的唯一事实源是 `src/event-catalog.ts`：每个角色 profile 引入同一份
 * bundle，用各自 profile 覆盖层给出 role / peers；插件据此：
 *   - 收：轮询本角色收件箱，校验通过且 target==本角色的事件先归档再去重投递，
 *     并在本进程内 `ctx.emit('rolebus/event', envelope)` 交给同角色插件订阅；
 *     进程重启先补收存量（at-least-once，事件幂等靠 event_id 归档）。
 *   - 待机（`resident: true`）：进程启动时不跑模型，只把本角色的 agent 建出来停在 idle；
 *     上面每投递一封事件就按**该条目的 `wakeMode`** 叫醒它（`steer` 交给最近的步骤边界 /
 *     `followup` 独占下一轮 / `inject` 只投不唤醒）。启动形态由 profile/叠加层决定，见 resident.ts。
 *   - 发：按下表为本角色注册**具名**工具 `rolebus_send_<event_type>`，每条事件一个；
 *     source 由条目强制为本角色，target 由条目强制且必须是 peers 之一，信封按目录
 *     参数校验后落目标收件箱。目录外的事件没有工具可发——目录是唯一发送通道。
 *
 * 依赖解析：引擎包未发布、本 checkout 未构建 lib，因此本包**不声明引擎依赖**
 * （否则 profile `pnpm add` 会在 DSH_HOME 内解析失败）；运行时在 tsx 源码启动的
 * dsh（fork 根的开发形态，`@deepseek-ai/*` 经 tsconfig paths 指向 src）内解析。
 *
 * 遵循 dsh 引擎函数插件约定：命名导出 name / Config / apply，无默认导出（默认导出
 * 会被 Loader 丢弃命名导出元信息）。Config 用 schemastery 校验并落默认值。
 * @module @embody/dsh-rolebus
 */

import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import { defineTool, type ToolDefinition } from '@deepseek-ai/dsh-tools'
import { eventsForRole, isKnownRole, type RoleBusEventSpec } from './event-catalog.ts'
import { installResident } from './resident.ts'
import { defaultSpoolRoot, deliver, post } from './transport.ts'
import { EVENT_TYPE_PATTERN, HEADER_FIELDS, REQUIRED_HEADER_FIELDS, SEVERITIES, UNSET_ROLE } from './types.ts'
import type { Severity } from './types.ts'
// 让 cordis `Events` 带上 `rolebus/event`（src/types.ts 内的声明合并入编译程序）。
export type * from './types.ts'

export const name = 'rolebus'

/** 注册发件工具依赖 `tools` 服务；声明注入使 apply 延迟到该服务就绪（同 @deepseek-ai/dsh-mcp-client）。 */
export const inject = ['tools']

/**
 * 本角色对 rolebus 的部署选择。role / peers 由每个角色 profile 覆盖层赋予
 * （每个已建置角色一份：Interactor/cordis.patch.yml、Orchestrator/cordis.patch.yml、
 * Planner/cordis.patch.yml）；bundle 默认是 role=unset / peers=[] 的中性值，未覆盖时
 * 插件只收不发。
 */
export interface Config {
  /** 本角色身份（目录条目的 source，如 interactor / orchestrator；只收角色如 planner 无 source 条目）。 */
  role: string
  /** 允许投递的目标角色集合（装配时校验每条事件的 target ∈ peers）。 */
  peers: string[]
  /** 传输实现；当前仅 `file`（spool），其余值在 apply 处 fail loud。 */
  transport: 'file'
  /**
   * 常驻待机：本进程启动时把本角色的 agent 建出来放着（不跑模型），收到本角色事件时
   * 唤醒它。缺省 false —— 常驻是启动形态而非角色身份，headless 一次性试跑不能被它污染。
   */
  resident: boolean
  /** 传输层可选调参：root 覆盖共享 spool 根；pollIntervalMs 覆盖收件轮询间隔。 */
  transportConfig?: {
    /** 共享 spool 根；缺省 = $DSH_HOME/rolebus（隔离验证时随 DSH_HOME 落临时目录）。 */
    root?: string
    /** 收件轮询间隔毫秒；缺省 200。 */
    pollIntervalMs?: number
  }
}

/** Schemastery 配置：required + 默认值在此声明，应用层不再做隐藏默认。 */
export const Config: z<Config> = z.object({
  role: z.string().required(),
  peers: z.array(z.string()).default([]),
  transport: z.union(['file'] as const).default('file'),
  resident: z.boolean().default(false),
  transportConfig: z.object({
    root: z.string(),
    pollIntervalMs: z.number(),
  }),
})

const DEFAULT_POLL_INTERVAL_MS = 200

/** 发件地址：共享 spool 根 + 本角色身份/路由（apply 与测试各自持有）。 */
export interface SendAddress {
  root: string
  role: string
  peers: string[]
}

/** 参数名中属于信封头（而非 payload）的集合；见 {@link HEADER_FIELDS}。 */
const HEADER_FIELD_SET: ReadonlySet<string> = new Set(HEADER_FIELDS)

/** 由条目强制、模型不得提供的参数名；见 {@link REQUIRED_HEADER_FIELDS}。 */
const FORCED_ARG_KEYS: ReadonlySet<string> = new Set(REQUIRED_HEADER_FIELDS)

/** 信封 severity 取值判定（取值域见 types.ts 的 `SEVERITIES`）。 */
function isSeverity(value: string): value is Severity {
  return (SEVERITIES as readonly string[]).includes(value)
}

/**
 * 取一个字符串型信封头参数。
 * 隐式参数根是开放的，未在条目里声明的键也会到达这里，因此非字符串值 fail loud，
 * 而不是悄悄丢弃或强转成一种模型没要过的值。
 * @param args - 模型参数。
 * @param key - 头字段名。
 * @returns 该字段的字符串值，未提供时 `undefined`。
 */
function headerText(args: Record<string, unknown>, key: string): string | undefined {
  const value = args[key]
  if (value === undefined) return undefined
  if (typeof value !== 'string') {
    throw new Error(`rolebus: 参数 ${JSON.stringify(key)} 必须是字符串，收到 ${typeof value}`)
  }
  return value
}

/**
 * 构造一条事件对应的具名发件工具 `rolebus_send_<event_type>`。
 *
 * 身份与路由由条目强制（`source` = 本进程角色、`target` = 条目 target、
 * `event_type` = 条目 event_type），模型无法改；模型参数按 {@link HEADER_FIELDS}
 * 分区：同名键进信封头，其余进 `payload`。条目决定哪些头字段暴露给模型——参数表里
 * 没写的头字段模型就填不了。
 *
 * 抽成导出以便在缺工具服务/免 key 的测试里直接执行工具 body，而不必起整套引擎。
 * @param address - 本角色发件身份与共享 spool 根。
 * @param spec - 本角色发出的一条事件条目。
 * @returns 可由 `ctx.tools.register` 挂载的工具定义。
 */
export function createEventTool(address: SendAddress, spec: RoleBusEventSpec): ToolDefinition {
  return defineTool({
    name: `rolebus_send_${spec.event_type}`,
    description: `${spec.description} `
      + `Sends ${JSON.stringify(spec.event_type)} from role ${JSON.stringify(address.role)} `
      + `to role ${JSON.stringify(spec.target)}. `
      + 'Delivery is at-least-once and deduplicated by `event_id` on the receiver; a repeated send of the '
      + 'same `event_id` fails loud rather than overwriting.',
    parameters: spec.parameters,
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        // 值模式 DSL 里 required 是逐属性注解，不加对象级 required 数组。
        properties: {
          ok: { type: 'boolean', required: true },
          event_id: { type: 'string', required: true },
          event_type: { type: 'string', required: true },
          target: { type: 'string', required: true },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: `Posted ${value.event_type} → ${value.target} as ${value.event_id}.`,
      }],
    },
    async execute(args) {
      // 条目表把 parameters 声明为 ParameterSchemaSpec，defineTool 由它推断出的实参
      // 类型是索引签名而非具名键；这里按头字段词表收窄回具名读法。
      const raw = args as Record<string, unknown>
      const payload: Record<string, unknown> = {}
      for (const [key, value] of Object.entries(raw)) {
        if (value === undefined || HEADER_FIELD_SET.has(key)) continue
        // 条目不会声明这几个名字（见 assertSendableSpec），但隐式参数根是开放的，
        // 模型可能凭空给一个。身份与路由不由模型决定，报错而不是塞进 payload。
        if (FORCED_ARG_KEYS.has(key)) {
          throw new Error(`rolebus: 参数 ${JSON.stringify(key)} 由事件条目强制，不接受模型提供`)
        }
        payload[key] = value
      }
      const severity = headerText(raw, 'severity')
      if (severity !== undefined && !isSeverity(severity)) {
        throw new Error(`rolebus: 参数 "severity" 必须是 ${SEVERITIES.join('/')} 之一，收到 ${JSON.stringify(severity)}`)
      }
      const posted = await post({
        root: address.root,
        role: address.role,
        peers: address.peers,
        target: spec.target,
        event_type: spec.event_type,
        task_id: headerText(raw, 'task_id'),
        program_id: headerText(raw, 'program_id'),
        correlation_id: headerText(raw, 'correlation_id'),
        severity,
        ...(Object.keys(payload).length > 0 ? { payload } : {}),
      })
      return { ok: true, event_id: posted.event_id, event_type: posted.event_type, target: posted.target }
    },
    presentCall: args => ({ card: 'generic', title: `Post ${spec.event_type} → ${spec.target}`, kind: 'other', rawInput: args }),
  })
}

/**
 * 注册前的 fail loud 校验：条目不合法就在装配时抛，而不是等模型调用时才失败。
 * @param address - 本角色发件身份与共享 spool 根。
 * @param spec - 待校验的事件条目。
 * @throws event_type 不合 snake_case、target 不是本角色的 peer、或参数名占用了信封的强制头字段。
 */
function assertSendableSpec(address: SendAddress, spec: RoleBusEventSpec): void {
  if (!EVENT_TYPE_PATTERN.test(spec.event_type)) {
    throw new Error(`rolebus: 事件类型 ${JSON.stringify(spec.event_type)} 不是 snake_case`)
  }
  if (spec.source !== address.role) {
    throw new Error(
      `rolebus: 事件 ${JSON.stringify(spec.event_type)} 的 source ${JSON.stringify(spec.source)} `
      + `不等于本进程角色 ${JSON.stringify(address.role)}`,
    )
  }
  if (!address.peers.includes(spec.target)) {
    const peers = address.peers.map(value => JSON.stringify(value)).join(', ') || '无'
    throw new Error(
      `rolebus: 事件 ${JSON.stringify(spec.event_type)} 的 target ${JSON.stringify(spec.target)} `
      + `不在 ${JSON.stringify(address.role)} 的 peers（${peers}）内 —— 该工具永远发不出去`,
    )
  }
  for (const field of REQUIRED_HEADER_FIELDS) {
    if (field in spec.parameters) {
      throw new Error(
        `rolebus: 事件 ${JSON.stringify(spec.event_type)} 的参数名 ${JSON.stringify(field)} `
        + '与信封的强制头字段冲突（source/target/event_type 由条目强制，不可作为参数）',
      )
    }
  }
}

/**
 * 为一个角色装配全部发件工具。
 * @param address - 本角色发件身份与共享 spool 根。
 * @param specs - 本角色发出的事件条目（通常来自 `eventsForRole`）。
 * @returns 可由 `ctx.tools.register` 逐个挂载的工具定义，顺序同 `specs`。
 * @throws 任一条目不合法（见 {@link assertSendableSpec}）。
 */
export function createEventTools(address: SendAddress, specs: readonly RoleBusEventSpec[]): ToolDefinition[] {
  for (const spec of specs) assertSendableSpec(address, spec)
  return specs.map(spec => createEventTool(address, spec))
}

/**
 * 挂载 rolebus：启动本角色收件轮询（含一次性补收存量），并在工具服务存在且本角色
 * 身份可用时注册本角色的具名发件工具。
 * @param ctx - registrant context。
 * @param config - 校验并落默认值后的 rolebus 配置。
 * @throws transport 不是 `file` 时（唯一已实现传输）；role 有 peers 却在事件目录里既不是任何
 * 条目的 source 也不是 target（角色名拼错）。
 */
export function apply(ctx: Context, config: Config): void {
  if (config.transport !== 'file') {
    throw new Error(`rolebus: only transport 'file' is implemented, got ${JSON.stringify(config.transport)}`)
  }
  const role = config.role
  const peers = config.peers
  const root = config.transportConfig?.root ?? defaultSpoolRoot()
  const intervalMs = config.transportConfig?.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS

  // 装配即校验，早于收件 effect：角色名拼错时不留任何副作用。unset / 无 peer 是
  // bundle 中性默认，不是错误——那种 profile 只收不发。
  //
  // 判定的是「目录里认不认识这个名字」，不是「有没有 source 为自己的条目」：只收角色
  // （planner）没有任何发件条目却是别人事件的 target，是合法形态；拼错的名字在目录里
  // 哪个位置都不出现，照样抛。
  const specs = role === UNSET_ROLE || peers.length === 0 ? [] : eventsForRole(role)
  if (role !== UNSET_ROLE && peers.length > 0 && !isKnownRole(role)) {
    throw new Error(
      `rolebus: 角色 ${JSON.stringify(role)} 在 src/event-catalog.ts 里既不是任何事件的 source `
      + '也不是 target —— 角色名是否拼错？',
    )
  }

  /** 一轮收件：投递合格事件到本进程 `rolebus/event`；IO 偶发错误仅记录，不中断轮询。 */
  const deliverAndEmit = async (): Promise<void> => {
    try {
      const result = await deliver(root, role)
      for (const envelope of result.delivered) ctx.emit('rolebus/event', envelope)
    } catch (error) {
      // spool IO 失败（如目录被外部清空）不该让常驻轮询退出；失败细节留 console。
      console.error('[rolebus] inbox deliver failed:', error)
    }
  }

  // ctx.effect：随插件生命周期启停；先补收一次存量（角色离线期间积压的事件），再按
  // 间隔轮询，保证“进程重启不丢事件”的 at-least-once 语义。
  ctx.effect(() => {
    void deliverAndEmit()
    const timer = setInterval(() => { void deliverAndEmit() }, intervalMs)
    return () => clearInterval(timer)
  })

  // 常驻待机：把本角色的 agent 建出来待命，事件到达时唤醒（见 resident.ts）。
  //
  // 这里用 ctx.inject 而不是把 'agents' 加进模块级 inject：inject 是静态声明，role=unset
  // 的收件-only profile（bundle 层的中性默认）等不到 agents 就会连收件轮询都不启动。
  if (config.resident) {
    if (role === UNSET_ROLE || peers.length === 0) {
      throw new Error(
        `rolebus: resident=true 需要角色身份（当前 role=${JSON.stringify(role)}, peers=${peers.length}）`,
      )
    }
    ctx.inject(['agents', 'agentDefaultModel', 'sessionPersistence'], residentCtx => {
      installResident(residentCtx, role)
    })
  }

  // 工具只在本角色身份可用（已被 profile 赋予 role 且有 peers）时暴露；unset 或
  // 无 peer 的收件-only profile 不该向模型展示任何发件工具。
  const tools = ctx.get('tools')
  if (tools === undefined || role === UNSET_ROLE || peers.length === 0) return
  const definitions = createEventTools({ root, role, peers }, specs)

  // 注册即 effect：整批全有或全无——部分失败时摘掉已注册的再抛，模型看到的要么是
  // 本角色完整的发件面，要么一个都没有（引擎 mcp-client 的批量注册同一写法）。
  ctx.effect(() => {
    const disposers: Array<() => void> = []
    try {
      for (const definition of definitions) disposers.push(tools.register(definition))
    } catch (error) {
      for (const dispose of disposers) dispose()
      throw error
    }
    return () => {
      for (const dispose of disposers) dispose()
    }
  })
}
