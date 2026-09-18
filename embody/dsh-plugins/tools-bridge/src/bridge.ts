/**
 * 自定义工具的 Python 调用通道：取工具定义、执行一次调用。
 *
 * 工具「有哪些、参数长什么样」的事实源有两份，都在 Python 侧：
 * `embody/plugin_tools/available_tools.yaml`（定义）与 `embody/configs/agent_plugin.py`
 * 的 `ROLE_TOOLS`（角色白名单）。本模块不复制它们，只经 stdio 桥
 * `python -m plugin_tools.bridge` 取回与转发；桥的两条子命令与返回格式见
 * `embody/plugin_tools/bridge.py` 的模块文档。
 *
 * 桥是**进程**通道，按「validate at process boundary」处理它的输出：`list` 的每条
 * 描述、`call` 的结果都先判形状再交给上层。工具自身报错不是通道故障（桥以 0 退出、
 * 在 stdout 给 `{"error": ...}`），故在这里转成异常交给引擎的工具执行管线。
 *
 * `speak` / `listen` 一类的交互工具在子进程里读写控制终端（`/dev/tty`），不碰
 * stdin/stdout —— 那两个流归本通道的协议用；子进程继承调用方的控制终端，因此人机
 * 交互与工具调用可以同时发生。
 * @module @embody/dsh-tools-bridge/bridge
 */

import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { assertSupportedJsonSchema, type ToolDefinition, type ToolRunContext } from '@deepseek-ai/dsh-tools'

/**
 * 仓库根（= 本包的 `embody/`）。本包位于 `<embody>/dsh-plugins/tools-bridge/{src,lib}/`，
 * 源码装载与 lib 装载上溯三级都一样，因此它是从模块位置推出的常量，不是配置项
 * （与 `configs/paths.py` 的 `ROOT_DIR` 同一做法）。子进程以它为工作目录，
 * 桥自身也按模块位置定位仓库根。
 */
const REPO_ROOT = fileURLToPath(new URL('../../../', import.meta.url))

/** Python 桥模块名；`python -m` 的取值，与 plugin_tools/bridge.py 的 `prog` 一致。 */
const BRIDGE_MODULE = 'plugin_tools.bridge'

/** 子进程调用的部署选择。 */
export interface BridgeOptions {
  /** Python 解释器可执行文件；桥依赖 PyYAML（见 plugin_tools/requirements.txt）。 */
  readonly python: string
  /** 角色身份，取值同 `ROLE_TOOLS` 的键；桥用它选白名单。 */
  readonly role: string
}

/** 桥 `list` 返回的一条工具描述，字段取自 available_tools.yaml 的 `function` 段。 */
export interface ToolDescription {
  /** 工具名（模型看到的名字）。 */
  readonly name: string
  /** 模型可见的工具说明。 */
  readonly description: string
  /** 工具参数表，原始 JSON Schema。 */
  readonly parameters: Record<string, unknown>
}

/** 一次桥调用的结果：两个输出流与退出码。 */
interface BridgeRun {
  stdout: string
  stderr: string
  code: number
}

/**
 * 跑一次桥，等它结束。
 * @param options - 解释器与角色身份。
 * @param args - 桥的子命令参数（`list`，或 `call <工具名>`）。
 * @param stdin - 写进子进程 stdin 的内容；省略表示立刻关闭 stdin。
 * @param signal - 调用方取消时用来终止子进程；省略表示不可取消。
 * @returns 子进程的 stdout、stderr 与退出码。
 * @throws 子进程起不来（解释器不存在、无执行权限等）；取消时抛取消错误。
 */
function runBridge(options: BridgeOptions, args: readonly string[], stdin?: string, signal?: AbortSignal): Promise<BridgeRun> {
  return new Promise((resolve, reject) => {
    const child = spawn(
      options.python,
      ['-m', BRIDGE_MODULE, '--role', options.role, ...args],
      { cwd: REPO_ROOT, stdio: ['pipe', 'pipe', 'pipe'] },
    )
    const stdout: Buffer[] = []
    const stderr: Buffer[] = []
    child.stdout.on('data', (chunk: Buffer) => stdout.push(chunk))
    child.stderr.on('data', (chunk: Buffer) => stderr.push(chunk))

    const onAbort = (): void => { child.kill() }
    if (signal !== undefined) {
      if (signal.aborted) onAbort()
      else signal.addEventListener('abort', onAbort, { once: true })
    }

    child.on('error', error => {
      signal?.removeEventListener('abort', onAbort)
      reject(new Error(`tools-bridge: 起不了 ${options.python} -m ${BRIDGE_MODULE}：${error.message}`))
    })
    child.on('close', code => {
      signal?.removeEventListener('abort', onAbort)
      const run = { stdout: Buffer.concat(stdout).toString('utf8'), stderr: Buffer.concat(stderr).toString('utf8'), code: code ?? -1 }
      // 取消不是工具失败：把它与「工具报错」分开，否则模型会把中断当成真实的执行结果。
      if (signal?.aborted === true) reject(new Error(`tools-bridge: ${args.join(' ')} 被取消`))
      else resolve(run)
    })

    child.stdin.end(stdin ?? '')
  })
}

/**
 * 把桥的 stdout 解析成一个 JSON 值。
 * 桥的协议是「stdout 恰好一个 JSON 值」（工具结果里的换行在字符串中被转义），解析不了
 * 说明桥的协议被破坏或 stdout 混入了别的东西 —— 原样带上输出内容抛错，便于定位。
 * @param stdout - 桥的完整 stdout。
 * @param what - 出错信息里指代这次调用的说法。
 * @returns 解析出的 JSON 值。
 * @throws stdout 不是合法 JSON。
 */
