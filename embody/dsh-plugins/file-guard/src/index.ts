/**
 * @embody/dsh-file-guard —— 角色的**文件围栏**：交互器 / 编排器 / 监督器 / planner 只许读写
 * runtime_files（与本机临时目录）里的文件，其余一律拒。
 *
 * 用户定的规矩（2026-10-13）：这四个角色**不许用命令行类工具**读写本项目根目录下的文件，
 * 唯一例外是 runtime_files —— 也就是「机器人自己的数据区」可以进出，**harness 本体**
 * （代码、人设、configs、install/secret.env…）一律不可读写。清单里的工具（
 * plugin_tools/available_tools.yaml，经 @embody/dsh-tools-bridge 注册）不在这条规矩里：
 * 它们是角色的正业（robot_state / task_list / weather…），各自按自己的需要碰文件。
 *
 * 判定的两类与为什么分两类，见 ./guard.ts 的头部注释。这里只讲接线与边界：
 *
 * **第一道在 profile 层的 disabled 行**：`tool-bash` / `tool-pwsh` 在四个角色的
 * <Role>/cordis.patch.yml 里都是 `disabled: true`，模型那一轮里根本没有这两个工具（也不
 * 占工具位、不占 token）。本插件是**第二道**：`bash` / `pwsh` 的名字在这里再拒一次。两道
 * 都要，因为两道挡的不是同一件事——profile 那一行是「这个角色没有命令行工具」（可调用面的
 * 裁剪），这里这一句是「就算有人把那一行重新打开，也不许用」（围栏本身的判据）。改其中一道
 * 别忘了另一道。
 *
 * **判定挂在 `tools/pre-execute`**：引擎在每次派发前的瀑布，返回 `{ kind: 'deny', reason }`
 * 就是「这次没跑、结果是 Error: <reason>」——拒绝走的是普通工具结果那条路（模型看得见、
 * 会话日志里有），不抛异常、不静默丢弃。这条瀑布对 **native 与 PTC 两种呈现模式都生效**：
 * 编排器 / planner 是 PTC，模型写的程序里每一次 `await tools.<名>(…)` 都是一次子派发，同样
 * 过这里（引擎 pts.ts 的调度器与 native 共用 prepare 阶段）。
 *
 * **管不到的地方（写在这里，不装作没有）**：PTC 的 run_code 里模型写的是**任意 TypeScript**，
 * 它可以自己 `await import('node:fs')` 去读文件——引擎明说 worker-thread 代码运行时是
 * 「containment, not a security boundary」（README 的 Trust posture：模型代码按 bash 等价
 * 信任）。所以本插件保证的是**工具面**：模型经工具调用的读写全在围栏内；要连模型代码一起挡住，
 * 得换一个容器级后端，不是这一层能给的。
 *
 * 允许范围从 profile 层进来（`allowedRoot`），值经 scripts/role-daemon.sh 过桥
 * （configs/paths.py 的 RUNTIME_DIR）。本机临时目录由插件自己加上——与引擎沙箱的
 * `writableRoots` 同一口径（/tmp 与 os.tmpdir()），写临时文件不该被围栏挡下。
 *
 * 依赖解析：引擎包未发布、本 checkout 未构建 lib，因此本包**不声明引擎依赖**（否则 profile
 * `pnpm add` 会在 DSH_HOME 内解析失败）；运行时在 tsx 源码启动的 dsh（fork 根的开发形态，
 * `@deepseek-ai/*` 经 tsconfig paths 指向 src）内解析。与 ../location-context 同形。
 *
 * 遵循 dsh 引擎函数插件约定：命名导出 name / Config / apply，无默认导出（默认导出会被 Loader
 * 丢弃命名导出元信息）。Config 用 schemastery 校验、**不设默认值**：让哪些路径可读写是部署
 * 选择，由 profile 层给全（漏了在装载时报 missing required value，而不是退回「全都放行」）。
 * @module @embody/dsh-file-guard
 */

import { realpathSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
// 既用 PreToolDecision，也借这次导入带进 `tools/pre-execute` 在 Context 事件表里的声明
// （引擎惯例：用到哪条事件就导入声明它的包的类型）。
import type { PreToolDecision } from '@deepseek-ai/dsh-tools'
import { judge } from './guard.ts'

export const name = 'file-guard'

/** 本插件的部署选择。只有一项——项目内可读写的根随部署变，由 profile 层给全。 */
export interface Config {
  /**
   * 项目内允许读写的根，绝对路径（本部署是 configs/paths.py 的 RUNTIME_DIR =
   * <仓库根>/runtime_files）。**只有一个**：本部署的正当读写面就是这一棵目录，要放行第二棵
   * 就得先有那个需要。值经 scripts/role-daemon.sh 过桥进进程环境，profile 层用
   * `!!js process.env.…` 取回（Loader 的 `!!js` 只能挂标量，故是字符串不是数组）。
   */
  allowedRoot: string
}

/** Schemastery 配置：required 在此声明，应用层不再做隐藏默认。 */
export const Config: z<Config> = z.object({
  allowedRoot: z.string().required(),
})

/**
 * canonical 一个根：整条 realpath，解析不出来（根还不存在）就**原样返回**——同引擎
 * `dsh-sandbox` 的 canonicalPath：不存在的根匹配不到东西是刻意的，替它编一个兜底才是给了
 * 调用方没指名的路径。
 * @param path - 根的字面绝对路径。
 * @returns canonical 后的根。
 */
function canonicalRoot(path: string): string {
  try {
    return realpathSync.native(path)
  } catch {
    return path
  }
}

/**
 * 本机临时目录（/tmp 与 os.tmpdir()）——与引擎沙箱 `writableRoots` 同一口径：写临时文件是
 * 平台上「本来就该能写」的地方，围栏在这里不另立规矩。
 * @returns 已 canonical 的临时根。
 */
export function tempRoots(): string[] {
  const roots = process.platform === 'win32' ? [tmpdir()] : ['/tmp', tmpdir()]
  return [...new Set(roots.map(canonicalRoot))]
}

/**
 * 装配围栏：把配置里的根加本机临时根拼成允许范围，注册一个 `tools/pre-execute` 监听器，
 * 每次派发前按 ./guard.ts 的判据决定放行还是拒。
 *
 * 相对路径按**会话工作目录**解析（`exec.agent.session.header.cwd`），与引擎 fs 工具的
 * 口径一致；没有 agent 的调用（本部署不该出现）退回进程工作目录（= 引擎根），判出来同样是
 * 「不在允许范围内」。
 * @param ctx - registrant context；监听器随它一起卸载。
 * @param config - 校验后的插件配置。
 */
export function apply(ctx: Context, config: Config): void {
  if (config.allowedRoot.trim() === '') {
    // 空串 = 角色连 runtime_files 都读写不了，那是配置漏项（环境变量没设置 / 取了空）而不是
    // 「更严的策略」——装载当场失败，别把四个角色变成碰不了任何文件的空壳。
    throw new Error('file-guard: allowedRoot 是空的——没有它角色连 runtime_files 都读写不了（值由 role-daemon.sh 过桥，见 Config.allowedRoot）')
  }
  const allowedRoots = [canonicalRoot(resolve(config.allowedRoot)), ...tempRoots()]

  ctx.on('tools/pre-execute', async (exec, next): Promise<PreToolDecision> => {
    const cwd = exec.agent?.session.header.cwd ?? process.cwd()
    const reason = await judge(exec.name, exec.arguments, { cwd, allowedRoots })
    return reason === undefined ? next() : { kind: 'deny', reason }
  })
}
