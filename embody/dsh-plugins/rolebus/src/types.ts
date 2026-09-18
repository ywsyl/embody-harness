/**
 * rolebus 的领域类型：事件信封与进程内投递事件。事件目录（谁→谁、何时发、带哪些
 * 字段）的唯一事实源是 `./event-catalog.ts`；本文件只承载传输层需要的“统一事件头”
 * 结构，不编码目录本身。
 * @module @embody/dsh-rolebus/types
 */

// Type-only: keeps the '@deepseek-ai/cordis' module in the program so the
// declaration merge below augments its `Events` map (engine idiom: consumers
// must `import '@embody/dsh-rolebus'` types to see `rolebus/event`).
import type {} from '@deepseek-ai/cordis'

/** 事件名称一律 snake_case（`EVENT_TYPE_PATTERN`）；保持为普通 string，语义由目录条目约束。 */
export type RoleName = string

/** severity 取值（信封头字段：仅面向用户的事件有意义）。 */
export const SEVERITIES = ['info', 'warning', 'blocking', 'safety'] as const
/** 面向用户事件的严重度；非面向用户事件不带此字段。 */
export type Severity = (typeof SEVERITIES)[number]

/**
 * 统一事件头。`event_id`/`timestamp` 可由 rolebus 自动补全，但
 * `event_type`/`source`/`target` 由调用方表达；`source` 必须是本角色身份
 * （send 时强制为本角色），`target` 必须是本角色 `peers` 之一（send 时校验）。
 */
export interface RoleBusEnvelope {
  /** 发送方生成或 rolebus 补全的全局唯一事件 id（去重键）。 */
  event_id: string
  /** 目录条目（`./event-catalog.ts`）的事件类型，如 `task_content_update`。 */
  event_type: string
  /** 发送角色身份（= 发送方 profile 的 rolebus.role）。 */
  source: RoleName
  /** 目标角色身份（= 收件箱角色）。 */
  target: RoleName
  /** Unix 秒级时间戳（发件时写入）。 */
  timestamp: number
  /** 信封头字段：有任务上下文时填写。 */
  task_id?: string
  /** 信封头字段：有 Program 上下文时填写。 */
  program_id?: string
  /** 信封头字段：请求-应答关联 id。 */
  correlation_id?: string | null
  /** 信封头字段：仅面向用户事件携带。 */
  severity?: Severity
  /** 事件负载（领域字段由目录条目的 parameters 定义，rolebus 透明透传）。 */
  payload?: Record<string, unknown>
}

/**
 * 进程内投递事件：rolebus 从本角色收件箱取到一封合法、目标为本角色的事件后，
 * 在本角色进程内 `ctx.emit('rolebus/event', envelope)`，供同角色其他插件订阅。
 * 事件头完整保留，投递不丢字段。
 */
declare module '@deepseek-ai/cordis' {
  interface Events {
    'rolebus/event'(envelope: RoleBusEnvelope): void
  }
}

/** 发件与投递两侧共用校验的强制头字段。 */
export const REQUIRED_HEADER_FIELDS = ['event_type', 'source', 'target'] as const

/**
 * 信封的**可选**头字段：工具参数中与之同名的键写进事件头，其余进 `payload`
 * （分区实现见 `index.ts` 的 `createEventTool`）。名称镜像 {@link RoleBusEnvelope}
 * 的可选字段，不是预留。
 */
export const HEADER_FIELDS = ['task_id', 'program_id', 'correlation_id', 'severity'] as const

/** 事件类型必须是 snake_case：小写字母开头，仅小写字母/数字/下划线。 */
export const EVENT_TYPE_PATTERN = /^[a-z][a-z0-9_]*$/

/** 角色身份不可能是 `unset`（bundle 中性默认）——未赋予身份即不可发件。 */
export const UNSET_ROLE = 'unset'
