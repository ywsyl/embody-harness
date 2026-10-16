/**
 * 逐句送 TTS：一个**单槽**队列，默认走常驻服务（`TTS_Manager/tts_service.py --serve`）。
 *
 * 为什么不再逐句起进程：每句一个 `run_tts_speak.sh` 要付 bash 前导 + 常驻脚本的 configs 桥 +
 * `ros2 service call` 的 CLI 启动与 DDS 发现，2026-10-12 实测合计约 2.1 秒；而设备从收到请求到
 * 第一个音频包只要 0.46 秒。用户听到的就是句间那 2~3 秒的空档（speechcore 日志：
 * 10:18:42.795 第一句播完 → 10:18:45.311 第二句才 `runInquiry`）。走常驻服务时一句只是 socket
 * 上的一行。
 *
 * **为什么必须等这一句播完才交下一句**（`serial`，默认）：**设备没有队列** —— 每来一条新
 * `play_tts` 请求，`speech_core` 都先 `speech app stop` 停掉在播的那条再 `runInquiry`。
 * 2026-10-12 15:37~15:39 实测三次，前一句分别在 75ms / 96ms / 185ms 处被后一句的请求掐断
 * （`speech app stop called notifyUserAbort: 0` → `UserAbort_e` → `displayError UserCancel`），
 * 用户听到的就是「只播了后半句」。所以连播只能由客户端保证：等这服务广播的 `drained`（这一句
 * 真的播完了）再交下一句，句间多出的 0.8s 就是它的价钱。
 *
 * 单槽（一次只交一句、上一句了结再交下一句）保住的是**顺序**：并发写多条命令时谁先到服务
 * 节点并不由我们决定，而顺序是这套东西唯一不能让的东西。
 *
 * **回退**：连不上常驻服务（没起 / `--no-tts-service`）或中途断开 → 这一轮剩下的句子整体退回
 * 逐句 spawn（行为与接服务之前逐字相同）。中途断开时**那句已经写出去的不再重发**（它很可能
 * 已经交给设备出声了，重发会说两遍），只记一行日志 —— 少播一句好过说两遍。
 *
 * 判据见 `machine.ts`（轮次号不是「用户的一轮」，按它打断会把上一段话切在半句上）；
 * 记进 `state.ts` 的 `drained_at` 见 `onDrain` 的说明。
 * @module @embody/dsh-speech-stream/tts
 */

import { spawn, type ChildProcess } from 'node:child_process'
import { REPO_ROOT } from './paths.ts'
import { TtsServiceLink } from './service.ts'

/** 交接失败/结束的日志出口，形如 `[speech-stream] …`。 */
export type Logger = (message: string) => void

/**
 * 交付方式：`serial` = 等这一句播完（服务广播 `drained`）再交下一句（默认，设备没有队列时的
 * 唯一正解）；`pipeline` = 收到 `accepted` 就交下一句 —— 只有设备改成真排队才有意义，现在等于
 * 让后一句掐断前一句，留着是为了设备升级后能一行配置切回去。
 */
export type DeliveryMode = 'pipeline' | 'serial'

/** 逐句交付器：队列 + 单槽执行 + 卸载时收尾。 */
export class Speaker {
  private readonly queue: string[] = []
  private child: ChildProcess | undefined
  private link: TtsServiceLink | undefined
  private connecting = false
  private fallback = false
  private inFlight = false
  /**
   * 交出去之后还没收到「见底」。`serial` 靠它拦下一句：**不能只看 `drained` 到没到过**——
   * 应答（`accepted`）与 `drained` 的先后不定（设备要等这句话播完才回 Idle，而 play_tts 的
   * 应答本身可能晚到十几秒），只看先后会在「drained 先到」时永久卡住。
   */
  private sentSinceDrain = false
  /** 服务报过「见底」，且那次之后没有再交新句子（新交接会让它作废，同 QueueDrain.handoff）。 */
  private serviceDrained = false
  private disposed = false

