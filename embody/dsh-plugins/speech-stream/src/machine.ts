/**
 * 流式语音的状态机：**帧 → 句子**，以及「这一轮已经处理到第几个字」的记账。
 *
 * 单独成文件是为了能脱开引擎验：本模块不 import 任何引擎运行时（帧类型是 `import type`，
 * 运行时被擦掉），只依赖同目录三个纯模块。`tools/selftest.ts` 用一个假 sink 把整条链
 * （分片到达 → 解码 → 切句 → 记账 → 尾部处理）跑一遍。
 *
 * 认帧的三条规则：
 *   · **只认当前 attempt**：引擎在一个 attempt 被放弃后会重发同样的参数（`llm-retry` 在
 *     请求层重试），所以 `end{abandoned}` 时要记下已经播出的明文，让重试那次只补新增的部分
 *     ——人耳听到的是连续的一句话，而不是同一句念两遍。
 *   · **新一轮 = 上一轮的记账作废，但出声一律不抢麦**：`start.turn` 是 durable 且单调的。
 *     轮次一变，上一轮的记录全部作废、手里的缓冲丢弃。**`play_now` 恒为 false**：轮次号是
 *     唤醒计数而不是「用户的一轮」——云端任务、监督器事件、开机自查各占一轮；而一轮在工具
 *     调用派发时就结束了，音频还要在队列里走十几秒。按轮次打断就是让下一条无关事件把上一段
 *     话拦腰切断。`play_now=false` 只表示**我们不发** stop —— **设备没有队列**：每来一条新
 *     请求它自己先 `speech app stop` 停掉在播的那条（实测 75~185ms 处掐断，见 `tts.ts`），
 *     所以连播只能由我们「等这一句播完再交下一句」（`SPEECH_DELIVERY_MODE=serial`）。用户侧
 *     的打断由设备自己做（唤醒发 status=8 UserCancel 掐掉在播的 TTS）。
 *   · **尾巴分两种**：参数写完（顶层对象闭合）、或整条消息提交（`assistant/message`）→ 把
 *     最后那句没带句末标点的补播；被中断的 attempt（`assistant/attempt`）与放弃 → 丢掉那半句。
 * @module @embody/dsh-speech-stream/machine
 */

import type { AssistantStreamFrame } from '@deepseek-ai/dsh-agent'
import { splitChunks, sanitize, type FenceState } from './sentence.ts'
import { readTextField } from './toolArgs.ts'
import type { SpokenItem } from './state.ts'

/** 帧里带 chunk 的那一种（避免为 `chunk.chunk` 的 StreamChunk 再引一个引擎包）。 */
type ChunkFrame = Extract<AssistantStreamFrame, { type: 'chunk' }>

/** 会把 `text` 参数当播报稿的工具。三个共用同一套「说 + 等播完 + 收音」语义。 */
export const SPEECH_TOOLS: ReadonlySet<string> = new Set(['speak', 'speak_listen', 'speak_input'])

/** 一次工具调用的中途状态。 */
export interface CallState {
  /** 该调用在消息里的块下标（同一条消息里的第几个工具调用）。 */
  readonly index: number
  /** 工具名：只在首个分片上到达，之后的分片不带，所以要记住。 */
  name: string | undefined
  /** 累积的 `argumentsDelta` 原文。 */
  raw: string
  /** `text` 参数**已能确定的**明文（前缀稳定，只会变长）。 */
  text: string
  /** 已从明文里处理掉多少字符（送出去过、或按规则丢弃）。 */
  emitted: number
  /** 已处理但还没成句、留在手里的部分。 */
  buffer: string
  /** 重试去重要跳过的前缀长度；`undefined` = 还没判定出来（文本还不够长）。 */
  skip: number | undefined
  /** 该次调用自己的围栏状态（一次调用的 text 里可能有代码围栏）。 */
  readonly fence: FenceState
}

/** 状态机的出口：记账、出声、日志。由 index.ts 接真实实现，自测里接假的。 */
export interface SpeechSink {
  /** 进入新轮次：上一轮的记录作废。 */
  beginTurn(turn: number): void
  /** 记一次交接（**必须在对应句子的 `speak` 之前**，见 `SpeechMachine.feed`）。 */
  record(item: Omit<SpokenItem, 'at'>): void
  /** 送一句出去（排在上一条播完之后；不主动打断在播的）。 */
  speak(text: string): void
  /** 记一条日志。 */
  log(message: string): void
}

/** 建一次调用的状态。 */
export function createCall(index: number): CallState {
  return { index, name: undefined, raw: '', text: '', emitted: 0, buffer: '', skip: undefined, fence: { open: false } }
}

/** 流式语音的状态机，一个 dsh 进程一份（本插件只装在交互器上）。 */
export class SpeechMachine {
  private turn = -1
  private retrySkip: string | undefined
  private readonly calls = new Map<number, CallState>()

