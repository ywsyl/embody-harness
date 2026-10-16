/**
 * 常驻 TTS 服务（`TTS_Manager/tts_service.py --serve`）的一条 socket 连接。
 *
 * 协议是 NDJSON 行协议，逐字对着服务端那份实现写（它的模块头有完整表）：
 *   请求 →  `{"id":N,"op":"speak","text":"…","multi_wheel":false,"play_now":false}`
 *   应答 →  `{"id":N,"event":"accepted","section_id":"…","error_code":0}`
 *           `{"id":N,"event":"failed","error_code":-20,"detail":"…"}`
 *   广播 →  `{"event":"drained","at":<epoch秒>}`（**没有 id**，可能夹在任何两条应答之间）
 * 因此读这边必须按 `id` 认自己的应答、顺手放过广播（`drained` 转给 `onDrained`）。
 *
 * 为什么要它：逐句 spawn 一个脚本，每句要付 bash 前导 + 常驻脚本 configs 桥 + `ros2 service
 * call` 的 CLI 启动与 DDS 发现（2026-10-12 实测合计约 2.1 秒），而设备从收到请求到第一个音频包
 * 只要 0.46 秒 —— 那 2.1 秒就是句间空档。走常驻服务时一句只是 socket 上的一行。
 *
 * 生命周期：连接失败（服务没起 / socket 不在）→ `connect` 抛错，调用方整体退回逐句 spawn；
 * 连接**中途**断开 → `onLost` 回调 + 所有在飞的 `speak` 立刻以失败 reject（不能让它们吊着，
 * 那会把队列卡死）。建连失败与中途断开都由调用方去回退，本类不留退路。
 * @module @embody/dsh-speech-stream/service
 */

import { connect, type Socket } from 'node:net'
import type { Logger } from './tts.ts'

/** 一条在飞的 `speak` 请求：认到自己的应答（或出错）时兑现。 */
interface InFlight {
  readonly resolve: () => void
  readonly reject: (error: Error) => void
  readonly timer: NodeJS.Timeout
}

/** 交一句的应答上限（毫秒）。服务端自己对 play_tts 的应答等 10 秒才判超时，这边多留些余量。 */
const SPEAK_REPLY_TIMEOUT_MS = 12_000

/** 一行 NDJSON 的长度上限（协议里只有短文本；超了说明对面不是本服务）。 */
const MAX_LINE_BYTES = 64 * 1024

/** 连接回调。 */
export interface ServiceLinkOptions {
  /** 服务 socket 的绝对路径（见 paths.ts）。 */
  readonly path: string
  /** 日志出口。 */
  readonly log: Logger
  /** 收到一次 `drained` 广播（= 交给服务的每一句都不再出声了）。 */
  readonly onDrained: () => void
  /** 连接断了（原因已格式化）；调用方据此整体退回逐句 spawn。 */
  readonly onLost: (reason: string) => void
}

/**
 * 到常驻服务的一条长连接。
 *
 * 一条连接同时只该跑一条 `speak`（本插件是单槽串行队列，天然满足）：协议允许并发，但按 `id`
 * 分派时并发没有额外收益，单槽更好定位。
 */
export class TtsServiceLink {
  private buffer = ''
  private nextId = 0
  private readonly inFlight = new Map<number, InFlight>()
  private closed = false

  /**
   * @param socket - 已连上的 socket。
   * @param options - 回调与日志。
   */
  private constructor(
    private readonly socket: Socket,
    private readonly options: ServiceLinkOptions,
  ) {
    socket.setEncoding('utf8')
    socket.on('data', (chunk: string) => { this.onData(chunk) })
    socket.on('error', (error: Error) => { this.lose(`连接出错：${error.message}`) })
    socket.on('close', () => { this.lose('连接被对端关闭') })
  }

  /**
   * 连上常驻服务；连不上（ENOENT / ECONNREFUSED / 超时）抛错，由调用方退回逐句 spawn。
   * @param options - 回调与日志。
   * @returns 已连上的连接。
   */
  static connect(options: ServiceLinkOptions): Promise<TtsServiceLink> {
    return new Promise((resolve, reject) => {
      const socket = connect(options.path)
      const onConnectError = (error: Error): void => {
        socket.destroy()
        reject(error)
      }
      socket.once('error', onConnectError)
      socket.once('connect', () => {
        socket.off('error', onConnectError)
        resolve(new TtsServiceLink(socket, options))
      })
    })
  }