  /**
   * @param scriptPath - 回退路径的 TTS 脚本绝对路径（`run_tts_speak.sh`）。
   * @param socketPath - 常驻服务的 unix socket 路径。
   * @param deliveryMode - 交下一句的时机（见 `DeliveryMode`）。
   * @param log - 日志出口。
   * @param onDrain - 队列见底（队里没句子、也没有在飞的交接，且**服务报过见底**）时回调一次：
   *   这一刻**交出去的每一句都不会再出声了**。记进 `state.ts` 的记录文件供 Python 侧等「这段
   *   文字播完」——它拿不到这里的 `section_id`，只能靠这个事实（见 `plugin_tools/audio_tools.py` 的 `speak_text`）。
   *   「服务报过见底」这半条不能省：服务只知道它自己收到过哪些句子，不知道这里还攒着几句
   *   没生成完；少了它，长时间生成中间那次见底就会让 Python 侧以为整段都播完了。
   */
  constructor(
    private readonly scriptPath: string,
    private readonly socketPath: string,
    private readonly deliveryMode: DeliveryMode,
    private readonly log: Logger,
    private readonly onDrain: () => void,
  ) {
    this.connect()
  }

  /** 还排在队里、没交出去的句数。 */
  get pending(): number {
    return this.queue.length
  }

  /**
   * 排一句，交给队列去送。
   *
   * 注意调用方要**先**把「已经交出去多少个字」写进状态文件、再调这里：记的是交接意图，
   * 不是交接结果。顺序反过来的话，交接那一小段时间里 Python 侧会以为这几个字还没送出去，
   * 于是又送一遍——用户就听见两遍。反过来（记了却没送成）只会在极端情况下少播一句，
   * 两害相权取其轻。
   * @param text - 已净化的一句话。
   */
  enqueue(text: string): void {
    this.queue.push(text)
    this.pump()
  }

  /**
   * 卸载：关掉到服务的连接、杀掉回退路径在飞的脚本、清空队列（没交出去的句子不再播）。
   *
   * 置 `disposed` 是为了不再报「队列见底」：没交出去的那几句不会出声，这不是一次正常的收尾。
   */
  dispose(): void {
    this.disposed = true
    this.queue.length = 0
    const running = this.child
    this.child = undefined
    if (running !== undefined && running.exitCode === null && running.signalCode === null) {
      running.kill('SIGTERM')
    }
    const link = this.link
    this.link = undefined
    link?.close()
  }

  // ---------------------------------------------------------------- 选路

  /** 建连（只试一次；连不上就是这一轮全程回退，不反复重连）。 */
  private connect(): void {
    if (this.link !== undefined || this.connecting || this.fallback) return
    this.connecting = true
    TtsServiceLink.connect({
      path: this.socketPath,
      log: this.log,
      onDrained: () => { this.onServiceDrained() },
      onLost: (reason) => { this.loseService(reason) },
    }).then((link) => {
      this.connecting = false
      if (this.disposed) {
        link.close()
        return
      }
      this.link = link
      this.log(`已连上常驻 TTS 服务（${this.socketPath}），逐句走 socket`)
      this.pump()
    }).catch((error: unknown) => {
      this.connecting = false
      if (this.disposed) return
      this.loseService(`连不上（${error instanceof Error ? error.message : String(error)}）`)
    })
  }

  /**
   * 判定「这一轮不再走服务」：关掉连接、把剩下的句子交给逐句 spawn。
   * @param reason - 原因（已格式化，进日志）。
   */
  private loseService(reason: string): void {
    const first = !this.fallback
    this.fallback = true
    this.inFlight = false
    this.sentSinceDrain = false
    this.serviceDrained = false
    const link = this.link
    this.link = undefined
    link?.close()
    if (first) this.log(`常驻 TTS 服务不可用（${reason}），本轮退回逐句 spawn`)
    this.pump()
  }

  // ---------------------------------------------------------------- 送

