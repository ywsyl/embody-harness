/**
 * `@embody/dsh-file-guard` 的判定本体：一次工具调用该不该拒、拒的时候给模型看什么。
 *
 * 只依赖 node（路径与 realpath），**不 import 任何引擎包** —— 这份判定因此可以拿 tsx
 * 直接跑（不起引擎、不起模型）：同级 check.mts 就是那张表的自检，改表以后先跑它再重起角色。
 *
 * 判定分两类，判据不同：
 *   · **命令类**（{@link SHELL_TOOLS}）：入参是任意命令文本，「这次不会碰到文件」给不出
 *     证明，所以角色一律不许用 —— 命令行工具在 profile 层已经是 disabled 的（第一道），
 *     这里是第二道（把某一行重新打开也仍然被拒）。
 *   · **文件类**（{@link FILE_TOOLS}）：目标路径是参数里指名道姓的一个值，按它判。
 *     相对路径按**会话工作目录**解析（与引擎 fs 工具同一口径，见 tool-fs 的 session-cwd），
 *     `glob` / `grep` 不给 `path` 时以会话工作目录为搜索根 —— 那就是「引擎根」，同样按目标判。
 *
 * 允许范围 = profile 层传进来的 {@link GuardContext.allowedRoots}（本部署是
 * configs/paths.py 的 RUNTIME_DIR 加本机临时目录）。命中判据是 **canonical 之后**的包含关系：
 * 先按 fs-local 的 `resolveLocalTarget` 同法算出真身（整条路径 realpath，缺的部分退回
 * 「最近的存在祖先 realpath + 拼回缺失后缀」），再逐根做词法包含。这样 runtime_files 里
 * 指向别处的软链接不会成为出口，而「还不存在的目标文件」也判得出归属。
 *
 * **本文件只负责拒**：它不碰文件、不写日志、没有状态，判定结果就是一句给模型看的原因
 * （或 `undefined` = 放行）。@module @embody/dsh-file-guard/guard
 */

import { realpath } from 'node:fs/promises'
import { basename, dirname, join, resolve, sep } from 'node:path'

/** 命令类工具：命令文本判不出文件效果，角色一律不许用（拒文本见 {@link shellDenial}）。 */
const SHELL_TOOLS: ReadonlySet<string> = new Set(['bash', 'pwsh'])

/** 文件类工具的路径参数。`defaultsToCwd` = 不传该参数时以会话工作目录为根（`glob` / `grep`）。 */
const FILE_TOOLS: ReadonlyMap<string, { readonly key: string; readonly defaultsToCwd?: true }> = new Map([
  ['read', { key: 'file_path' }],
  ['read_image', { key: 'file_path' }],
  ['write', { key: 'file_path' }],
  ['edit', { key: 'file_path' }],
  ['glob', { key: 'path', defaultsToCwd: true }],
  ['grep', { key: 'path', defaultsToCwd: true }],
])

/** 一次判定需要的外部输入。 */
export interface GuardContext {
  /** 会话工作目录：相对路径按它解析（同引擎 fs 工具的口径）。 */
  readonly cwd: string
  /** 允许读写的根，绝对路径、已 canonical（含本机临时目录）。空数组 = 什么都不许。 */
  readonly allowedRoots: readonly string[]
}

/**
 * 命令类工具的拒绝文本。为什么不是「按路径判」写在这里：命令里出现的路径是**字符串**，
 * `cat $F`、`base64`、脚本文件都能把真身藏起来 —— 判不出来的东西只能整类拒。
 * @param tool - 工具名（`bash` / `pwsh`）。
 * @returns 给模型看的拒绝原因。
 */
export function shellDenial(tool: string): string {
  return `文件围栏：本角色不能执行命令（${tool} 已对本角色关闭）。`
}

/**
 * 文件类工具的拒绝文本：说清允许范围与这次的目标，不用模型去猜边界在哪。
 * @param displayPath - 已按会话工作目录解析成绝对路径的目标（未 canonical）。
 * @param allowedRoots - 允许读写的根，用于把范围写给模型看。
 * @returns 给模型看的拒绝原因。
 */
