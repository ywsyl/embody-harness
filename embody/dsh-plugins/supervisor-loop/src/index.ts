/**
 * @embody/dsh-supervisor-loop —— 监督器（Supervisor）的定时观察循环。
 *
 * 本项目里每个角色是各自独立的 dsh profile/进程。监督器与其余角色的**唤醒源不同**：
 * interactor / orchestrator / planner 由 rolebus 事件唤醒（`rolebus.resident: true`，
 * 见 ../rolebus/src/resident.ts），监督器不接任何入站事件——rolebus 事件目录里没有一条
 * target 是 supervisor 的条目，它只发不收。它的唤醒源是本插件：**每隔 intervalMs 读一遍
 * 配置里的观测 yaml（本部署的三份：视觉观察 / 本体状态 / 运动状态，见 Supervisor/resident.patch.yml，
 * 路径来自 configs/paths.py），把原文拼成一轮输入交给本角色 agent**，
 * agent 判断这一轮有没有需要告警的事件：有就调 rolebus 的发件工具，没有就结束本轮。
 *
 * 因此本插件持有本角色 agent 的整个生命周期（建/续接/拆），rolebus 对它的 `resident`
 * 始终为 false——两个插件都想持有同一个会话就会互相踩（同一会话 id 的写租约只有一个持有者）。
 * 会话 id 与建/续接的二选一手法与 rolebus 的常驻形态完全一致：进程内有同 id 就复用；
 * 否则看 `sessionPersistence.stat`，有落盘会话走 `resume` 续接上一次的上下文，没有才 `create`。
 * 人设、工具面按本角色 profile 解析，模型要在这里显式给（`agentDefaultModel.currentSelection()`，
 * 可选地被本插件 config 的 `modelProvider` 换成另一条路由）——引擎在建 agent 时定 route，
 * 不给就是空 provider/model，第一轮直接失败。
 *
 * 四条实现约定：
 *   1) **不解析 YAML，但去掉注释**：观测文件按原文读进来，只把 `#` 注释切掉（见
 *      {@link stripComments}）——行尾注释与整行注释都不进上下文。**注释不传给模型**是
 *      2026-10-14 定的：注释是写给人看的，模型侧的口径只有人设速查那一份，而这两份带注释的
 *      观测里注释占了原文一半以上，每轮送一遍就是白花 token（文件本身照旧带注释）。yaml
 *      解析要么引入 `yaml` 运行时依赖，要么自己写一半，而模型读 yaml 原文没有障碍；文件读不到
 *      时把失败原因原样交给模型（观测源本身异常也是一条可告警的事实），不静默跳过、也不当成空轮。
 *   2) **首轮迟到一个 `firstTickDelayMs`**：启动不跑模型（与其余角色的常驻形态一致），
 *      进程起来时没有任何问候，也没有任何模型请求；第一个**被处理**的观察落在第一个
 *      ≥ `firstTickDelayMs` 的间隔边界上，之前的每一跳都丢掉（不打日志，同第 3 条）。
 *      为什么要这一条：监督器看的三份观测文件在本进程起来之前就在盘上（上一轮留下的），
 *      若本进程一起来就读，读到的是**上一轮的账**——真机实测报过「通信链路中断…距今约
 *      3 小时」，一分钟后又报「链路恢复」。等满一个最长的产出周期，盘上那份已被本轮的
 *      观测器至少覆盖写过一次。取值见 configs/configs.py 的 SUPERVISOR_FIRST_TICK_DELAY_SEC
 *      （0 = 不延迟：本轮没有观测器可等时用，见 scripts/role-daemon.sh 过桥那一段）。
 *   3) **上一轮没跑完就跳过本轮**：观察是采样，不是队列——上一轮的读数已被这一轮的新读数
 *      取代，补跑一串过期观察没有意义。跳过不打日志：间隔是秒级而一轮要跑几十秒，跳过是常态，
 *      打出来会占满终端；要看一轮的耗时，取会话日志里相邻两条观测抬头的间隔。
 *   4) **日志到上限就轮转**：这是唯一一个「跑不完」的角色，会话日志只增不减（引擎没有
 *      裁剪/保留接口，压缩也不动原始日志）。所以当前会话的日志到 `maxSessionBytes` 就换成
 *      一份新会话，**不带上一份的任何记忆**：它按当前观测从零判断，会把它看到的异常重新报
 *      一遍——与重启一次（本角色重启续接的是基础 id 那一份）语义相近，差别是重启不换 id、
 *      轮转要换。同一份会话换不回来（create 见盘即抛 SessionAlreadyExistsError），所以每次
 *      轮转派生一个新 id，旧的那份由 scripts/role-daemon.sh 在下次启动时归档（轮转件没有进程
 *      会 resume，无论本角色在不在 RESET_ON_START_ROLES 里都归档）。
 *   5) **每轮顺手清一次 runtime_files/**：按 `cleanupScript` 起一个子进程跑一遍三个目录的
 *      体积清理（见 {@link runCleanup}，脚本是 Observer/cleanup.py）。它**不经过大模型**：
 *      不 await、不进上下文、不改本轮的观测输入，模型看到的一个字节都不因此改变；真删了东西
 *      才记一行日志。放在本插件是因为这种清理要在**运行期间**反复做（开机/收尾各一次的形态
 *      盖不住运行期长胖），而它是全仓唯一「一直在跑」的循环。
 *
 * 定时器用 `ctx.effect` + `setInterval`（与 rolebus 的收件轮询同一写法，不依赖 timer
 * 插件在不在）。它同时持有事件循环——空闲 agent 自身不持有任何句柄，没有这个定时器
 * node 会直接退出。
 *
 * 依赖解析：引擎包未发布、本 checkout 未构建 lib，因此本包**不声明引擎依赖**（否则
 * profile `pnpm add` 会在 DSH_HOME 内解析失败）；运行时在 tsx 源码启动的 dsh（fork 根的
 * 开发形态，`@deepseek-ai/*` 经 tsconfig paths 指向 src）内解析。与 ../rolebus 同形。
 *
 * 遵循 dsh 引擎函数插件约定：命名导出 name / Config / apply，无默认导出（默认导出
 * 会被 Loader 丢弃命名导出元信息）。Config 用 schemastery 校验：部署选择全是 required
 * （intervalMs / sessionId / sources / firstTickDelayMs 等由 profile 层给全，漏了就在装载时报
 * missing required value，而不是悄悄退化成某个内置节拍），另有两个例外——`python` 有环境缺省
 * `python3`，`modelProvider` 可选（不配 = 不改路由，是合法状态，见其 JSDoc）。
 * @module @embody/dsh-supervisor-loop
 */