  /** 队列里还有话就接着送；在飞时什么都不做（应答/事件回来会再叫一次）。 */
  private pump(): void {
    if (this.disposed) return
    if (this.child !== undefined) return                  // 回退路径在飞
    if (this.link === undefined) {
      // 还没连上：建连的结果回来会再叫一次；已经判定服务不在就直接走脚本
      if (this.fallback) this.pumpFallback()
      else this.connect()
      return
    }
    this.pumpService()
  }

  /** 服务模式：单槽交一句（默认的 `serial` 要等上一句播完 —— 服务广播 `drained` 才算）。 */
  private pumpService(): void {
    if (this.inFlight || (this.deliveryMode === 'serial' && this.sentSinceDrain)) return
    const text = this.queue.shift()
    if (text === undefined) return
    const link = this.link
    if (link === undefined) return
    // 一次交接就让先前那次「见底」作废：它说的是交代它**之前**的事（同 QueueDrain.handoff）。
    // `sentSinceDrain` 同一次交接立起来，由 `drained` 放下来 —— 两种到达次序都拦得住。
    this.serviceDrained = false
    this.sentSinceDrain = true
    this.inFlight = true
    // 第 2 个参数是 multi_wheel（恒 false，与脚本默认一致），第 3 个是 play_now（恒 false：
    // 不主动打断在播的音频，判据见 machine.ts）
    link.speak(text, false, false).then(() => {
      this.inFlight = false
      this.pump()          // serial 由 `sentSinceDrain` 拦住（等 drained 再叫一次）
    }).catch((error: unknown) => {
      this.inFlight = false
      if (this.disposed) return
      this.log(`这一句没能交给常驻服务（${error instanceof Error ? error.message : String(error)}）`
        + '：不再重发（它可能已经出声），剩下的退回逐句 spawn')
      this.loseService('交接失败')
    })
  }

  /** 回退路径：起一个脚本、等它退出（`wait_done=true`，退出即播完）。 */
  private pumpFallback(): void {
    if (this.disposed || this.child !== undefined) return
    const text = this.queue.shift()
    if (text === undefined) {
      // 到这里 = 队里没句子、也没有在飞的脚本 = 不会再出声了（正常是最后那句播完了）
      this.onDrain()
      return
    }
    let child: ChildProcess
    try {
      // 第 3 个参数是 play_now（恒 false，不主动打断），第 4 个是 wait_done=true ——
      // 等这一句真的播完才让下一句接手
      child = spawn(
        this.scriptPath,
        [text, 'false', 'false', 'true'],
        { cwd: REPO_ROOT, stdio: ['ignore', 'ignore', 'pipe'] },
      )
    } catch (error: unknown) {
      this.log(`起不了 ${this.scriptPath}：${error instanceof Error ? error.message : String(error)}`)
      this.pump()
      return
    }
    this.child = child
    const stderr: Buffer[] = []
    child.stderr?.on('data', (chunk: Buffer) => stderr.push(chunk))
    child.on('error', (error: Error) => {
      this.log(`播报子进程出错：${error.message}`)
    })
    child.on('close', (code: number | null) => {
      this.child = undefined
      if (code !== 0) {
        // 脚本的诊断都在 stderr（stdout 成功时保持为空），原样带出来便于定位
        const detail = Buffer.concat(stderr).toString('utf8').trim()
        this.log(`播报失败（退出码 ${String(code)}）：${detail === '' ? '(无输出)' : detail}`)
      }
      this.pump()
    })
  }

  /** 服务报「见底」。它只是一半的判据，另一半是本队列自己的状态（见 `reportDrained`）。 */
  private onServiceDrained(): void {
    this.serviceDrained = true
    this.sentSinceDrain = false          // 上一句确实播完了，serial 可以交下一句
    this.pump()
    this.reportDrained()
  }

  /** 两半都成立才报「队列见底」：服务报过，且本队列空着、没有在飞的交接。 */
  private reportDrained(): void {
    if (this.disposed || !this.serviceDrained) return
    if (this.inFlight || this.queue.length > 0 || this.child !== undefined) return
    this.serviceDrained = false
    this.onDrain()
  }
}
