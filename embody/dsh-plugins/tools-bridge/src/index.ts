/**
 * @embody/dsh-tools-bridge —— 自定义工具的注册方。
 *
 * 本项目里的机器人技能（`speak` / `listen` / `speak_listen` / `speak_input` / `action` /
 * `gait` / `move` / `vision` / `waypoint_nav` …）在 Python 侧实现，定义在
 * `plugin_tools/available_tools.yaml`，按角色分成白名单放在 `configs/agents.py` 的 `ROLE_TOOLS`。
 * dsh 的模型只认工具表里注册过的工具，因此需要有人把白名单搬进去 —— 就是本插件：
 *   - 装载时按本 profile 的 `role` 取一次白名单（`list`），逐条 `ctx.tools.register`；
 *     取不到（角色不在白名单表里、解释器起不来、清单为空）即启动失败，不会安静地少一批
 *     工具。
 *   - 每次调用经桥转发给 Python（`call <tool>`），工具的输出文本原样回到模型。
 *
 * 一个 profile 只拿到自己角色的那份白名单：切角色 = 换 profile 的 `role`，不是在本插件里
 * 再写一份子集。
 *
 * 依赖解析：引擎包未发布、本 checkout 未构建 lib，因此本包**不声明引擎依赖**（否则
 * profile `pnpm add` 会在 DSH_HOME 内解析失败）；运行时在 tsx 源码启动的 dsh（fork 根的
 * 开发形态，`@deepseek-ai/*` 经 tsconfig paths 指向 src）内解析。与 @embody/dsh-rolebus 同。
 *
 * 遵循 dsh 引擎函数插件约定：命名导出 name / inject / Config / apply，无默认导出
 * （默认导出会被 Loader 丢弃命名导出元信息）。Config 用 schemastery 校验并落默认值。
 * @module @embody/dsh-tools-bridge
 */

import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import { createToolDefinition, listTools, type BridgeOptions } from './bridge.ts'

export const name = 'tools-bridge'

/** 注册工具依赖 `tools` 服务；声明注入使 apply 延迟到该服务就绪（同 @embody/dsh-mcp-client）。 */
export const inject = ['tools']

/**
 * 本 profile 对注册方的部署选择。role 由每个角色 profile 的覆盖层赋予
 * （Interactor / Orchestrator / Planner 各一份）；bundle 默认是 role=unset 的中性值，
 * 未覆盖时本插件不注册任何工具。
 */
export interface Config {
  /** 本 profile 的角色身份，取值同 configs/agents.py 的 ROLE_TOOLS 键。 */
  role: string
  /** 跑 Python 桥的解释器可执行文件；桥依赖 PyYAML。 */
  python: string
}

/** Schemastery 配置：required + 默认值在此声明，应用层不再做隐藏默认。 */
export const Config: z<Config> = z.object({
  role: z.string().required(),
  python: z.string().default('python3'),
})

/** 角色身份尚未赋予（bundle 层的中性默认）——与 @embody/dsh-rolebus 同约定。 */
const UNSET_ROLE = 'unset'

/**
 * 挂载注册方：取本角色白名单并把每条工具注册进工具表。
 *
 * apply 显式声明为 async 函数声明（不是箭头函数/带原型的普通函数）：Cordis 会把带原型的
 * 普通函数当成构造函数，其返回的 Promise 不算启动工作，工具就会晚于首个模型请求注册。
 * 这里等 `list` 落地再返回，保证 fiber 激活时工具已经在表里（同 @deepseek-ai/dsh-mcp-client）。
 * @param ctx - registrant context，携带 tools 服务。
 * @param config - 校验并落默认值后的配置。
 * @throws 角色不在 ROLE_TOOLS、清单取不到、清单为空，或注册发生冲突（后注册的整批回滚）。
 */
export async function apply(ctx: Context, config: Config): Promise<void> {
  const role = config.role
  // 中性默认：profile 还没给角色身份就一个工具都不注册 —— 也就不会去起 Python 桥，
  // bundle 层单独被装载（未覆盖）时不产生任何副作用。
  if (role === UNSET_ROLE) return

  const options: BridgeOptions = { python: config.python, role }
  const descriptions = await listTools(options)
  if (descriptions.length === 0) {
    throw new Error(
      `tools-bridge: 角色 ${JSON.stringify(role)} 的白名单一条工具都没取到`
      + ' —— 查 configs/agents.py 的 ROLE_TOOLS 与 plugin_tools/available_tools.yaml',
    )
  }
  const definitions = descriptions.map(description => createToolDefinition(options, description))

  // 注册即 effect：整批全有或全无——部分失败时摘掉已注册的再抛，模型看到的要么是本角色
  // 完整的工具面，要么一个都没有（引擎 mcp-client 的批量注册同一写法）。
  ctx.effect(() => {
    const disposers: Array<() => void> = []
    try {
      for (const definition of definitions) disposers.push(ctx.tools.register(definition))
    } catch (error) {
      for (const dispose of disposers) dispose()
      throw error
    }
    return () => {
      for (const dispose of disposers) dispose()
    }
  }, 'tools-bridge.register')
}
