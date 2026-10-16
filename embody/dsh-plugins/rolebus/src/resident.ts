/**
 * @embody/dsh-rolebus/resident —— 常驻待机：让本角色进程启动时不跑模型，收到事件再唤醒。
 *
 * 一个角色进程里只有一个 agent，就是该角色自己：它承载本 profile 的人设、工具白名单与
 * 这次会话。headless 为同一个 profile 建的也是它，只不过跑完一轮就退出；这里让它一直
 * 活着，由事件驱动。**不存在第二个 agent**，也不另配人设或工具面。
 *
 * `resident: true` 时 `apply` 调用 {@link installResident}，它做两件事：
 *   1) 把本角色的 agent 建出来放着（或复用/续接已有的），**默认不发任何消息**，停在 idle，
 *      所以启动本身不产生模型请求（同一形态见引擎 acp 的 newSession）；唯一的例外是配了
 *      `startupWake` 的角色——那类角色在装配完成后立刻收到一条开机消息起一轮（见
 *      {@link bootMessage}），此后与其余角色一样只由事件唤醒。
 *      会话 id 固定为 `rolebus-<role>`：进程内已有同 id 的 agent 就复用；否则看
 *      `sessionPersistence.stat`——有落盘会话走 `resume` 续接上一次的上下文，没有才
 *      `create`。人设、preset、工具面都按本 profile 的部署解析，只有模型要在这里显式给：
 *     `agentDefaultModel.currentSelection()`（与 headless 建 agent 时的取值方式相同，
 *     `packages/bundle/headless/src/index.ts:183`），本角色的部署可用 rolebus config 的
 *     `modelProvider` 换掉其中的 provider（模型 id 不动，见 {@link ResidentSetup}）。
 *     引擎在**建 agent 时**定 route，不给就是
 *     空 provider/model，第一轮直接以 "has no provider/model" 失败——不是等到请求时才补。
 *     取到的 route 在**每次进程启动时读一次**，并定死这个进程建/续接的那个 loop 实例：
 *     `agent-loop/src/agent.ts:510-529` 里 route 取自 loop 自己的 options，会话里记过的请求头
 *     只用来恢复同一 route 下的 reasoning effort。所以重启续接也是这次读到的 route 生效（不是
 *     沿用会话里记的那条）——正因如此，这里必须等组合装载完成（见 {@link whenCompositionLoaded}）。
 *   2) 订阅本进程的 `rolebus/event`，把每封信封渲染成一条 user message，按**该封事件的优先级**
 *      推出唤醒方式交给 agent（见 {@link wakeResident}）：P1 `steer` 交给最近的步骤边界、
 *      P2 `followup` 独占下一轮、P3 `inject` 只投不唤醒。处理完回到 idle，下一轮仍由事件或
 *      （配了 `startupWake` 时的）开机自查带起来。
 *
 * 为什么这一步要显式 `create`：`send` / `followup` / `steer`（`Agent` 的方法，见
 * `packages/core/agent/src/runtime-types.ts:215-241`）都挂在**一个 Agent 实例**上，
 * 而进程启动时这个实例还不存在——`dsh --profile interactor` 不建 session、不发请求。
 * 引擎里每个唤醒方都是这两步，没有第三条路：先 `ctx.agents.create` 再 `followup`
 * （headless `index.ts:184,197`、acp `session.ts:128,299`、webhook `session.ts:133`、
 * sdk server `server.ts:279`）。
 *
 * 订阅在 agent 就绪**之前**建立：开启期间到达的事件排在就绪 promise 的 `.then` 队列里，
 * 保持 FIFO。收件侧已把事件写进归档，丢一封就永远找不回来，因此这里不能有「还没建好
 * 就忽略」的窗口。
 *
 * 模型可见文本由 {@link renderEnvelope} 生成：一封信封 → 一段「谁发来什么、要做什么、带哪些
 * 字段」的文字，不含 spool/文件/轮询这类传输层词汇（收件方是角色自己的模型，不是运维）。
 * @module @embody/dsh-rolebus/resident
 */

import type { Context } from '@deepseek-ai/cordis'
import { brandString } from '@deepseek-ai/dsh-brand'
import type { Agent, AgentHandle, ModelSelection } from '@deepseek-ai/dsh-agent'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type { SessionId, UserMessage } from '@deepseek-ai/dsh-session'
// 空类型导入带进 `Context.sessionPersistence` / `Context.agentDefaultModel` 的声明合并
// （引擎惯例：用到哪个服务就导入哪个包的类型，服务契约留在声明它的包里）。
import type {} from '@deepseek-ai/dsh-session-persistence'
import type {} from '@deepseek-ai/dsh-agent-default-model'
import { findEventSpec, wakeModeFor, type WakeMode } from './event-catalog.ts'
import type { RoleBusEnvelope, RoleName } from './types.ts'

