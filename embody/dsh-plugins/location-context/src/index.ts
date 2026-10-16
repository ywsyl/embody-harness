/**
 * @embody/dsh-location-context —— 把【支持地点清单】每轮注入交互器与监督器的模型输入。
 *
 * 清单的事实源是 configs/configs.py 的 `load_supported_locations()`（数据在
 * skills/waypoint_nav/app/data/landmarks.json）。本插件不认识那份 JSON、也不认识 `name` 字段：
 * 它拿到的就是**已经渲染好的正文**（每行一条 `- 名字`），渲染在
 * configs/configs.py 的 `supported_locations_text()` 里，名字的抽取口径仍只有
 * `load_supported_locations()` 一处。
 *
 * 为什么这两个角色需要它：清单进模型上下文的另一条路是 plugin_tools/bridge.py 的
 * `_inject_supported_targets()`——它把清单拼进 `waypoint_nav` 的**工具说明与 target 参数说明**，
 * 只有手里有那个工具、且读到工具 schema 时才看得到。交互器与监督器的工具白名单里没有
 * waypoint_nav（configs/agents.py），却都要按这份清单判断：交互器按 prompts/info_policy.md 检查
 * 「任务地点在不在【支持地点清单】里、要不要补全名字」，监督器判一条任务里的地点是否合法。
 *
 * 注入点：`agent/pre-step`（引擎的 request-preparation 瀑布），与 ../task-context 及引擎的
 * time-context / tmux-context / session-reference 同一手法——`{ prepend: true }` 注册，在**每个
 * 轮次的第一步**把一条 plugin 来源的 user 消息插在下游消息之前（交互器由 rolebus 事件唤醒、
 * 监督器由 ../supervisor-loop 的定时观察唤醒，所以排在前面的就是这份清单，后面才是把它们叫起来
 * 的那条消息）。四条判据见 {@link apply}，都是「不注入」的边界：
 *   · 只在 `step === 1` 注入 —— 每轮一份，而不是每步一份；
 *   · 下游把这一步清空时不动 —— 注入等于把「这一轮没有任何事要处理」撑成一次模型调用；
 *   · subagent 会话不注入 —— 它不是角色自己的一轮。
 *
 * 值是**角色启动那一刻的快照**，不是每轮现读文件：正文经 scripts/role-daemon.sh 过桥进进程环境
 * （见那里的 interactor / supervisor 两支），profile 层用 `!!js process.env.…` 取回，装载时求值一次。
 * 这与 waypoint_nav 工具说明那条路（bridge.py 在装载时注入）**同一种新鲜度**是刻意的：两条路不同步
 * 就会出现「注入的清单里有这个地点、调 waypoint_nav 却被硬拒（不在支持范围内）」的窗口期。
 * 代价：landmarks.json 改了要重起角色才可见——与那条工具说明一样。
 *
 * 空正文（读不到 landmarks.json：文件不在、JSON 坏了、根不是列表）**不抛错**：那是运行时状态而不是
 * 配置错误，装载期的 `missing required value` 留给「环境变量没设置」。此时注入的是一句说明，
 * 不能让「读不到」静默表现成「没有可去的地方」（同 ../task-context 对空清单的处理）。
 *
 * 每轮注入的代价是会话线性增长（今天三条地点，几十字节/轮；交互器所在 profile 有 compaction，
 * 监督器有自己的 30k 上限）。**不在这里加去重**：口径是「每轮必注入」，要收上下文就在 profile 层
 * 调压缩。
 *
 * 依赖解析：引擎包未发布、本 checkout 未构建 lib，因此本包**不声明引擎依赖**（否则 profile
 * `pnpm add` 会在 DSH_HOME 内解析失败）；运行时在 tsx 源码启动的 dsh（fork 根的开发形态，
 * `@deepseek-ai/*` 经 tsconfig paths 指向 src）内解析。与 ../task-context 同形。
 *
 * 遵循 dsh 引擎函数插件约定：命名导出 name / Config / apply，无默认导出（默认导出会被 Loader
 * 丢弃命名导出元信息）。Config 用 schemastery 校验、**不设默认值**：清单正文是部署值，由 profile
 * 层给全（漏了在装载时报 missing required value）。
 * @module @embody/dsh-location-context
 */