import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import { brandString } from '@deepseek-ai/dsh-brand'
import type { Agent, AgentHandle, ModelSelection } from '@deepseek-ai/dsh-agent'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type { SessionId, UserMessage } from '@deepseek-ai/dsh-session'
// 空类型导入带进 `Context.sessionPersistence` / `Context.agentDefaultModel` 的声明合并
// （引擎惯例：用到哪个服务就导入哪个包的类型，服务契约留在声明它的包里）。
import type {} from '@deepseek-ai/dsh-session-persistence'
import type {} from '@deepseek-ai/dsh-agent-default-model'

export const name = 'supervisor-loop'

/** 观察输入在会话里的 `source.plugin`，同时用作日志前缀。 */
export const PLUGIN_SOURCE = 'supervisor-loop'

/** 一轮观察的来源：模型可见的标题 + yaml 路径。 */
export interface ObserveSource {
  /** 模型可见的来源名，渲染成该段的标题（如「视觉观察」）。 */
  readonly name: string
  /** 观测 yaml 路径；相对路径按启动工作目录（= 引擎根，与人设 `!!js` 同一约定）解析。 */
  readonly path: string
}

/**
 * 本插件的部署选择。全部 required —— 节拍、会话身份、观测源都是随部署变的量，
 * 由 profile 层给全；插件里没有可退化的默认（缺了在装载时 fail loud）。
 */
export interface Config {
  /** 轮询间隔毫秒（「每隔 n 秒」）；必须为正整数，否则 apply 抛错。 */
  intervalMs: number
  /**
   * 首个**被处理**的观察距装配的延迟毫秒：这段窗口内的每一跳都丢掉。必须为非负整数
   * （0 = 不延迟），否则 apply 抛错。首个实际观察落在第一个 ≥ 它的间隔边界上，所以取
   * intervalMs 的整数倍时最直观。只作用于本进程的第**一**次观察：会话轮转不重置它。
   */
  firstTickDelayMs: number
  /**
   * 本角色会话的**基础 id**：进程启动时用它建/续接；一个 DSH_HOME 内同一 id 只能有一个
   * 进程持有。运行期轮转出来的会话是 `<基础 id>.<时间戳>`（见 {@link createRotationAgent}）。
   */
  sessionId: string
  /** 单个会话日志的字节上限，到量就轮转；必须为正整数，否则 apply 抛错。 */
  maxSessionBytes: number
  /** 每轮读取的观测源，按数组顺序渲染进上下文；至少一个。 */
  sources: ObserveSource[]
  /**
   * 每轮旁路执行的 runtime_files 体积清理脚本（Observer/cleanup.py 的绝对路径，来自
   * configs/paths.py 的 RUNTIME_CLEANUP_SCRIPT）。空串或文件不存在在 apply 抛错。
   */
  cleanupScript: string
  /** 跑 `cleanupScript` 的解释器可执行文件；该脚本依赖 PyYAML 之类的仓库内依赖。 */
  python: string
  /**
   * 模型路由覆盖：非空时本角色改走这条 provider，模型 id 仍取 `agent-default-model`；
   * 空串或未配置时照走默认选择（一字不改）。
   *
   * 构造覆盖用的 selection 时**不带 `reasoningEffort`**——丢掉档位正是关掉扩展思考的那一动作
   * （与 `../rolebus` 的同名字段同一机制，见其 `resident.ts` 的 `residentSelection`）。本角色的
   * agent 由本插件建（rolebus 在监督器这侧不是常驻），所以这个口必须在本插件上，不能借 rolebus。
   */
  modelProvider?: string
}