export function fileDenial(displayPath: string, allowedRoots: readonly string[]): string {
  return `文件围栏：本角色只许读写 ${allowedRoots.join('、')} 下的文件，"${displayPath}" 不在其中。`
}

/**
 * 把一次性路径解析成真身：整条 realpath；缺的部分退回「最近的存在祖先 realpath + 拼回缺失
 * 后缀」。与引擎 fs-local 的 `resolveLocalTarget` 同法（这里不重复它那份 TOCTOU 与 Windows
 * 细节，只取归属判定需要的部分），所以本围栏与 fs 工具认的是同一个位置。
 *
 * 任何 realpath 失败都往上一级走（含 ENOTDIR、权限错），一路走到文件系统根都算不出来时
 * 返回词法解析结果 —— 那种情况下包含关系按词法判，与 fs-local 的兜底同形。
 * @param displayPath - 已解析成绝对路径的目标。
 * @returns canonical 后的路径。
 */
export async function canonicalTarget(displayPath: string): Promise<string> {
  try {
    return await realpath(displayPath)
  } catch {
    // 目标还不存在（或某一段不可读）：realpath 最近的存在祖先，再把缺失的后缀接回去。
  }
  const missing = [basename(displayPath)]
  let ancestor = dirname(displayPath)
  for (;;) {
    try {
      return join(await realpath(ancestor), ...missing)
    } catch {
      const parent = dirname(ancestor)
      if (parent === ancestor) return displayPath
      missing.unshift(basename(ancestor))
      ancestor = parent
    }
  }
}

/** 目标是否为该根本身或其后代（词法包含，大小写按平台惯例）。 */
export function isUnder(target: string, root: string): boolean {
  if (!process.platform.startsWith('win')) return target === root || target.startsWith(root.endsWith(sep) ? root : root + sep)
  const lowerTarget = target.toLowerCase()
  const lowerRoot = root.toLowerCase()
  return lowerTarget === lowerRoot || lowerTarget.startsWith(lowerRoot.endsWith(sep) ? lowerRoot : lowerRoot + sep)
}

/** 从参数对象里取一个非空字符串；类型不对或没给就是 `undefined`（交给工具自己的 schema 报错）。 */
function stringArg(args: unknown, key: string): string | undefined {
  if (typeof args !== 'object' || args === null) return undefined
  const value = (args as Record<string, unknown>)[key]
  return typeof value === 'string' && value.trim() !== '' ? value : undefined
}

/**
 * 判定一次调用：拒就给出给模型看的原因，放行返回 `undefined`。
 *
 * **不在表里的工具一律放行**：白名单会把 rolebus_send_*、schedule_*、run_code 这些一起关掉，
 * 而它们不碰文件。代价是新增的引擎文件类工具必须在这张表里登记，否则不围 —— 表只有这一处。
 * 引擎里另有带路径参数的两个（`str_replace_editor` 的 `path`、tool-lsp 的 `file_path`），本部署
 * 的 bundle 都没挂（前者只在 @deepseek-ai/dsh-sdk-minimal 里），所以不登记；哪天挂上要一起登记。
 * @param tool - 工具名（`exec.name`）。
 * @param args - 模型给的原始参数（`exec.arguments`，此时还没过工具自己的 schema）。
 * @param context - 会话工作目录与允许的根。
 * @returns 拒绝原因，或 `undefined` 表示放行。
 */
export async function judge(tool: string, args: unknown, context: GuardContext): Promise<string | undefined> {
  if (SHELL_TOOLS.has(tool)) return shellDenial(tool)
  const spec = FILE_TOOLS.get(tool)
  if (spec === undefined) return undefined

  const raw = stringArg(args, spec.key)
  if (raw === undefined && spec.defaultsToCwd !== true) {
    // 参数缺失或类型不对：`read` / `write` / `edit` / `read_image` 的 schema 本来就要求它，
    // 让工具去报它自己那句参数错误，别在这里抢先编一个。
    return undefined
  }
  const target = resolve(context.cwd, raw ?? '.')
  const canonical = await canonicalTarget(target)
  for (const root of context.allowedRoots) {
    if (isUnder(canonical, root)) return undefined
  }
  return fileDenial(target, context.allowedRoots)
}
