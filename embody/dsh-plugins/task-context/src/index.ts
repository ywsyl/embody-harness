/**
 * @embody/dsh-task-context —— 每轮把待做任务清单的**全文**注入编排器的模型输入。
 *
 * 待做任务清单是交互器与编排器共享的一份（plugin_tools/task_board.py，落
 * runtime_files/tasks/pending_tasks.yaml）。清单的**唯一写者**是 plugin_tools/task_manager.py，
 * 本角色要改就发 rolebus 的 task_update_orc 事件，字段改动写在 `items` 参数里——发件工具在
 * 投递之前先把它交给写者落盘，所以本角色改完这一封，收件方（以及下一轮注入的这份清单）读到
 * 的一定是新值。**读的方式**：编排器的白名单里没有 task_list 工具
 * （configs/agents.py 的 ORCHESTRATOR_TOOLS 注释掉了那一行），改由本插件把它摆在每一轮的开头。
 * 注入是**无条件**的：清单是这一轮判断的输入，不是「变了才通知」的增量。
 *
 * 注入点：`agent/pre-step`（引擎的 request-preparation 瀑布），与引擎的 time-context /
 * tmux-context / session-reference 同一手法——`{ prepend: true }` 注册，在**每个轮次的第一步**
 * 把一条 plugin 来源的 user 消息插在下游消息之前（编排器的一轮由 rolebus 事件唤醒，所以排在前
 * 面的就是这份清单，后面才是事件本身）。三条判据见 {@link apply}，都是「不注入」的边界：
 *   · 只在 `step === 1` 注入 —— 每轮一份，而不是每步一份（一轮里模型会走好几步、调好几次工具）；
 *   · 下游把这一步清空时不动 —— 引擎对「空的第一步」有跳过语义（判据同 model-selection 的同名
 *     监听器），我们注入等于把「这一轮没有任何事要处理」撑成一次模型调用；
 *   · subagent 会话不注入 —— 编排器保留了 base 的 subagent 工具，子会话是「干活的一轮」，
 *     不该背着一份与本轮无关的角色清单。
 *
 * **注的是整份原文，不筛**：清单是交互器与编排器共用的，里面既有归本角色的任务、也有归交互器的，
 * 状态从 pending 到 interrupted 都有。谁该做哪条由条目上的 `owner` 字段说了算，**挑选留给模型**，
 * 插件只把清单原样摆出来——筛过的视图会让「清单里还有哪些活」在编排器眼里只剩一半，而它做依赖
 * 分析、判断「这件事有没有人在跟」时需要的正是全貌。代价是每轮注入的 token 跟着整份清单走
 * （不再只是归它的那几条），这一层没有去重：要收上下文就在 profile 层补 compaction。
 *
 * **不解析 YAML**：文件按行照搬，连引号、多行 description、stepN 子块都不需要理解
 * （同 ../supervisor-loop 的约定）。取值口径不在这份文件里，由角色人设与工具 schema 交代；
 * 本插件也不在 TS 侧另维护一份词表。
 *
 * 读不到时按两类分开，**不一律当异常**：
 *   · 文件不存在或为空 → 「还没有任何待做任务」。这不是故障：清单要等第一次任务事件
 *     （rolebus_send_task_update_int 的 items）才被建出来，编排器完全可能在一个还没有任何任务的
 *     系统里被叫醒。报成「读取失败」会让它每轮误报一次；
 *   · 其他 IO 错误（权限、损坏的挂载…）→ 原文带上「读取失败：…」，那是要原样交给模型的事实，
 *     静默跳过会让它把「读不到」误当成「没有任务」。
 *
 * 依赖解析：引擎包未发布、本 checkout 未构建 lib，因此本包**不声明引擎依赖**（否则
 * profile `pnpm add` 会在 DSH_HOME 内解析失败）；运行时在 tsx 源码启动的 dsh（fork 根的开
 * 发形态，`@deepseek-ai/*` 经 tsconfig paths 指向 src）内解析。与 ../rolebus 同形。
 *
 * 遵循 dsh 引擎函数插件约定：命名导出 name / Config / apply，无默认导出（默认导出会被 Loader
 * 丢弃命名导出元信息）。Config 用 schemastery 校验、**不设默认值**：清单路径是部署选择，由
 * profile 层给全（漏了在装载时报 missing required value，而不是退回某个内置路径）。
 * @module @embody/dsh-task-context
 */

