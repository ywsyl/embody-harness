/**
 * @embody/dsh-speech-stream —— 交互器的流式语音：**边写边说**。
 *
 * 交互器说话走 `speak` / `speak_listen` / `speak_input` 工具（`plugin_tools/`）。这三条
 * 工具调用的参数里，`text` 就是「说给用户听的那句话」——**不是模型正文**：交互器的思考与
 * 分析会有大段文字，那些留在正文里，一个字都不播。本插件只认这三个工具的 `text` 参数。
 *
 * 今天的问题：工具调用要等整条消息结算才派发（`packages/core/agent-loop/src/agent.ts` 先
 * 收完 assistant message 里的 tool-call 块再 `executeToolCalls`），所以一句话要等整段写完、
 * 派发、再调服务（1~3 秒）才出声。而参数本身是**逐片到达**的
 * （`packages/llm/llm-deepseek/src/translate.ts` 把上游每片 `function.arguments` 原样发成
 * `tool-call-delta`），本插件就吃这条流：一句写完一句就送 TTS，首句出声从「整段生成完」
 * 提前到「第一句生成完」。
 *
 * 三件事（分别落在同目录三个模块里）：
 *   1) **认帧**（`machine.ts`）：只处理当前 attempt 的帧；新一轮（`start.turn` 变大）时
 *      作废上一轮的记账（**不打断**已在播的音频）；被放弃的 attempt 丢掉没成句的尾巴。
 *   2) **切句**（`sentence.ts` + `toolArgs.ts`）：从参数片段里增量解出 `text` 明文，按句末
 *      标点切开，交给 `tts.ts` 的单槽队列（默认走常驻 TTS 服务，一句一行）。
 *   3) **留痕**（`state.ts`）：每交接一句就把「这次调用已经处理到第几个字」写进记录文件，
 *      Python 侧派发 `speak` / `speak_listen` 时据此去重（`plugin_tools/audio_tools.py`），
 *      从而既不重复出声、也不依赖本插件一定在跑——**记录不在它就照老路整段播**，所以
 *      开关这个插件等于开关流式播报，Python 侧不需要任何模式分支。同一份记录里还写
 *      「队列见底」的时刻（`drained_at`）：工具层没有 `section_id`，等「这段文字播完」
 *      只能靠这个事实——状态话题在两句之间本来就会空一次。
 *
 * 已知残留风险（不假装解决）：
 *   · 本插件送出去的每一句都**不主动抢麦**（`play_now=false`，判据见 `machine.ts`），而且
 *     **设备没有队列**：每条新请求它自己先停掉在播的那条（实测见 `tts.ts`）。所以句子是
 *     **等上一句播完再交下一句**（`SPEECH_DELIVERY_MODE=serial`），句间因此多出约 0.8s 空档。
 *     用户侧的打断由设备自己做（唤醒发 status=8 UserCancel 掐断在播的 TTS），对话里抢麦的是
 *     `audio_interact`（走 Python）；插件这一路若要半途闭嘴，只能靠这两个现成机制。
 *
 * 遵循 dsh 引擎函数插件约定：命名导出 name / Config / apply，无默认导出（默认导出会被
 * Loader 丢弃命名导出元信息）。本插件不注册服务也不消费服务，因此没有 `inject`。Config 用
 * schemastery 校验、**不设默认值**——阈值是部署选择，由 profile 层给全（漏了就在装载时报
 * missing required value，没有内置节拍可退化）。依赖解析同 ../supervisor-loop：本包**不声明
 * 引擎依赖**，运行时在 tsx 源码启动的 dsh 内解析。
 * @module @embody/dsh-speech-stream
 */

import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import { SpeechMachine, type SpeechSink } from './machine.ts'
import { STATE_FILE, TTS_SCRIPT, TTS_SERVICE_SOCKET } from './paths.ts'
import { SpokenStore } from './state.ts'
import { Speaker, type DeliveryMode, type Logger } from './tts.ts'

export const name = 'speech-stream'

/** 日志前缀（与 ../supervisor-loop 同形，便于在 daemon 日志里按来源筛）。 */
const PLUGIN_SOURCE = '@embody/dsh-speech-stream'

/** 本插件的部署选择：全部 required（见模块注释）。 */
export interface Config {
  /**
   * 软边界阈值（字数）：一个句子长到这个字数还没说完，就在最近的语气停顿处断开。
   * 它是「不让一句长话拖住出声」的兜底，不是硬性字数限制——有句末标点就按标点断。
   */
  maxCharsPerChunk: number
  /**
   * 交下一句的时机：`serial` = 等这一句播完再交（默认，设备没有队列）；`pipeline` = 收到
   * `accepted` 就交（会让后一句掐断前一句，只有设备改成真排队才成立）。取值见 `tts.ts` 的
   * `DeliveryMode`。
   */
  deliveryMode: DeliveryMode
}

/** Schemastery 配置：不设默认值，由 profile 层给全（见模块注释）。 */
export const Config: z<Config> = z.object({
  maxCharsPerChunk: z.number().required(),
  deliveryMode: z.union(['pipeline', 'serial'] as const).required(),
})

/**
 * 挂载流式播报：订阅助手流、把句子交给 TTS，并在卸载时收尾。
 * @param ctx - 插件 context（订阅的是进程内事件，不需要任何服务）。
 * @param config - 校验后的配置。
 */
export function apply(ctx: Context, config: Config): void {
  // schemastery 只校验「是个 number」，NaN 也算 number（typeof NaN === 'number'）。而
  // profile 层的取值是 `!!js Number(process.env.SPEECH_MAX_CHARS_PER_CHUNK)`：环境变量没
  // 过桥时它就是 NaN，切句会退化成「一个字一句」——每个字一次 ROS 服务调用。宁可装载即失败。
  if (!Number.isFinite(config.maxCharsPerChunk) || config.maxCharsPerChunk <= 0) {
    throw new Error(
      `speech-stream: maxCharsPerChunk 必须是正数，收到 ${String(config.maxCharsPerChunk)}`
      + '（Interactor/cordis.patch.yml 的 !!js 没取到 process.env.SPEECH_MAX_CHARS_PER_CHUNK？'
      + '该变量由 scripts/role-daemon.sh 从 configs/configs.py 过桥）',
    )
  }
  const log: Logger = (message) => { console.error(`[${PLUGIN_SOURCE}] ${message}`) }
  const store = new SpokenStore(STATE_FILE, log)
  const speaker = new Speaker(
    TTS_SCRIPT, TTS_SERVICE_SOCKET, config.deliveryMode, log, () => { store.markDrained() },
  )
  const sink: SpeechSink = {
    beginTurn: (turn) => { store.beginTurn(turn) },
    record: (item) => { store.record(item) },
    speak: (text) => {
      // 一行一句：真机上「首句比整段早多少」只能从这里看
      log(`出声（逐句，${text.length} 字，队列 ${speaker.pending} 句）：${text}`)
      speaker.enqueue(text)
    },
    log,
  }
  const machine = new SpeechMachine(sink, config.maxCharsPerChunk)

  // 进程内瞬态事件，帧里有工具参数的增量（见模块注释第 1 条）
  ctx.on('agent/assistant-stream', ({ frame }) => { machine.onFrame(frame) })

  // 卸载：先丢掉手里没成句的缓冲，再杀掉在飞的那句（音频发出去收不回，但至少不再起新的），
  // 最后落一份记录——Python 侧读到它就知道「这些字已经归插件管过」。
  ctx.effect(() => () => {
    machine.dispose()
    speaker.dispose()
    store.flush()
  }, 'speech-stream.drain')
}