/** Schemastery 配置：required 在此声明，应用层不再做隐藏默认。 */
export const Config: z<Config> = z.object({
  intervalMs: z.number().required(),
  firstTickDelayMs: z.number().required(),
  sessionId: z.string().required(),
  maxSessionBytes: z.number().required(),
  sources: z.array(z.object({
    name: z.string().required(),
    path: z.string().required(),
  })).required(),
  cleanupScript: z.string().required(),
  // 唯一一个带默认值的字段：解释器名是环境事实（本部署恒为 python3），不是部署选择；
  // 写法与 ../tools-bridge 的 python 一致。
  python: z.string().default('python3'),
  // 可选（没有 `.required()`，也没给默认）：未配置 = 不改路由，是合法状态而非漏配。
  modelProvider: z.string(),
})

/** 观测源读到的东西：原文，或读不到的原因（两者都进上下文，见模块注释）。 */
export interface SourceText {
  /** 与 {@link ObserveSource.name} 同源，渲染成段标题。 */
  readonly name: string
  /** 配置里写的路径原文，渲染在标题里便于人对照。 */
  readonly path: string
  /** 去注释后的原文（去完为空是 `（文件为空）`），或 `（读取失败：…）`。 */
  readonly text: string
}

/**
 * 读一个观测源，归一成模型可见文本：**去掉注释**（见 {@link stripComments}）。
 *
 * 读不到时**不抛**：观测源缺失/不可读本身就是要交给模型的事实（监督器的职责之一就是
 * 发现感知端与控制端「没在供数」），静默跳过会让模型把「没有提供」误读成「没有问题」。
 * @param source - 一个观测源的标题与路径。
 * @param cwd - 解析相对路径的基准目录（启动工作目录 = 引擎根）。
 * @returns 该源的标题、路径与去注释文本。
 */
export async function readSource(source: ObserveSource, cwd: string): Promise<SourceText> {
  const path = resolve(cwd, source.path)
  const base = { name: source.name, path: source.path }
  try {
    const text = stripComments((await readFile(path, 'utf8')).trim())
    // 文件本来就空、与「整份只有注释」在这里同一条：两者都没有观测值可传。
    return { ...base, text: text === '' ? '（文件为空）' : text }
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error)
    return { ...base, text: `（读取失败：${reason}）` }
  }
}

/**
 * 切掉一份观测文本里的注释：行尾注释与整行注释都不进上下文，空行一并去掉。
 *
 * 只切注释，**不动值**。判据用 YAML 自己的注释规则——`#` 落在行首或空白之后、且不在引号里
 * 才是注释起始；所以这里数一遍引号状态（双引号里 `\` 转义下一个字符，单引号里 `''` 是一个
 * 字面单引号）。值里真含 `#` 的字段由生产端加了引号（如 SSID / 地名，见
 * Observer/status_observer.py 的渲染），裸值里不可能出现「空白 + `#`」（那在 YAML 里本来就是
 * 注释）。引号状态判错的后果是不对称的：只可能把注释**漏**在上下文里（值照传），不会把值切掉。
 * @param text - 观测文件原文（去注释前，已 trim）。
 * @returns 去注释后的文本；整份只有注释时返回空串。
 */
export function stripComments(text: string): string {
  const kept: string[] = []
  for (const line of text.split('\n')) {
    const start = commentStart(line)
    const body = (start === -1 ? line : line.slice(0, start)).trimEnd()
    if (body !== '') kept.push(body)
  }
  return kept.join('\n')
}

/**
 * 一行的注释起始下标。
 * @param line - 观测文件里的一行原文。
 * @returns 注释起始下标；这一行没有注释（或引号没闭合、其后不再有注释边界）时返回 `-1`。
 */
