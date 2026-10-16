/**
 * rolebus 的文件传输层：把角色间事件投到共享 spool 目录下目标角色的收件箱，
 * 收件方原子移入自己的归档（at-least-once + 按 `event_id` 去重，重启后先补收
 * 存量）。本模块无引擎依赖，纯 node:fs —— 可被插件 apply、发件工具
 * 与本仓免 key 测试共用同一套发送/校验代码路径。
 *
 * spool 布局（root 缺省 = $DSH_HOME/rolebus，见 {@link defaultSpoolRoot}）：
 *   <root>/inbox/<target>/<event_id>.json      —— 待投递（发件方写）
 *   <root>/archive/<myRole>/<event_id>.json    —— 已投递记录（收件方移入，去重键）
 *   非法/错路由文件以 `.bad-`/`.misroute-` 前缀留在收件箱，避免反复重读。
 * @module @embody/dsh-rolebus/transport
 */

import { randomUUID } from 'node:crypto'
import { mkdir, readdir, readFile, rename, stat, unlink, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'
import {
  EVENT_TYPE_PATTERN,
  PRIORITIES,
  REQUIRED_HEADER_FIELDS,
  SEVERITIES,
  UNSET_ROLE,
  type Priority,
  type RoleBusEnvelope,
  type RoleName,
  type Severity,
} from './types.ts'

const INBOX = 'inbox'
const ARCHIVE = 'archive'
/** 归档文件名即 event_id，仅允许安全字符，杜绝路径穿越。 */
const EVENT_ID_PATTERN = /^[A-Za-z0-9_-]+$/
/** 发件时先写临时文件再 rename，保证收件箱里只会出现完整 JSON。 */
const PENDING_SUFFIX = '.pending'

/**
 * 共享 spool 根目录：缺省落在本机 dsh home（隔离验证时 DSH_HOME 指向临时目录，
 * 各角色进程自然共享同一 spool）。
 * @returns spool 根目录绝对路径。
 */
export function defaultSpoolRoot(): string {
  return join(resolveDshHome(), 'rolebus')
}

/** 目标角色 <role> 的收件箱目录。 */
function inboxDir(root: string, role: RoleName): string {
  return join(root, INBOX, role)
}

/** 本角色 <role> 已投递事件的归档目录（event_id 去重键所在）。 */
function archiveDir(root: string, role: RoleName): string {
  return join(root, ARCHIVE, role)
}

/** 发件参数：身份与路由校验所需字段齐备，其余为信封头字段的可选项。 */
export interface PostOptions {
  /** 共享 spool 根（由 config.transportConfig.root 或 {@link defaultSpoolRoot} 给出）。 */
  root: string
  /** 发送方角色身份，写进信封 `source`。 */
  role: RoleName
  /** 本角色允许投递的目标集合（config.peers）。 */
  peers: readonly RoleName[]
  /** 目标角色，写进信封 `target`；必须 ∈ peers。 */
  target: RoleName
  /** 目录条目的事件类型（snake_case，见 ../event-catalog.ts）。 */
  event_type: string
  /** 显式 event_id；缺省自动生成 UUID（去重/应答关联时建议显式）。 */
  event_id?: string | undefined
  /** 信封头字段。 */
  task_id?: string | undefined
  /** 信封头字段。 */
  program_id?: string | undefined
  /** 信封头字段：请求-应答关联 id。 */
  correlation_id?: string | null | undefined
  /** 信封头字段：仅面向用户事件。 */
  severity?: Severity | undefined
  /** 信封头字段：本封事件选定的优先级；缺省或 null 即由收件侧按目录条目的固定值决定。 */
  priority?: Priority | null | undefined
  /** 领域负载，rolebus 透明透传。 */
  payload?: Record<string, unknown> | undefined
}

/**
 * 结构校验一封信封（deliver 解析侧与 send 组装侧共用）。只校验统一事件头；
 * 领域语义由 ../event-catalog.ts 与各角色层负责。
 * @param raw - 待校验值。
 * @returns 通过校验的规范化信封。
 * @throws 任一必填头字段缺失、类型不符、或命名/严重度不合规时，带可读原因。
 */
export function validateEnvelope(raw: unknown): RoleBusEnvelope {
  if (raw === null || typeof raw !== 'object') {
    throw new Error(`rolebus: envelope must be an object, got ${raw === null ? 'null' : typeof raw}`)
  }
  const value = raw as Record<string, unknown>
  for (const field of REQUIRED_HEADER_FIELDS) {
    const part = value[field]
    if (typeof part !== 'string' || part.length === 0) {
      throw new Error(`rolebus: envelope missing required string field ${JSON.stringify(field)}`)
    }
  }
  const eventType = value['event_type'] as string
  if (!EVENT_TYPE_PATTERN.test(eventType)) {
    throw new Error(`rolebus: event_type must be snake_case, got ${JSON.stringify(eventType)}`)
  }
  const eventId = value['event_id']
  if (typeof eventId !== 'string' || !EVENT_ID_PATTERN.test(eventId)) {
    throw new Error(`rolebus: event_id must be a non-empty [A-Za-z0-9_-] string, got ${JSON.stringify(eventId)}`)
  }
  const timestamp = value['timestamp']
  if (typeof timestamp !== 'number' || !Number.isFinite(timestamp)) {
    throw new Error(`rolebus: timestamp must be a finite Unix-seconds number, got ${JSON.stringify(timestamp)}`)
  }
  if (value['severity'] !== undefined && !(SEVERITIES as readonly string[]).includes(value['severity'] as string)) {
    throw new Error(`rolebus: severity must be one of ${SEVERITIES.join('/')}, got ${JSON.stringify(value['severity'])}`)
  }
  // priority 只查类型、不查是否 ∈ PRIORITIES（与上面的 severity 不同）：取值不认识时收件侧
  // 回落目录条目的固定值（event-catalog.ts 的 priorityFor），而抛错会把整封事件判成非法
  // （deliver 侧重命名 .bad- 后丢弃）——唤醒强度只是投递提示，不该拿一封已投递的事件去换。
  for (const field of ['task_id', 'program_id', 'correlation_id', 'priority'] as const) {
    if (value[field] !== undefined && typeof value[field] !== 'string' && value[field] !== null) {
      throw new Error(`rolebus: ${field} must be a string or null, got ${JSON.stringify(value[field])}`)
    }
  }
  if (value['payload'] !== undefined && (value['payload'] === null || typeof value['payload'] !== 'object')) {
    throw new Error('rolebus: payload must be an object when present')
  }
  return value as unknown as RoleBusEnvelope
}

/** 发件入参 → 完整信封（source 固定为本角色；timestamp 取当前 Unix 秒）。 */
function toEnvelope(options: PostOptions): RoleBusEnvelope {
  const envelope: RoleBusEnvelope = {
    event_id: options.event_id ?? randomUUID(),
    event_type: options.event_type,
    source: options.role,
    target: options.target,
    timestamp: Math.floor(Date.now() / 1000),
  }
  // exactOptionalPropertyTypes：只在确有值时写可选头字段，绝不写显式 undefined。
  if (options.task_id !== undefined) envelope.task_id = options.task_id
  if (options.program_id !== undefined) envelope.program_id = options.program_id
  if (options.correlation_id !== undefined) envelope.correlation_id = options.correlation_id
  if (options.severity !== undefined) envelope.severity = options.severity
  if (options.priority !== undefined) envelope.priority = options.priority
  if (options.payload !== undefined) envelope.payload = options.payload
  return envelope
}

/**
 * 发件：身份与路由校验通过后，把信封写成目标角色收件箱里的完整 JSON 文件。
 * 若目标收件箱已有同 event_id 文件，拒绝覆盖（fail loud，防同 id 重投歧义）。
 * @param options - 发件参数。
 * @returns 已投出的信封（含补全的 event_id/timestamp/source）。
 * @throws 本角色未赋予身份（role=unset/空）、event_type 不合规、target ∉ peers、
 * 同 event_id 已存在等任一情况。
 */
export async function post(options: PostOptions): Promise<RoleBusEnvelope> {
  if (options.role === '' || options.role === UNSET_ROLE) {
    throw new Error(`rolebus: role ${JSON.stringify(options.role)} is not a usable sender identity (profile 未覆盖 rolebus.role？)`)
  }
  if (options.peers.length === 0) {
    throw new Error(`rolebus: role ${JSON.stringify(options.role)} has no peers configured; cannot route a send`)
  }
  if (!options.peers.includes(options.target)) {
    throw new Error(
      `rolebus: target ${JSON.stringify(options.target)} is not a peer of ${JSON.stringify(options.role)} `
      + `(peers: ${options.peers.map(value => JSON.stringify(value)).join(', ') || 'none'})`,
    )
  }
  if (options.severity !== undefined && !(SEVERITIES as readonly string[]).includes(options.severity)) {
    throw new Error(`rolebus: severity must be one of ${SEVERITIES.join('/')}, got ${JSON.stringify(options.severity)}`)
  }
  // 发件侧对 priority 是严格的（收件侧只查类型，理由见 validateEnvelope）：写错在这里当场报，
  // 而不是让它过 wire 后靠收件侧回落掩盖。
  if (options.priority !== undefined && options.priority !== null && !(PRIORITIES as readonly string[]).includes(options.priority)) {
    throw new Error(`rolebus: priority must be one of ${PRIORITIES.join('/')}, got ${JSON.stringify(options.priority)}`)
  }
  const envelope = validateEnvelope(toEnvelope(options))
  const dir = inboxDir(options.root, options.target)
  await mkdir(dir, { recursive: true })
  const finalPath = join(dir, `${envelope.event_id}.json`)
  const pendingPath = join(dir, `${envelope.event_id}${PENDING_SUFFIX}-${process.pid}-${randomUUID()}`)
  if (await exists(finalPath)) {
    // rename 在 POSIX 上会覆盖目标，重复必须显式挡下（同 event_id 重投是歧义，不静默覆盖）。
    throw new Error(`rolebus: event_id ${JSON.stringify(envelope.event_id)} already exists in ${dir}`)
  }
  await writeFile(pendingPath, `${JSON.stringify(envelope)}\n`)
  await rename(pendingPath, finalPath).catch(async (error: unknown) => {
    await unlink(pendingPath).catch(() => undefined)
    throw error
  })
  return envelope
}

/** 一次收件的结果。 */
export interface DeliverResult {
  /** 本次投递（归档并交给调用方 emit）的事件。 */
  delivered: RoleBusEnvelope[]
  /** 因归档已有同 event_id 而跳过的事件数（重投/重启补收产生的重复）。 */
  duplicates: number
  /** 解析/校验失败或被移动出收件箱的事件数（含错路由 target）。 */
  invalid: number
}

/**
 * 收件：扫描本角色收件箱，校验合格且 target==myRole 的事件先归档（archive 即
 * 去重键）再移出收件箱；归档已有同 event_id 的重复直接丢弃；非法/错路由文件加
 * `.bad-`/`.misroute-` 前缀保留，避免循环重读。先 archive 后移除收件箱文件，
 * 崩溃只会留下“已归档 + 收件箱还在”状态，下次归入 duplicate 分支。
 * @param root - 共享 spool 根。
 * @param myRole - 本角色身份（只投 target == myRole 的事件）。
 * @returns 投递结果统计与事件列表。
 */
export async function deliver(root: string, myRole: RoleName): Promise<DeliverResult> {
  const dir = inboxDir(root, myRole)
  const archive = archiveDir(root, myRole)
  await mkdir(archive, { recursive: true })
  const result: DeliverResult = { delivered: [], duplicates: 0, invalid: 0 }
  let names: string[]
  try {
    names = await readdir(dir)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return result
    throw error
  }
  for (const name of names) {
    if (name.startsWith('.') || !name.endsWith('.json')) continue
    const path = join(dir, name)
    let envelope: RoleBusEnvelope
    try {
      envelope = validateEnvelope(JSON.parse(await readFile(path, 'utf8')) as unknown)
    } catch {
      await rename(path, join(dir, `.bad-${name}`)).catch(() => undefined)
      result.invalid++
      continue
    }
    if (envelope.target !== myRole) {
      await rename(path, join(dir, `.misroute-${name}`)).catch(() => undefined)
      result.invalid++
      continue
    }
    const archivePath = join(archive, `${envelope.event_id}.json`)
    if (await exists(archivePath)) {
      await unlink(path).catch(() => undefined)
      result.duplicates++
      continue
    }
    // 先写归档再删收件箱文件：中途崩溃 → 下次命中 duplicate 分支，保证 exactly-once 去重投递。
    await writeFile(archivePath, `${JSON.stringify(envelope)}\n`)
    await unlink(path).catch(() => undefined)
    result.delivered.push(envelope)
  }
  return result
}

async function exists(path: string): Promise<boolean> {
  try {
    await stat(path)
    return true
  } catch {
    return false
  }
}