import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
// 既用 PreStepDecision，也借这次导入带进 `agent/pre-step` 在 Context 事件表里的声明
// （引擎惯例：用到哪条事件就导入声明它的包的类型）。
import type { PreStepDecision } from '@deepseek-ai/dsh-agent'
import { createUserMessage } from '@deepseek-ai/dsh-llm'

export const name = 'location-context'

/** 注入消息在会话里的 `source.plugin`。 */
export const PLUGIN_SOURCE = name

/** 注入文本的第一行。**这个名字同时被人设引用**（Interactor/prompts/info_policy.md 的
 * 【支持地点清单】），两边是同一件事，改这里要连着那份提示词一起改。 */
const HEADING = '【支持地点清单】'

/** 正文为空（读不到 landmarks.json）时代替清单的一句说明——不能让读不到看起来像没有地方可去。 */
const NO_LIST = '（没读到支持地点清单：skills/waypoint_nav/app/data/landmarks.json 缺失或格式不对，'
  + '见 configs/configs.py 的 load_supported_locations）'

/** 本插件的部署选择。只有一项——清单正文随部署变，由 profile 层给全。 */
export interface Config {
  /**
   * 已渲染的清单正文，每行一条 `- 名字`（configs/configs.py 的 `supported_locations_text()`）。
   * 值经 scripts/role-daemon.sh 过桥进进程环境，profile 层用 `!!js process.env.…` 取回。
   */
  list: string
}

/** Schemastery 配置：required 在此声明，应用层不再做隐藏默认。 */
export const Config: z<Config> = z.object({
  list: z.string().required(),
})

/**
 * 把清单正文渲染成注入文本（抬头 + 空行 + 正文）。
 *
 * 正文是空串或纯空白时换成 {@link NO_LIST}：读不到清单与「清单里一个地点都没有」在 Python 侧
 * 是同一种结果（`load_supported_locations()` 两个都返回空元组），所以这一句把两种可能都说出来。
 * @param list - profile 层过桥进来的清单正文。
 * @returns 注入到每轮开头的文本。
 */
export function renderList(list: string): string {
  const body = list.trim() === '' ? NO_LIST : list
  return `${HEADING}\n\n${body}`
}

/**
 * 装配每轮注入：注册一个前置的 `agent/pre-step` 监听器，在每一轮的第一步插一条清单。
 *
 * 注册用 `{ prepend: true }`：本插件在 profile 层装载（最后加载），因此是**最外层**的前置监听器，
 * 看到的是下游全部组装完的 decision。返回 `{ ...decision, messages: [注入, ...messages] }` ——
 * 不动下游给的消息，只在前面加一条。
 * @param ctx - registrant context；监听器随它一起卸载。
 * @param config - 校验后的插件配置。
 */
export function apply(ctx: Context, config: Config): void {
  const text = renderList(config.list)

  ctx.on('agent/pre-step', async ({ agent, step, signal }, next): Promise<PreStepDecision> => {
    // 先委托：不调 next() 就是短路整条瀑布，下游（模型路由、直接消息准备等）全被跳过。
    const decision = await next()
    // 后两条判据与引擎 model-selection 的同名监听器一致：下游可能拒掉这一步，也可能把它清空。
    if (decision.kind === 'reject' || signal.aborted) return decision
    if (step !== 1) return decision
    if (decision.messages.length === 0) return decision
    // 子会话（subagent）不是角色自己的一轮，别把角色清单塞进它的上下文。
    if (agent.session.header.origin === 'subagent') return decision

    return {
      ...decision,
      messages: [
        createUserMessage({
          content: [{ type: 'text', text }],
          // snapshot：这是「当前状态」，同一生产者后一轮的那份取代前一轮的那份（引擎的
          // ContextForm 语义），故带上 sections 归属。
          source: { kind: 'plugin', plugin: PLUGIN_SOURCE, form: 'snapshot', sections: [{ name: PLUGIN_SOURCE, text }] },
        }),
        ...decision.messages,
      ],
    }
  }, { prepend: true })
}