function commentStart(line: string): number {
  for (let at = 0; at < line.length; at += 1) {
    const ch = line.charAt(at)
    if (ch === '"' || ch === "'") {
      at = quoteEnd(line, at, ch)
      continue
    }
    if (ch === '#' && (at === 0 || line.charAt(at - 1) === ' ' || line.charAt(at - 1) === '\t')) {
      return at
    }
  }
  return -1
}

/**
 * 从起始引号找它这一行的收尾引号。
 * @param line - 观测文件里的一行原文。
 * @param start - 起始引号的下标。
 * @param quote - 起始引号（`"` 或 `'`）。
 * @returns 收尾引号的下标；这一行里没有收尾引号时返回行尾下标（其后当作没有注释边界）。
 */
function quoteEnd(line: string, start: number, quote: string): number {
  for (let at = start + 1; at < line.length; at += 1) {
    const ch = line.charAt(at)
    if (quote === '"' && ch === '\\') {
      at += 1
      continue
    }
    if (ch !== quote) continue
    if (quote === "'" && line.charAt(at + 1) === "'") {
      at += 1
      continue
    }
    return at
  }
  return line.length
}

/**
 * 一轮观察的模型可见文本：本轮抬头 + 每个源一段原文 + 收尾的处理边界。
 *
 * 抬头只写**时间**与采样间隔，不写轮次：判断观测陈旧与否靠的是时间戳，而「这是我第几次
 * 看到同一状态」在模型自己的会话历史里就有——插件里再数一个轮次会与那份历史漂（它按进程
 * 计、重启归零，会话里的回合却是续接的）。收尾只说明这一轮的处理边界，该发什么事件由人设定义。
 * @param sources - 本轮各观测源的文本，按配置顺序。
 * @param header - 抬头说明，形如 `2026-09-15 10:00:03（+08:00）（间隔 10 秒）`。
 * @returns 交给 `agent.followup()` 的消息文本。
 */
export function renderObservation(
  sources: readonly SourceText[],
  header: string,
): string {
  const lines = [`【定时观察】${header}`]
  for (const source of sources) {
    lines.push('', `## ${source.name}（${source.path}）`, source.text)
  }
  lines.push('', '按你的职责判断这一轮有没有需要告警的事件：有就发 rolebus 事件，没有就结束本轮。')
  return lines.join('\n')
}

/**
 * 一段观察文本 → 一条插件来源的 user message（`id`/`role` 由引擎补全）。
 * @param text - {@link renderObservation} 的产物。
 * @returns 可直接交给 `agent.followup()` 的消息。
 */
export function observeMessage(text: string): UserMessage {
  return createUserMessage({
    content: [{ type: 'text', text }],
    source: { kind: 'plugin', plugin: PLUGIN_SOURCE },
  })
}

/** 两位补零。抬头与轮转会话 id 都要用，所以放在模块级而不是各写一份。 */
function pad2(value: number): string {
  return String(value).padStart(2, '0')
}

/** 本地的 `YYYYMMDD-HHMMSS`，用于轮转出的会话 id（排序即时间序，人读也方便）。 */
function localStamp(at: Date): string {
  return `${at.getFullYear()}${pad2(at.getMonth() + 1)}${pad2(at.getDate())}`
    + `-${pad2(at.getHours())}${pad2(at.getMinutes())}${pad2(at.getSeconds())}`
}