import { readFileSync } from 'node:fs'
import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
// 既用 PreStepDecision，也借这次导入带进 `agent/pre-step` 在 Context 事件表里的声明
// （引擎惯例：用到哪条事件就导入声明它的包的类型）。
import type { PreStepDecision } from '@deepseek-ai/dsh-agent'
import { createUserMessage } from '@deepseek-ai/dsh-llm'

export const name = 'task-context'

/** 注入消息在会话里的 `source.plugin`，同时用作日志前缀。 */
export const PLUGIN_SOURCE = name

/** 注入文本的第一行：这份输入是什么、谁给的、改它的唯一路子是哪条。 */
const HEADING = '本轮待做任务清单（系统每轮自动注入这份清单的**全文**：它是交互器与编排器共享的'
  + '同一份，两个角色的任务都在里面、状态各异，其中只有 owner 是你的那些是交给你的活；改动发'
  + ' rolebus_send_task_update_orc，字段改动写在 items 里——发件工具投递前先落盘）：'

/** 本插件的部署选择。只有一项——清单路径随部署变，由 profile 层给全。 */
export interface Config {
  /**
   * 待做任务清单 yaml 的路径（configs/paths.py 的 RUNTIME_PENDING_TASKS_FILE）。
   * 绝对路径：值经 scripts/role-daemon.sh 过桥进进程环境，profile 层用 `!!js process.env.…` 取回。
   */
  path: string
}

/** Schemastery 配置：required 在此声明，应用层不再做隐藏默认。 */
export const Config: z<Config> = z.object({
  path: z.string().required(),
})

/**
 * 读清单原文，配上抬头。
 *
 * 文件不存在或为空是「还没有任何待做任务」（正常状态，清单要等第一次任务事件才建出来），
 * 其余 IO 错误原样报出来——两者都不能静默跳过，见模块注释。
 * @param path - configs/paths.py 给出的清单路径。
 * @returns 抬头加清单原文；读不到时抬头加一句说明原因。
 */
export function readBoard(path: string): string {
  let text: string
  try {
    text = readFileSync(path, 'utf8')
  } catch (error: unknown) {
    const code = (error as NodeJS.ErrnoException).code
    if (code === 'ENOENT') return `${HEADING}\n\n（还没有任何待做任务：清单文件尚未建出 ${path}）`
    const reason = error instanceof Error ? error.message : String(error)
    return `${HEADING}\n\n（读取失败：${path}：${reason}）`
  }
  // 空文件只可能来自手工编辑（task_board 整份写入从不产生这种文件，它按空清单处理）。
  if (text.trim() === '') return `${HEADING}\n\n（还没有任何待做任务：清单文件是空的 ${path}）`
  return `${HEADING}\n\n${text.trimEnd()}`
}

/**
 * 装配每轮注入：注册一个前置的 `agent/pre-step` 监听器，在每一轮的第一步插一份清单原文。
 *
 * 注册用 `{ prepend: true }`：本插件在 profile 层装载（最后加载），因此是**最外层**的前置监听器，
 * 看到的是下游全部组装完的 decision（模型路由提示、直接消息准备等都在它之后跑完）。
 * 返回 `{ ...decision, messages: [注入, ...messages] }` —— 不动下游给的消息，只在前面加一条。
 * @param ctx - registrant context；监听器随它一起卸载。
 * @param config - 校验后的插件配置。
 * @throws path 是空串或纯空白（profile 层的 `!!js` 取到了空值）——空路径只会读成 ENOENT 被当作
 * 「还没有任何任务」，那是最糟的静默退化，故在装配时就报出来。
 */
export function apply(ctx: Context, config: Config): void {
  const path = config.path
  if (path.trim() === '') {
    throw new Error('task-context: path 不能是空串或纯空白'
      + '（清单路径是 configs/paths.py 的 RUNTIME_PENDING_TASKS_FILE，经 scripts/role-daemon.sh 过桥）')
  }

  ctx.on('agent/pre-step', async ({ agent, step, signal }, next): Promise<PreStepDecision> => {
    // 先委托：不调 next() 就是短路整条瀑布，下游（模型路由、直接消息准备等）全被跳过。
    const decision = await next()
    // 后两条判据与引擎 model-selection 的同名监听器一致：下游可能拒掉这一步，也可能把它清空。
    if (decision.kind === 'reject' || signal.aborted) return decision
    if (step !== 1) return decision
    if (decision.messages.length === 0) return decision
    // 子会话（subagent）不是角色自己的一轮，别把角色清单塞进它的上下文。
    if (agent.session.header.origin === 'subagent') return decision

    const text = readBoard(path)
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
