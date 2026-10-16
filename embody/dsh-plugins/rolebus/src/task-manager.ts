/**
 * 待做任务清单（task board）的写盘通道：把一次 `items` 改动交给清单的**唯一写者**
 * `embody/plugin_tools/task_manager.py`。
 *
 * 清单是交互器与编排器共享的一份文件（`embody/runtime_files/tasks/pending_tasks.yaml`，
 * 语义见 `embody/plugin_tools/task_board.py`）。两个角色手里都没有写工具：改动装在事件
 * （`task_update_int` / `task_update_orc`）的 `items` 参数里，发件工具在**投递之前**调本模块
 * 落盘（见 `event-catalog.ts` 的 `RoleBusEventSpec.writesTaskBoard`）——收件方被唤醒时读到的
 * 清单因此一定已含这次改动。互斥归 task_manager 里的 flock（阻塞等待），这里不管。
 *
 * 与 `@embody/dsh-tools-bridge` 的 Python 调用同形：`spawn(python, ['-m', …])`、stdout 一次
 * `JSON.parse`、子进程 stderr 原样透传。桥是**进程**通道，按「validate at process boundary」
 * 处理输出：协议层失败（起不来 / 非零退出 / stdout 不是 JSON）与工具自身报错
 * （`{"error": ...}`，task_manager 以 0 退出）都抛异常，交给引擎记为一次失败的调用。
 * @module @embody/dsh-rolebus/task-manager
 */

import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

/**
 * 仓库根（= 本包的 `embody/`）。本包位于 `<embody>/dsh-plugins/rolebus/{src,lib}/`，
 * 源码装载与 lib 装载上溯三级都一样，因此它是从模块位置推出的常量，不是配置项
 * （与 `configs/paths.py` 的 `ROOT_DIR`、tools-bridge 的同名常量一致）。子进程以它为工作目录。
 */
const REPO_ROOT = fileURLToPath(new URL('../../../', import.meta.url))

/** Python 侧写者的模块名；`python -m` 的取值，与 plugin_tools/task_manager.py 的 prog 对应。 */
const TASK_MANAGER_MODULE = 'plugin_tools.task_manager'

/** 子进程调用的部署选择。 */
export interface TaskManagerOptions {
  /** Python 解释器可执行文件；写者依赖 PyYAML（见 plugin_tools/requirements.txt）。 */
  readonly python: string
  /**
   * 发出这次改动的角色，取值同目录条目的 `source`。子进程按它判写入权限
   * （`configs/agents.py` 的 `ROLE_TASK_BOARD_LIMITS`：编排器不能新建任务；已有任务只有它的
   * owner 能改，见 `task_board._reject_owner_write`），也写进诊断行。
   */
  readonly source: string
  /** 这次改动挂在哪个事件类型上；只写进子进程的诊断行。 */
  readonly eventType: string
}

/** 一次落盘的请求：任务号（省略即新建）与要写入的字段改动。 */
export interface TaskBoardRequest {
  /**
   * 目标任务号；省略、`undefined` 或空串都表示新建，由 task_manager 按时间戳发号——发出方
   * 角色不许新建时（编排器）整次落盘被拒。
   */
  readonly task_id?: string | undefined
  /** 点分字段路径数组（`[{"field": …, "value": …}, …]`），形状与 task_board.write 的 data 同款。 */
  readonly items: unknown
}

/** 落盘回执：最终任务号（新建时是系统发的）、一句给模型看的确认，以及这次改动有没有真的改变内容。 */
export interface TaskBoardReceipt {
  /** 这次改动落进的那条任务号。 */
  readonly task_id: string
  /** 写盘确认（含 task_id / status / owner / last_update_time），作为工具结果交给模型。 */
  readonly summary: string
  /**
   * 这次改动有没有真的改变清单内容：`false` 表示要写的值本来就是文件里的值，清单**一字未动**
   * （连时间戳都没动）。调用方据此不投递事件——没有值得收件方醒的事。
   */
  readonly changed: boolean
}

/** 判断一个解析出的值是否是字符串键对象。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * 把写者的 stdout 解析成回执。
 * task_manager 的协议是「stdout 恰好一个 JSON 值」：成功 `{"result": {…}}`，失败
 * `{"error": "<异常文本>"}` 且退出码 0（工具自身报错不是通道故障）。两个分支在这里分开抛错。
 * @param stdout - 写者的完整 stdout。
 * @param options - 本次调用的角色与事件类型，用于出错信息。
 * @returns 落盘回执（含这次改动是否真的改变了内容）。
 * @throws stdout 不是合法 JSON、不是约定的对象、或写者报错（`{"error": …}`，越权也在这一支）。
 */