function parseJson(stdout: string, what: string): unknown {
  try {
    return JSON.parse(stdout)
  } catch (error) {
    throw new Error(`tools-bridge: ${what} 的 stdout 不是 JSON（${(error as Error).message}）：${JSON.stringify(stdout.slice(0, 500))}`)
  }
}

/** 判断一个解析出的值是否是字符串键对象。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * 取该角色白名单内的全部工具定义。
 * @param options - 解释器与角色身份。
 * @returns 工具描述，顺序即白名单顺序；角色不在 `ROLE_TOOLS` 里时桥以非零退出。
 * @throws 桥退出码非零，或返回的形状不是工具描述数组。
 */
export async function listTools(options: BridgeOptions): Promise<ToolDescription[]> {
  const run = await runBridge(options, ['list'])
  if (run.code !== 0) {
    throw new Error(`tools-bridge: 取 ${JSON.stringify(options.role)} 的工具清单失败（退出码 ${run.code}）：${run.stderr.trim()}`)
  }
  const parsed = parseJson(run.stdout, '工具清单')
  if (!Array.isArray(parsed)) {
    throw new Error(`tools-bridge: 工具清单不是数组，收到 ${JSON.stringify(parsed).slice(0, 200)}`)
  }
  // 每条都要有名有参数：注册进去的工具由模型调用，参数表是引擎校验实参的唯一依据。
  return parsed.map((entry, index) => {
    if (!isRecord(entry) || typeof entry.name !== 'string' || entry.name === '') {
      throw new Error(`tools-bridge: 第 ${index} 条工具描述没有 name：${JSON.stringify(entry).slice(0, 200)}`)
    }
    if (!isRecord(entry.parameters)) {
      throw new Error(`tools-bridge: 工具 ${entry.name} 的 parameters 不是对象：${JSON.stringify(entry.parameters).slice(0, 200)}`)
    }
    // 引擎在调用时按这张表校验模型实参，超出它支持子集的 schema 会到那时才炸；这里提前炸。
    assertSupportedJsonSchema(entry.parameters)
    return {
      name: entry.name,
      description: typeof entry.description === 'string' ? entry.description : '',
      parameters: entry.parameters,
    }
  })
}

/**
 * 执行一次工具调用。
 * @param options - 解释器与角色身份。
 * @param name - 工具名；必须在白名单内，否则桥以非零退出。
 * @param args - 模型给的实参对象。
 * @param signal - 调用方取消时用来终止子进程。
 * @returns 工具的输出文本。
 * @throws 桥协议层失败（非零退出、stdout 不是 JSON），或工具自身报错（`{"error": ...}`）。
 */
export async function callTool(
  options: BridgeOptions,
  name: string,
  args: unknown,
  signal: AbortSignal,
): Promise<string> {
  const run = await runBridge(options, ['call', name], JSON.stringify(isRecord(args) ? args : {}), signal)
  if (run.code !== 0) {
    throw new Error(`tools-bridge: 调用 ${name} 失败（退出码 ${run.code}）：${run.stderr.trim()}`)
  }
  const parsed = parseJson(run.stdout, `调用 ${name}`)
  if (!isRecord(parsed)) {
    throw new Error(`tools-bridge: 调用 ${name} 的返回不是对象：${JSON.stringify(parsed).slice(0, 200)}`)
  }
  // 工具自身报错（桥以 0 退出、在 stdout 给 error）—— 交给引擎记为一次失败的调用，
  // 模型据此换一条路，与 mcp-client 对 `isError` 的处理一致。
  if (typeof parsed.error === 'string') throw new Error(parsed.error)
  if (typeof parsed.result !== 'string') {
    throw new Error(`tools-bridge: 调用 ${name} 的返回既无 error 也无 result：${JSON.stringify(parsed).slice(0, 200)}`)
  }
  return parsed.result
}

/**
 * 把一条工具描述做成本进程可注册的工具定义。
 *
 * 参数表原样来自 available_tools.yaml —— 它已经是引擎需要的 JSON Schema，不再经任何
 * 转换（`defineTool` 那类助手的参数 DSL 会把数组形式的 `required` 当普通字段，这里不用）。
 * @param options - 解释器与角色身份，调用时透传给桥。
 * @param description - 桥返回的一条工具描述。
 * @returns 可由 `ctx.tools.register` 挂载的工具定义。
 * @throws 参数表超出引擎支持的 JSON Schema 子集。
 */
export function createToolDefinition(options: BridgeOptions, description: ToolDescription): ToolDefinition {
  assertSupportedJsonSchema(description.parameters)
  return {
    name: description.name,
    description: description.description,
    parameters: description.parameters,
    output: {
      schema: {
        type: 'object',
        properties: { text: { type: 'string' } },
        required: ['text'],
        additionalProperties: false,
      },
      render: (_args: unknown, value: unknown) => [{
        type: 'text',
        text: isRecord(value) && typeof value.text === 'string' ? value.text : '',
      }],
    },
    // 不声明 isConcurrencySafe：这些工具动机器人或读终端，只能独占执行（缺省即独占）。
    presentCall: args => ({ card: 'generic', title: `调用 ${description.name}`, kind: 'other', rawInput: args }),
    async execute(args: unknown, exec: ToolRunContext): Promise<unknown> {
      return { text: await callTool(options, description.name, args, exec.signal) }
    },
  }
}