/** rolebus 交给模型的唤醒消息在会话里的 `source.plugin`，同时用作日志前缀。 */
export const PLUGIN_SOURCE = 'rolebus'

/** 唤醒文本的收尾指令（角色各自该做什么由人设定义，这里只说明这一轮的处理边界）。 */
const CLOSING_LINE = '按你的职责处理这件事，处理完结束本轮。'

/** 开机自查那一轮的抬头；角色人设按这个抬头区分「这一轮不是事件」。 */
const BOOT_HEADING = '【开机自查】'

/** 开机那一轮的收尾指令；与信封那条分开写：这里的「这件事」是自查，不是某封事件。 */
const BOOT_CLOSING_LINE = '按你的职责完成这次自查，处理完结束本轮。'

/** 信封可选头字段的中文标签，按渲染顺序排列。 */
const HEADER_LABELS = [
  ['priority', '优先级'],
  ['task_id', '任务号'],
  ['program_id', '程序号'],
  ['correlation_id', '关联号'],
  ['severity', '级别'],
] as const

/** payload 键的标签；目录里出现过的载荷字段都在此，未列出的键原样打印。 */
const PAYLOAD_LABELS: Readonly<Record<string, string>> = {
  description: '说明',
  action: '动作',
  status: '状态',
  component: '组件',
  name: '名称',
}

/**
 * 本角色 agent 的会话 id：只由角色决定，因此进程重启后仍指向同一个会话。
 * @param role - 角色身份（= `rolebus.role`）。
 * @returns 该角色的常驻会话 id。
 */
export function residentSessionId(role: RoleName): SessionId {
  return brandString<SessionId>(`rolebus-${role}`)
}

/** 载荷值转文本：字符串原样，其余用 JSON（对象/数组/数字都保真）。 */
function formatValue(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value) ?? String(value)
}

/**
 * 把一封信封渲染成模型可见的一轮输入。
 *
 * 事件语义取自目录条目（`event-catalog.ts`）的 `description`——与模型在发件工具上看到的
 * 是同一段文字，不引入第二套说明。目录里查不到的事件（收件侧不按目录校验）退化成只印
 * 事件类型：已投递的事件不能再丢，渲染不出语义时也要交给模型。条目标了 `hiddenPayload`
 * 的 payload 键（如两条任务事件的 `items`——机器改清单的字段改动）只进信封留审计，**不进
 * 唤醒文本**：收件方该看的是人话说明，改动本身在清单文件里。
 * @param envelope - 本角色收到的一封事件。
 * @returns 交给常驻 agent 的消息文本（唤醒方式见 {@link wakeResident}）。
 */
export function renderEnvelope(envelope: RoleBusEnvelope): string {
  const lines = [`【角色事件】${envelope.source} → ${envelope.event_type}`]
  const spec = findEventSpec(envelope.source, envelope.event_type)
  if (spec !== undefined) lines.push(spec.description)
  for (const [field, label] of HEADER_LABELS) {
    const value = envelope[field]
    if (value === undefined || value === null) continue
    lines.push(`${label}：${String(value)}`)
  }
  const hidden = new Set(spec?.hiddenPayload ?? [])
  for (const [key, value] of Object.entries(envelope.payload ?? {})) {
    if (hidden.has(key)) continue
    lines.push(`${PAYLOAD_LABELS[key] ?? key}：${formatValue(value)}`)
  }
  lines.push(CLOSING_LINE)
  return lines.join('\n')
}

/**
 * 一封信封 → 一条插件来源的 user message（`id`/`role` 由引擎补全）。
 * @param envelope - 本角色收到的一封事件。
 * @returns 可直接交给常驻 agent 的消息。
 */
export function wakeMessage(envelope: RoleBusEnvelope): UserMessage {
  return createUserMessage({
    content: [{ type: 'text', text: renderEnvelope(envelope) }],
    source: { kind: 'plugin', plugin: PLUGIN_SOURCE },
  })
}

/**
 * 一次开机自查 → 一条插件来源的 user message（形状与 {@link wakeMessage} 同：抬头 + 说明 +
 * 收尾指令，只是没有信封头，整段说明就是配置给的那句）。
 *
 * 谁还需要这一轮：**只有交互器** —— 它开机要把还没做完的任务问给用户、听答、按用户的话写回
 * 清单（做法见 Interactor/prompts/ 的【开机自查】），全程要跟用户一来一回，只有一条消息能把它
 * 带起来。要问哪几批任务**由代码挑好、拼在 `startupWake` 里**（见 Interactor/resident.patch.yml），
 * 这一轮不负责挑。其余角色（含编排器）都不配 `startupWake`：清单该不该问用户全在交互器那一轮里，
 * 它们起来直接待机，那种启动不产生任何模型请求。
 * @param startupWake - 该角色 profile 配的开机说明（`rolebus.startupWake`）。
 * @returns 可直接交给常驻 agent 的消息。
 */