/** 开启结果：`owned` 为假表示复用了进程内已有的 agent，卸载时不得拆它。 */
interface LoopStartup {
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
 * 宿主不等：那里没有「装载完成」这个概念，读到的就是该宿主当前的默认值。rolebus 的常驻路径为
 * 同一件事留着同一段等待（`rolebus/src/resident.ts`）。
 * @param ctx - 已注入 `agentDefaultModel` 的 context。
 */
async function whenCompositionLoaded(ctx: Context): Promise<void> {
  await ctx.get('loader')?.await()
}

/**
 * 本次建 agent 用的模型选择：未配 `modelProvider` 时就是组合解析出的默认值（一字不改）。
 *
 * 非空时只换 provider、**丢掉 `reasoningEffort`**——那条镜像路由声明了推理档位而这里不选档位，
 * 线上就是 thinking 的关闭值（见 `Config.modelProvider`）。与 `rolebus/src/resident.ts` 的
 * `residentSelection` 逐字同形：两个插件各建各的常驻 agent，这一小段没有共享去处。
 * @param defaults - `agentDefaultModel.currentSelection()` 的结果。
 * @param modelProvider - 归一化后的路由覆盖值；空串表示不覆盖。
 * @returns 交给 `agents.create` / `agents.resume` 的 agentOptions。
 */
function loopSelection(defaults: ModelSelection, modelProvider: string): ModelSelection {
  if (modelProvider === '') return defaults
  return { provider: modelProvider, model: defaults.model }
}

/**
 * 建出本角色的 agent（幂等；不发送任何消息）：进程内已有就复用，有落盘会话就续接。
 * @param ctx - 已注入 `agents` / `agentDefaultModel` / `sessionPersistence` 的 context。
 * @param sessionId - 本角色会话 id（= `Config.sessionId`）。
 * @param modelProvider - 归一化后的模型路由覆盖值（见 {@link loopSelection}）。
 * @returns 该 agent 与它的所有权。
 */
async function openLoopAgent(ctx: Context, sessionId: SessionId, modelProvider: string): Promise<LoopStartup> {
  const existing = ctx.agents.get(sessionId)
  if (existing !== undefined) {
    return { agent: existing, owned: false, dispose: async () => undefined }
  }
  await whenCompositionLoaded(ctx)
  const agentOptions = loopSelection(ctx.agentDefaultModel.currentSelection(), modelProvider)
  // 显式二选一，不做 try/catch 兜底：会话在不在由持久化层回答，不是猜出来的。
  const stored = await ctx.sessionPersistence.stat(sessionId)
  const handle: AgentHandle = stored === undefined
    ? await ctx.agents.create({ sessionId, meta: { cwd: process.cwd() }, agentOptions })
    : await ctx.agents.resume({ resumeSessionId: sessionId, agentOptions })
  return { agent: handle.agent, owned: true, dispose: () => handle.dispose() }
}

/**
 * 轮转专用的建 agent：**只 create，绝不 resume**。
 *
 * 与启动路径的二选一不同，这里不做 resume：候选 id 是新派生的，落盘只可能是「同一秒内已经
 * 轮转过一次」，续接那种东西是错的。候选先 `stat` 预检、撞了就换下一个，而不是去 catch
 * `SessionAlreadyExistsError`——那个错误类在本包里没有别的用处，不值得为它引一个运行时导入。
 * @param ctx - 已注入 `agents` / `agentDefaultModel` / `sessionPersistence` 的 context。
 * @param base - 基础会话 id（= `Config.sessionId`），轮转 id 由它派生。
 * @param modelProvider - 归一化后的模型路由覆盖值（见 {@link loopSelection}）；轮转出来的
 *   会话必须与启动那份走同一条路由，否则轮转一次思考就自己开回来了。
 * @returns 新会话的 agent 与它的所有权（一定是 `owned: true`：这个 id 不可能是别人建的）。
 * @throws 三个候选 id 都撞上已有会话（正常部署下不可能，除非时钟被拨回去）。
 */
async function createRotationAgent(ctx: Context, base: string, modelProvider: string): Promise<LoopStartup> {
  const stem = `${base}.${localStamp(new Date())}`
  await whenCompositionLoaded(ctx)
  const agentOptions = loopSelection(ctx.agentDefaultModel.currentSelection(), modelProvider)
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const sessionId = brandString<SessionId>(attempt === 1 ? stem : `${stem}-${attempt}`)
    if (await ctx.sessionPersistence.stat(sessionId) !== undefined) continue
    const handle: AgentHandle = await ctx.agents.create({
      sessionId,
      meta: { cwd: process.cwd() },
      agentOptions,
    })
    return { agent: handle.agent, owned: true, dispose: () => handle.dispose() }
  }
  throw new Error(`supervisor-loop: 连试 3 个轮转会话 id 都撞上已有会话（${stem} …）`)
}

/** 间隔的可读写法：整秒写「n 秒」，否则写毫秒。 */
function intervalLabel(intervalMs: number): string {
  return intervalMs % 1000 === 0 ? `${intervalMs / 1000} 秒` : `${intervalMs} 毫秒`
}

/**
 * 抬头里的本轮时间：**本地时间** + 时区偏移。
 *
 * 观测文件里的时间戳是本地时间（生产端写的就是本机时间），抬头若用 UTC，模型要拿
 * `getTimezoneOffset` 做一次换算才能判断「这份观测是不是陈旧」——真机实测里它为此
 * 调了两次 `stat` 和一次 `date`，把一轮花在对时上。同一基准写在抬头里就没有这一步。
 * 偏移随部署变（跨时区/夏令时），所以写出来而不是写死。
 * @returns 形如 `2026-09-15 15:50:49（+08:00）`。
 */
