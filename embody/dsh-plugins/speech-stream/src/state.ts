/**
 * 「这一轮已经播到第几个字」的记录文件——插件与 Python 工具之间唯一的事实源。
 *
 * 为什么需要它：插件在参数流里把句子提前送进 TTS 之后，Python 侧的 `speak` / `speak_listen`
 * 还会被派发一次（工具调用要等整条消息结算，见 packages/core/agent-loop/src/agent.ts）。
 * 那时它必须知道「这句话已经播过多少」：全播过就只等播完，播了一半就只补剩下的那截，
 * 没有记录（插件没装载、刚重启、记录过期）就照老路整段播。**插件不在 = 行为回到今天**，
 * 开关因此不需要在 Python 侧再写一份模式分支。
 *
 * 单写者 = 本插件（Python 只读），所以原子写只要 tmp + rename，不需要跨进程锁。
 * `spoken` 记的是**已交给 TTS 的明文长度**（不是「已播完」）——按解码后的字符数算，与
 * Python 侧切片用的是同一个下标基准。另有一个顶层的 `drained_at`（队列见底的时刻，见
 * `markDrained`）专门回答「播完了没有」——两者刻意分开：交出去和播完是两件事。
 * @module @embody/dsh-speech-stream/state
 */

import { mkdirSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import type { Logger } from './tts.ts'

/** 一条播报记录：某一次工具调用里，已经送出去多少个字。 */
export interface SpokenItem {
  /** 所属轮次（引擎的 durable turn 号）。 */
  readonly turn: number
  /** 该轮内工具调用块的下标（同一条消息里第几个工具调用）。 */
  readonly index: number
  /** 工具名（speak / speak_listen / speak_input）。 */
  readonly tool: string
  /** 该次调用 `text` 参数**已能确定的**明文（可能还在长）。 */
  readonly text: string
  /** 已交给 TTS 的明文长度。 */
  readonly spoken: number
  /** 记录时刻，epoch 秒（与 Python 的 `time.time()` 同一基准）。 */
  readonly at: number
}

/** 落盘的整体结构。 */
interface SpokenFile {
  turn: number
  updated_at: number
  drained_at: number
  items: SpokenItem[]
}

/** 记录器：内存里按「轮次 + 调用下标」攒着，一有变化就整份原子写盘。 */
export class SpokenStore {
  private turn = -1
  private drainedAt = 0
  private readonly items = new Map<number, SpokenItem>()

  /**
   * @param path - 记录文件路径（见 paths.ts）。
   * @param log - 日志出口。
   */
  constructor(private readonly path: string, private readonly log: Logger) {}

  /**
   * 进入新一轮：清空上一轮的全部记录。
   *
   * 新一轮开时上一轮的文字已全部交出去（`spoken` 记的就是「已交给 TTS」，包括被中断那次的
   * 半句），那些记录再被认领只会让 Python 侧少播——留着是错的。
   * @param turn - 引擎给的轮次号。
   */
  beginTurn(turn: number): void {
    if (this.turn === turn) return
    this.turn = turn
    this.items.clear()
    this.flush()
  }

  /**
   * 记一次交接：某次调用的文字现在写到第几个字。
   * @param item - 不含 `at` 的记录（时刻在这里补）。
   */
  record(item: Omit<SpokenItem, 'at'>): void {
    this.items.set(item.index, { ...item, at: Date.now() / 1000 })
    this.flush()
  }

  /**
   * 记一次「队列见底」：交给 TTS 的每一句都不会再出声了（`tts.ts` 的 `Speaker` 在队里没句子、
   * 也没有在飞的脚本时叫这里；正常情况就是每句都播完了）。
   *
   * 这是给 Python 侧唯一一条「这段话真的播完了」的判据：播报是插件送出去的，工具层拿不到
   * `section_id`，而**状态话题上的 Idle 不等于播完**——逐句队列在两句之间要做一次 ROS 服务
   * 调用（1~3 秒），那段时间话题上就是 Idle，按它判会返回到两句中间去。`drained_at` 单调
   * 前进（不随 `beginTurn` 清空），读方拿它和自己那条记录的 `at` 比：`drained_at >= at` 就是
   * 「写这条记录之后队列见底过」。
   */
  markDrained(): void {
    this.drainedAt = Date.now() / 1000
    this.flush()
  }

  /** 整份落盘（tmp + rename，读方永远看到一份完整的 JSON）。 */
  flush(): void {
    const payload: SpokenFile = {
      turn: this.turn,
      updated_at: Date.now() / 1000,
      drained_at: this.drainedAt,
      items: [...this.items.values()],
    }
    try {
      mkdirSync(dirname(this.path), { recursive: true })
      const tmp = `${this.path}.tmp`
      writeFileSync(tmp, `${JSON.stringify(payload)}\n`, 'utf8')
      renameSync(tmp, this.path)
    } catch (error: unknown) {
      // 写不进去只影响去重（Python 侧会退回整段播），不该把一轮对话带走
      this.log(`写记录文件失败：${error instanceof Error ? error.message : String(error)}`)
    }
  }
}