  /**
   * 交一句给服务，收到它那条 `accepted` 就兑现。
   *
   * `play_now=false` 只表示不主动抢麦：**设备没有队列**（每来一条新请求它自己先停掉在播的
   * 那条，实测见 `tts.ts`），所以「接着播」要等服务广播 `drained` 之后再交下一句
   * （`Speaker` 的 `serial`）。
   * @param text - 已净化的一句话。
   * @param multiWheel - 多轮播报开关（与脚本默认一致，恒 false）。
   * @param playNow - 是否打断在播的那条（恒 false，判据见 machine.ts）。
   * @returns 服务受理后兑现；被拒/断开/超时都 reject。
   */
  speak(text: string, multiWheel: boolean, playNow: boolean): Promise<void> {
    if (this.closed) return Promise.reject(new Error('连接已关闭'))
    this.nextId += 1
    const id = this.nextId
    return new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.inFlight.delete(id)
        reject(new Error(`交一句超过 ${SPEAK_REPLY_TIMEOUT_MS}ms 没有应答`))
      }, SPEAK_REPLY_TIMEOUT_MS)
      this.inFlight.set(id, { resolve, reject, timer })
      this.write(JSON.stringify({ id, op: 'speak', text, multi_wheel: multiWheel, play_now: playNow }))
    })
  }

  /** 收掉这条连接（不再收 `drained`，也不会再回退 —— 卸载时用）。 */
  close(): void {
    this.closed = true
    this.socket.destroy()
    this.failInFlight(new Error('连接已关闭'))
  }

  /**
   * 写一行请求（同步失败也走 `lose`：socket 已经坏了，回退由调用方做）。
   * @param line - 一行 JSON（不带换行）。
   */
  private write(line: string): void {
    try {
      this.socket.write(`${line}\n`)
    } catch (error: unknown) {
      this.lose(`写请求失败：${error instanceof Error ? error.message : String(error)}`)
    }
  }

  /**
   * 收数据：按行切开，逐行分派（`drained` 广播 vs 带 id 的应答）。
   * @param chunk - 本次读到的文本（可能半行）。
   */
  private onData(chunk: string): void {
    this.buffer += chunk
    if (this.buffer.length > MAX_LINE_BYTES) {
      this.lose('收到超长的一行（对面不是常驻 TTS 服务？）')
      return
    }
    let index = this.buffer.indexOf('\n')
    while (index >= 0) {
      const line = this.buffer.slice(0, index)
      this.buffer = this.buffer.slice(index + 1)
      this.dispatch(line)
      if (this.closed) return
      index = this.buffer.indexOf('\n')
    }
  }

  /**
   * 分派一行：广播交给 `onDrained`，应答按 id 兑现。
   * @param line - 一行 JSON（空行忽略）。
   */
  private dispatch(line: string): void {
    if (line.trim() === '') return
    let message: { id?: unknown; event?: unknown; detail?: unknown }
    try {
      message = JSON.parse(line) as typeof message
    } catch {
      this.lose(`应答不是合法 JSON：${line.slice(0, 200)}`)
      return
    }
    if (message.event === 'drained') {
      this.options.onDrained()
      return
    }
    if (typeof message.id !== 'number') {
      this.options.log(`忽略认不出的消息：${line.slice(0, 200)}`)
      return
    }
    const pending = this.inFlight.get(message.id)
    if (pending === undefined) {
      this.options.log(`收到没有对应请求的应答 id=${String(message.id)}（忽略）`)
      return
    }
    this.inFlight.delete(message.id)
    clearTimeout(pending.timer)
    if (message.event === 'accepted') {
      pending.resolve()
      return
    }
    pending.reject(new Error(typeof message.detail === 'string' && message.detail !== ''
      ? message.detail
      : `服务拒绝了这一句（${String(message.event)}）`))
  }

  /**
   * 判定这条连接没了：通知一次、让在飞的请求全部失败（不许有请求永远吊着）。
   * @param reason - 原因（已格式化）。
   */
  private lose(reason: string): void {
    if (this.closed) return
    this.closed = true
    this.socket.destroy()
    this.failInFlight(new Error(reason))
    this.options.onLost(reason)
  }

  /**
   * 让所有在飞的请求立刻失败。
   * @param error - 拒绝原因。
   */
  private failInFlight(error: Error): void {
    for (const pending of this.inFlight.values()) {
      clearTimeout(pending.timer)
      pending.reject(error)
    }
    this.inFlight.clear()
  }
}