function localNow(): string {
  const now = new Date()
  const offsetMinutes = -now.getTimezoneOffset()
  const sign = offsetMinutes < 0 ? '-' : '+'
  const offset = `${sign}${pad2(Math.floor(Math.abs(offsetMinutes) / 60))}:${pad2(Math.abs(offsetMinutes) % 60)}`
  return `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`
    + ` ${pad2(now.getHours())}:${pad2(now.getMinutes())}:${pad2(now.getSeconds())}（${offset}）`
}

/** 一次清理的结论：目录名（`logs` / `tasks` / `photos`）→ 本轮删掉的个数。 */
export interface CleanupOutcome {
  /** 目录名 → 本轮删掉的个数。 */
  readonly removed: Record<string, number>
}

/**
 * 旁路跑一次 runtime_files 的体积清理，真删了东西才记一行日志；**永不抛**。
 *
 * 跑的是 `python <script> --runtime-sweep --json`（脚本见 `Config.cleanupScript`）。判据全在
 * 脚本那一侧（两档水位在 configs/configs.py，哪些文件可删在 Observer/cleanup.py），本函数只
 * 负责起进程、读结论、写日志——插件里不复制任何一条清理规则。
 *
 * **没删东西是常态**（目录没到触发线），那时完全安静。起不来（解释器/脚本不在）、非零退出、
 * 输出不是 JSON 各打一行告警并返回 `undefined`，下一轮照常重试：清理是旁路，一次失败不许
 * 影响本轮观察，所以它不抛、调用点不需要 `.catch`。
 * @param options - 解释器可执行文件与脚本绝对路径（即插件 config 的那两个字段）。
 * @returns 各目录删掉的个数；任何失败都返回 `undefined`。
 */
export async function runCleanup(
  options: { python: string, script: string },
): Promise<CleanupOutcome | undefined> {
  try {
    const result = await new Promise<{ code: number | null, stdout: string, stderr: string }>(settle => {
      const child = spawn(options.python, [options.script, '--runtime-sweep', '--json'], {
        stdio: ['ignore', 'pipe', 'pipe'],
      })
      let stdout = ''
      let stderr = ''
      child.stdout.on('data', (chunk: Buffer) => { stdout += chunk.toString('utf8') })
      child.stderr.on('data', (chunk: Buffer) => { stderr += chunk.toString('utf8') })
      // 起不来（解释器不在之类）走 'error'：它先于 'close' 到达，message 就是失败原因。
      child.on('error', (error: Error) => { settle({ code: null, stdout: '', stderr: error.message }) })
      child.on('close', (code: number | null) => { settle({ code, stdout, stderr }) })
    })
    if (result.code !== 0) {
      console.error(`[${PLUGIN_SOURCE}] runtime_files 清理没跑完（${options.python} 退出码 `
        + `${result.code}，不影响本轮观察）：${firstLine(result.stderr)}`)
      return undefined
    }
    const removed = parseCleanupReport(result.stdout)
    if (removed === undefined) {
      console.error(`[${PLUGIN_SOURCE}] runtime_files 清理的输出解析不了（不影响本轮观察）：`
        + firstLine(result.stdout))
      return undefined
    }
    const total = Object.values(removed).reduce((sum, count) => sum + count, 0)
    if (total > 0) {
      const detail = Object.entries(removed).map(([label, count]) => `${label} ${count}`).join(' / ')
      console.error(`[${PLUGIN_SOURCE}] runtime_files 清理：删了 ${detail}`)
    }
    return { removed }
  } catch (error: unknown) {
    // 兜住这条链上的意外（spawn 参数非法之类）。本函数是旁路副作用：不许让任何异常逃到
    // tick() 的 try 里去顶掉一轮观察。
    console.error(`[${PLUGIN_SOURCE}] runtime_files 清理出异常（不影响本轮观察）：`, error)
    return undefined
  }
}

/** 一段诊断文字压成一行：取第一段非空行，免得把整份 stderr 灌进日志。 */
function firstLine(text: string): string {
  const line = text.split('\n').find(part => part.trim() !== '')
  return line === undefined ? '' : line.trim()
}

/**
 * 解析 `--json` 的输出：`{ dirs: { <目录名>: { removed: [...] } } }` → 各目录删了几个。
 *
 * 这是**进程边界**（另一种语言写的脚本的输出），所以逐层校验、形状不对就返回 `undefined`
 * 交给调用方报「解析不了」，而不是按类型断言直接用。
 * @param stdout - 脚本的整份标准输出。
 * @returns 目录名 → 删掉的个数；输出不是这个形状时返回 `undefined`。
 */