  /**
   * @param sink - 记账/出声/日志的出口。
   * @param maxCharsPerChunk - 软边界阈值（见 Config）。
   */
  constructor(private readonly sink: SpeechSink, private readonly maxCharsPerChunk: number) {}

  /**
   * 吃一帧。
   * @param frame - `agent/assistant-stream` 的载荷帧。
   */
  onFrame(frame: AssistantStreamFrame): void {
    switch (frame.type) {
      case 'start':
        this.startTurn(frame.turn)
        return
      case 'chunk':
        this.onChunk(frame.chunk)
        return
      case 'end':
        this.onEnd(frame.outcome)
        return
      default:
        return
    }
  }

  /** 卸载：手里的缓冲全丢（进程要走了，没成句的尾巴不再播），记录由调用方落盘。 */
  dispose(): void {
    this.calls.clear()
  }

  private startTurn(turn: number): void {
    if (turn === this.turn) return
    this.turn = turn
    this.retrySkip = undefined
    this.calls.clear()
    this.sink.beginTurn(turn)
  }

  private onChunk(chunk: ChunkFrame['chunk']): void {
    if (chunk.type !== 'tool-call-delta') return
    let call = this.calls.get(chunk.index)
    if (call === undefined) {
      call = createCall(chunk.index)
      this.calls.set(chunk.index, call)
    }
    if (chunk.name !== undefined) call.name = chunk.name
    call.raw += chunk.argumentsDelta
    if (call.name === undefined || !SPEECH_TOOLS.has(call.name)) return
    this.feed(call)
  }

  private feed(call: CallState): void {
    const { text, complete } = readTextField(call.raw)
    if (call.skip === undefined) {
      if (this.retrySkip === undefined) {
        call.skip = 0
      } else if (text.length >= this.retrySkip.length) {
        // 判得出来了：被放弃那次播出的明文是这次的前缀 → 只补新增的部分
        call.skip = text.startsWith(this.retrySkip) ? this.retrySkip.length : 0
        this.retrySkip = undefined
      } else {
        return
      }
    }
    if (text.length > call.emitted) {
      call.buffer += text.slice(Math.max(call.emitted, call.skip))
      call.emitted = text.length
      call.text = text
      const { chunks, rest } = splitChunks(call.buffer, this.maxCharsPerChunk, call.fence)
      if (chunks.length > 0) {
        // 记账**先于**出声：这份记录是「这几个字已经归我管了」的凭据，Python 侧的
        // speak 派发时据此避免重复送；写晚了（脚本起来那 1~3 秒里）它就会再送一遍。
        this.sink.record({
          turn: this.turn,
          index: call.index,
          tool: call.name ?? '',
          text: call.text,
          spoken: call.emitted - rest.length,
        })
        for (const sentence of chunks) this.sink.speak(sentence)
      }
      call.buffer = rest
    }
    if (complete) this.finish(call, true)
  }

  private onEnd(outcome: Extract<AssistantStreamFrame, { type: 'end' }>['outcome']): void {
    if (outcome.kind === 'abandoned') {
      // 重试会重发同一段参数：记下已经播到哪里，重试那次只补新增的部分
      this.retrySkip = this.longestText()
    }
    // 被中断的 attempt（assistant/attempt）与放弃：最后那半句正是被掐掉的内容，不补
    const keepTail = outcome.kind === 'committed' && outcome.eventType === 'assistant/message'
    for (const call of this.calls.values()) {
      if (call.name === undefined || !SPEECH_TOOLS.has(call.name)) continue
      this.finish(call, keepTail)
    }
    this.calls.clear()
    if (outcome.kind === 'committed') this.retrySkip = undefined
  }

  /**
   * 收束一次调用：先按 `keepTail` 决定尾巴播不播，再落一条收尾记录。
   *
   * 收尾记录即便一个字都没送出去也要写，且 `spoken` 记成「全部已处理」：Python 侧对不上
   * 记录时会整段重播，而这一刻的语义恰恰是「这次调用到此为止，没播的（被中断的半句、被
   * 丢弃的围栏内容）不要再补」。
   * @param call - 该次调用。
   * @param keepTail - 是否补播最后那句没带句末标点的。
   */
  private finish(call: CallState, keepTail: boolean): void {
    const tail = keepTail ? sanitize(call.buffer) : ''
    call.buffer = ''
    if (call.text !== '') {
      this.sink.record({
        turn: this.turn,
        index: call.index,
        tool: call.name ?? '',
        text: call.text,
        spoken: call.emitted,
      })
    }
    if (tail !== '') this.sink.speak(tail)
  }

  /** 被放弃那次的明文（最长的那条，正常只有一条同 `text` 的调用）。 */
  private longestText(): string | undefined {
    let longest: string | undefined
    for (const call of this.calls.values()) {
      if (call.text === '') continue
      if (longest === undefined || call.text.length > longest.length) longest = call.text
    }
    return longest
  }
}