export function bootMessage(startupWake: string): UserMessage {
  return createUserMessage({
    content: [{ type: 'text', text: [BOOT_HEADING, startupWake, BOOT_CLOSING_LINE].join('\n') }],
    source: { kind: 'plugin', plugin: PLUGIN_SOURCE },
  })
}

/**
 * 三种唤醒方式 → 引擎 `Agent` 上的同名方法。表驱动：以后加第四种方式时，这里与
 * `event-catalog.ts` 的 `WAKE_MODES` 是仅有的两个改动点（`Record<WakeMode, …>` 要求键齐全）。
 */
const WAKE_CALLS: Readonly<Record<WakeMode, (agent: Agent, message: UserMessage) => void>> = {
  followup: (agent, message) => agent.followup(message),
  steer: (agent, message) => agent.steer(message),
  inject: (agent, message) => agent.inject(message),
}

/**
 * 把一封事件交给常驻 agent，唤醒方式由该封事件的优先级推出（{@link wakeModeFor}），一对一
 * 调用引擎的同名方法，不按角色忙闲做任何改写。
 *
 * 三种方式的差别只在角色**忙碌**（这一轮在跑）时才显出来：`steer` 交给最近的一个步骤边界、
 * 搭在这一轮已有的请求上；`followup` 等这一轮收敛后独占下一轮；`inject` 只投不唤醒。空闲时
 * `steer` / `followup` 都立刻开一轮，`inject` 不会 —— 空闲角色收到 inject 事件后没人取走它，
 * 后果见 `event-catalog.ts` 的 `WAKE_MODES`。
 *
 * 已知限制：投递若落进引擎 driver 的**退休窗口**（本轮最后一次查收件箱与提交 idle 之间），
 * 三种方式都会被搁置——引擎自身的缺陷（`packages/jobs/tool-jobs/README.md` 的 Known
 * Limitations：steering 与 inject 都被这条窗口漏掉，闭合它属于 agent-loop）。本插件不兜底：
 * 兜底要把同一条消息再投一次，而 inbox 投影按消息 id 去重，同 id 重投会被判成非法 splice。
 * @param agent - 本角色的常驻 agent（停在 idle 或正在跑一轮）。
 * @param envelope - 本角色收到的一封事件。
 */
function wakeResident(agent: Agent, envelope: RoleBusEnvelope): void {
  WAKE_CALLS[wakeModeFor(envelope)](agent, wakeMessage(envelope))
}

/** 开启结果：`owned` 为假表示复用了进程内已有的 agent，卸载时不得拆它。 */
interface ResidentStartup {
  readonly agent: Agent
  readonly owned: boolean
  readonly dispose: () => Promise<void>
}

/**
 * 等组合装载完成再读默认模型：`agentDefaultModel` 在 settings 接上之前给的是组合默认值。
 *
 * 本插件由 `ctx.inject([agents, agentDefaultModel, sessionPersistence])` 触发，那个回调只保证这
 * 三个服务在；settings-file 是同一层里的另一行，可能在它之后才装上。实测过：那个时刻读到的
 * selection 仍是组合默认值，而 `ctx.get('settings')` 此刻还是 undefined——所以不能等某个服务，
 * 要等**整棵组合树**装完（Loader 的 `await()`，app-boot 启动应用时等的正是它）。没有 Loader 的
 * 宿主不等：那里没有「装载完成」这个概念，读到的就是该宿主当前的默认值。
 * @param ctx - 已注入 `agentDefaultModel` 的 context。
 */
async function whenCompositionLoaded(ctx: Context): Promise<void> {
  await ctx.get('loader')?.await()
}

/**
 * 装配常驻 agent 的部署选择，两项都来自本角色的 rolebus config（空串＝未配置，见其 `Config`）。
 */
export interface ResidentSetup {
  /** 开机自查的说明文本；非空时装配完成后主动起一轮（见 {@link bootMessage}）。 */
  readonly startupWake: string
  /**
   * 模型路由覆盖：非空时本角色改走这条 provider，模型 id 仍取 `agent-default-model`。
   * 构造覆盖用的 selection 时**不带 `reasoningEffort`**——丢掉档位正是关掉思考的那一动作。
   */
  readonly modelProvider: string
}