export function parseCleanupReport(stdout: string): Record<string, number> | undefined {
  let payload: unknown
  try {
    payload = JSON.parse(stdout)
  } catch {
    // 只吞 JSON.parse 那一下：非 JSON 的输出（脚本崩在半路的 traceback 之类）由调用方报出来。
    return undefined
  }
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) return undefined
  const dirs = (payload as { dirs?: unknown }).dirs
  if (typeof dirs !== 'object' || dirs === null || Array.isArray(dirs)) return undefined
  const removed: Record<string, number> = {}
  for (const [label, entry] of Object.entries(dirs)) {
    if (typeof entry !== 'object' || entry === null || Array.isArray(entry)) return undefined
    const list = (entry as { removed?: unknown }).removed
    if (!Array.isArray(list)) return undefined
    removed[label] = list.length
  }
  return removed
}

/**
 * 装配定时观察：建出本角色 agent，按 `intervalMs` 每轮读一遍观测源并交给它判断。
 *
 * 定时器在 `ctx.inject` 的服务就绪后起（`agents` 不在时连 agent 都建不出来），
 * agent 建立与首轮之间隔一个完整间隔——启动本身不产生模型请求。首个**被处理**的观察还要
 * 等过 `firstTickDelayMs`（见模块注释第 2 条），这段窗口内的跳一律丢掉。
 *
 * 建立失败只记一次（日志），之后每轮静默跳过：那时失败原因已经在日志里，
 * 能做的只有把它留在那儿。
 * @param ctx - registrant context。
 * @param config - 校验并落默认值后的插件配置。
 * @throws intervalMs / maxSessionBytes 不为正整数、firstTickDelayMs 不为非负整数、
 * 或观测源为空（都是配置错误，装配时抛，不留到运行期）。
 */