function receiptFrom(stdout: string, options: TaskManagerOptions): TaskBoardReceipt {
  const what = `${options.source} → ${options.eventType} 落盘`
  let parsed: unknown
  try {
    parsed = JSON.parse(stdout)
  } catch (error) {
    throw new Error(`rolebus: ${what}的 stdout 不是 JSON（${(error as Error).message}）：${JSON.stringify(stdout.slice(0, 500))}`)
  }
  if (!isRecord(parsed)) {
    throw new Error(`rolebus: ${what}的返回不是对象：${JSON.stringify(parsed).slice(0, 200)}`)
  }
  // 清单改动的下层校验（字段路径、取值词表、终结任务等）全归 task_board，这里只转达它的话。
  if (typeof parsed.error === 'string') throw new Error(parsed.error)
  const result = parsed.result
  if (!isRecord(result) || typeof result.task_id !== 'string' || typeof result.summary !== 'string'
    || typeof result.changed !== 'boolean') {
    throw new Error(`rolebus: ${what}的返回既无 error 也无 result.task_id/summary/changed：${JSON.stringify(parsed).slice(0, 200)}`)
  }
  return { task_id: result.task_id, summary: result.summary, changed: result.changed }
}

/**
 * 把一次 `items` 改动落进待做任务清单，等写者结束。
 * @param options - 解释器与诊断用的角色/事件类型。
 * @param request - 任务号（省略即新建）与字段改动数组。
 * @param signal - 调用方取消时用来终止子进程；省略表示不可取消。
 * @returns 落盘回执（含最终任务号）。
 * @throws 子进程起不来、退出码非零、stdout 不是约定 JSON、写者报错（`{"error": …}`），或调用被取消。
 */
export function writeTaskBoard(
  options: TaskManagerOptions,
  request: TaskBoardRequest,
  signal?: AbortSignal,
): Promise<TaskBoardReceipt> {
  const stdin = JSON.stringify({
    // 空串与省略同义（都表示「没有号」）：task_manager 按时间戳发一个新号。
    task_id: request.task_id === undefined || request.task_id === '' ? null : request.task_id,
    items: request.items,
    source: options.source,
    event_type: options.eventType,
  })
  return new Promise((resolve, reject) => {
    const child = spawn(
      options.python,
      ['-m', TASK_MANAGER_MODULE, '--apply'],
      { cwd: REPO_ROOT, stdio: ['pipe', 'pipe', 'pipe'] },
    )
    const stdout: Buffer[] = []
    const stderr: Buffer[] = []
    child.stdout.on('data', (chunk: Buffer) => stdout.push(chunk))
    // 写者执行期间把 stdout 借给协议，它自己的诊断行（包括 task_board 的 print）都在 stderr 上；
    // 那些行仍是有用的运行信息，收集之外原样透传，随 resident 进程的日志落盘。
    child.stderr.on('data', (chunk: Buffer) => { stderr.push(chunk); process.stderr.write(chunk) })

    const onAbort = (): void => { child.kill() }
    if (signal !== undefined) {
      if (signal.aborted) onAbort()
      else signal.addEventListener('abort', onAbort, { once: true })
    }

    child.on('error', error => {
      signal?.removeEventListener('abort', onAbort)
      reject(new Error(`rolebus: 起不了 ${options.python} -m ${TASK_MANAGER_MODULE}：${error.message}`))
    })
    child.on('close', code => {
      signal?.removeEventListener('abort', onAbort)
      if (signal?.aborted === true) {
        // 取消不是工具失败：单独的措辞，免得模型把中断当成真实的执行结果。
        reject(new Error(`rolebus: ${options.source} → ${options.eventType} 落盘被取消`))
        return
      }
      const out = Buffer.concat(stdout).toString('utf8')
      if ((code ?? -1) !== 0) {
        reject(new Error(`rolebus: ${options.source} → ${options.eventType} 落盘失败（退出码 ${code}）：`
          + `${Buffer.concat(stderr).toString('utf8').trim() || out.trim()}`))
        return
      }
      try {
        resolve(receiptFrom(out, options))
      } catch (error) {
        reject(error as Error)
      }
    })

    child.stdin.end(stdin)
  })
}