/**
 * 本次常驻 agent 用的模型选择：未配 `modelProvider` 时就是组合解析出的默认值（一字不改）。
 * @param defaults - `agentDefaultModel.currentSelection()` 的结果。
 * @param modelProvider - 本角色的路由覆盖；空串表示不覆盖。
 * @returns 交给 `agents.create` / `agents.resume` 的模型选择。
 */
function residentSelection(defaults: ModelSelection, modelProvider: string): ModelSelection {
  if (modelProvider === '') return defaults
  return { provider: modelProvider, model: defaults.model }
}

/**
 * 建出本角色的 agent（幂等；不发送任何消息）：进程内已有就复用，有落盘会话就续接。
 * @param ctx - 已注入 `agents` / `agentDefaultModel` / `sessionPersistence` 的 context。
 * @param role - 角色身份。
 * @param setup - 本次装配的部署选择（模型路由覆盖在其内）。
 * @returns 该角色的 agent 与它的所有权。
 */
async function openResident(ctx: Context, role: RoleName, setup: ResidentSetup): Promise<ResidentStartup> {
  const sessionId = residentSessionId(role)
  const existing = ctx.agents.get(sessionId)
  if (existing !== undefined) {
    return { agent: existing, owned: false, dispose: async () => undefined }
  }
  await whenCompositionLoaded(ctx)
  const agentOptions = residentSelection(ctx.agentDefaultModel.currentSelection(), setup.modelProvider)
  // 显式二选一，不做 try/catch 兜底：会话在不在由持久化层回答，不是猜出来的。
  const stored = await ctx.sessionPersistence.stat(sessionId)
  const handle: AgentHandle = stored === undefined
    ? await ctx.agents.create({ sessionId, meta: { cwd: process.cwd() }, agentOptions })
    : await ctx.agents.resume({ resumeSessionId: sessionId, agentOptions })
  return { agent: handle.agent, owned: true, dispose: () => handle.dispose() }
}

/**
 * 装配常驻待机：建出本角色的 agent、订阅本角色事件、把事件转成模型轮次，卸载时拆掉自己建的那个。
 *
 * 一个 `ctx.effect` 覆盖整个生命周期——建立、订阅、拆除是同一件事的三个阶段，没有
 * 独立的就绪状态需要第二个人负责。建立失败只记一次（console），之后到达的事件静默丢弃：
 * 那时事件已进归档，能做的只有把失败留在日志里。
 * @param ctx - 已注入 `agents` / `sessionPersistence` 的 context。
 * @param role - 角色身份。
 * @param setup - 本次装配的部署选择（开机自查文本与模型路由覆盖）。
 */
export function installResident(ctx: Context, role: RoleName, setup: ResidentSetup): void {
  ctx.effect(() => {
    let disposed = false
    let owned: ResidentStartup | undefined

    const startup: Promise<ResidentStartup | undefined> = (async () => {
      const opened = await openResident(ctx, role, setup)
      // 卸载发生在建立完成之前：agent 已经建出来了，必须由这次建立自己拆掉。
      if (disposed) {
        await opened.dispose()
        return undefined
      }
      owned = opened
      // 开机自查排在所有事件之前：订阅方都挂在同一个 startup promise 上，这一段跑完才轮到
      // 它们，所以它必然先于任何排队事件进入 agent。判据是「这次是本进程建的」而不是「这次是
      // 新建会话」——续接（会话没被重置）同样是刚拉起，一样该回头看一眼没做完的任务。
      //
      // 等一次 whenIdle 再投，与引擎自己的两步用法一致（headless bundle 的
      // `agents.create` → `whenIdle` → `followup`）：刚建出来的 agent 立刻就是 idle，
      // 这次等待不花时间，只是不在驱动起步之前抢投。
      if (setup.startupWake) {
        await opened.agent.whenIdle()
        opened.agent.followup(bootMessage(setup.startupWake))
      }
      return opened
    })().catch((error: unknown) => {
      console.error(`[rolebus] 常驻 agent 建立失败（角色 ${JSON.stringify(role)}），事件将无人处理：`, error)
      return undefined
    })

    const stopListening = ctx.on('rolebus/event', envelope => {
      void startup
        .then(opened => {
          if (opened !== undefined) wakeResident(opened.agent, envelope)
        })
        .catch((error: unknown) => {
          console.error('[rolebus] 常驻 agent 唤醒失败，该事件无人处理：', error)
        })
    })

    return async () => {
      disposed = true
      stopListening()
      // 建立尚未完成时由 startup 分支自己拆（见上）；这里只管已经到手的那个。
      if (owned !== undefined) await owned.dispose()
    }
  }, 'rolebus.resident()')
}