export function apply(ctx: Context, config: Config): void {
  if (!(Number.isInteger(config.intervalMs) && config.intervalMs > 0)) {
    throw new Error(`supervisor-loop: intervalMs 必须是正整数毫秒，收到 ${config.intervalMs}`)
  }
  if (!(Number.isInteger(config.firstTickDelayMs) && config.firstTickDelayMs >= 0)) {
    throw new Error('supervisor-loop: firstTickDelayMs 必须是非负整数毫秒（0 = 不延迟），'
      + `收到 ${config.firstTickDelayMs}`)
  }
  if (!(Number.isInteger(config.maxSessionBytes) && config.maxSessionBytes > 0)) {
    throw new Error(`supervisor-loop: maxSessionBytes 必须是正整数（会话日志字节数），收到 ${config.maxSessionBytes}`)
  }
  if (config.sources.length === 0) {
    throw new Error('supervisor-loop: sources 至少要有一个观测源（否则每轮没有任何内容可判断）')
  }
  if (config.cleanupScript === '') {
    throw new Error('supervisor-loop: cleanupScript 不能为空（该给 Observer/cleanup.py 的绝对路径，'
      + '来自 configs/paths.py 的 RUNTIME_CLEANUP_SCRIPT）')
  }
  // 路径由 paths.py 推导，取不到只可能是仓库/过桥坏了——那要在这儿就炸，而不是等每轮静默失败。
  if (!existsSync(config.cleanupScript)) {
    throw new Error(`supervisor-loop: cleanupScript 指向的文件不存在：${config.cleanupScript}`
      + ' —— 查 configs/paths.py 的 RUNTIME_CLEANUP_SCRIPT 与 scripts/role-daemon.sh 的过桥')
  }
  // 路由覆盖归一化：未配 / 只配了空白都当「不覆盖」（与 rolebus 的 resident 同一处理）。
  const modelProvider = config.modelProvider?.trim() ?? ''
  const sessionId = brandString<SessionId>(config.sessionId)
  const label = intervalLabel(config.intervalMs)
  // 首轮的门槛时刻：**装配时刻**（不是 agent 建好那一刻）起算。agent 建立通常只要几十毫秒，
  // 但建失败时会一直被重试——那时这个门槛早已过去，恢复后的第一跳就该照常跑，不必再等。
  const firstTickReadyAt = Date.now() + config.firstTickDelayMs

  ctx.inject(['agents', 'agentDefaultModel', 'sessionPersistence'], injected => {
    let disposed = false
    /** 当前持有的那个 agent。轮转会把指针换到新会话上，所以 tick 一律看它、不看 startup。 */
    let current: LoopStartup | undefined
    /** 整轮互斥：`status` 只挡「模型在跑」，挡不住 stat 未回、create/dispose 的窗口。 */
    let ticking = false

    const startup: Promise<LoopStartup | undefined> = openLoopAgent(injected, sessionId, modelProvider)
      .then((opened) => {
        // 卸载发生在建立完成之前：agent 已经建出来了，必须由这次建立自己拆掉。
        if (disposed) {
          void opened.dispose()
          return undefined
        }
        current = opened
        return opened
      })
      .catch((error: unknown) => {
        console.error(`[${PLUGIN_SOURCE}] 监督器 agent 建立失败（会话 ${JSON.stringify(config.sessionId)}），`
          + '此后每轮观察都不会被处理：', error)
        return undefined
      })

    /**
     * 把当前会话轮转成一份新的：建新会话 → 换指针 → 拆旧会话。新会话是空的，不带上一份的
     * 任何记忆（见模块注释第 4 条）。
     *
     * **先建成新的再拆旧的**，与「先 dispose 再 create」的直觉相反，是刻意的：`startup` 是
     * 一次性 promise，没有第二条建 agent 的路径，create 失败若发生在 dispose 之后，插件就
     * 再没有任何 agent，此后每轮只能记日志静默跳过，直到进程重启。反过来的代价只是两个 idle
     * agent 同时活几十毫秒——会话 id 不同，写租约不冲突。
     *
     * create 失败则原样抛出，旧会话继续用，下一轮再试。
     */
    const rotate = async (): Promise<void> => {
      const outgoing = current
      // 复用别人的 agent 时不许轮转：拆不掉它，另建一个只会变成两个监督器同时观察、重复告警。
      if (outgoing === undefined || !outgoing.owned) return
      const outgoingId = outgoing.agent.session.id
      const sizeBytes = (await injected.sessionPersistence.stat(outgoingId))?.sizeBytes
      const created = await createRotationAgent(injected, config.sessionId, modelProvider)
      // 卸载发生在轮转中途：新 agent 已经建出来了，由这里自己拆（同 startup 分支的处理）。
      if (disposed) {
        void created.dispose()
        return
      }
      current = created
      try {
        await outgoing.dispose()
      } catch (error) {
        console.error(`[${PLUGIN_SOURCE}] 旧会话拆除失败（新会话已在跑，不再重试）:`, error)
      }
      const size = sizeBytes === undefined ? '大小未知' : `${(sizeBytes / 1024 / 1024).toFixed(1)} MB`
      console.error(`[${PLUGIN_SOURCE}] 会话已轮转：${outgoingId}（${size}）→ ${created.agent.session.id}`
        + '（新会话从零判断，不带上一份的记忆）')
    }

    const tick = async (): Promise<void> => {
      if (ticking) return
      // 首轮门槛：还没到就丢这一跳（与「上一轮没跑完」同款，不打日志）。放在 ticking 之前，
      // 免得把 ticking 白占一瞬。
      if (Date.now() < firstTickReadyAt) return
      ticking = true
      try {
        // 每轮顺手清一次 runtime_files/（模块注释第 5 条）：旁路、不 await、不改本轮的观测输入。
        // 摆在 startup 之前是为了**模型侧建不起来时清理照跑**——目录长胖跟模型在不在没关系。
        void runCleanup({ python: config.python, script: config.cleanupScript })
        // 建立还没完成时排在建立 promise 的 .then 队列里，保持 FIFO。
        await startup
        const active = current
        if (active === undefined) return
        // 上一轮还在跑就丢掉这一跳（连同这一跳读到的观测），不打日志。
        if (active.agent.status !== 'idle') return
        // 到量就轮转，本轮不再观察：观测是采样不是队列，这一轮的读数会被下一轮的新读数取代。
        // sizeBytes 缺省（刚 create、还没物化）一律不轮转——那是「还不知道」，不是「是 0」。
        if (active.owned) {
          const snapshot = await injected.sessionPersistence.stat(active.agent.session.id)
          if (snapshot?.sizeBytes !== undefined && snapshot.sizeBytes >= config.maxSessionBytes) {
            await rotate()
            return
          }
        }
        const sources = await Promise.all(config.sources.map(source => readSource(source, process.cwd())))
        const header = `${localNow()}（间隔 ${label}）`
        active.agent.followup(observeMessage(renderObservation(sources, header)))
      } finally {
        ticking = false
      }
    }

    injected.effect(() => {
      const timer = setInterval(() => {
        tick().catch((error: unknown) => {
          console.error(`[${PLUGIN_SOURCE}] 本轮观察失败，该轮无人处理：`, error)
        })
      }, config.intervalMs)
      return async () => {
        disposed = true
        clearInterval(timer)
        // 建立尚未完成时由 startup 分支自己拆，轮转中途由 rotate 自己拆（见上）；这里只管
        // 已经到手的那个。
        if (current !== undefined) await current.dispose()
      }
    }, `${PLUGIN_SOURCE}.tick()`)
  })
}
