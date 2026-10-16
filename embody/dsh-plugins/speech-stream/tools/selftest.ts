/**
 * speech-stream 的纯函数自测：不起 ROS、不起 TTS、不起 dsh。
 *
 * 为什么要有它：这套东西的正确性全在三块纯逻辑里——参数分片的增量解码（`toolArgs.ts`）、
 * 切句与净化（`sentence.ts`）、以及认帧/记账/重试去重的状态机（`machine.ts`）。三者都不碰
 * 引擎也不碰 IO，所以能在这里用几行断言跑完；而真机上要验的就是「首句早了多少」那点手感，
 * 出错了不好定位。引擎的 vitest 不覆盖 `dsh-plugins/**`（不在任何 include 里），这就是本树
 * 的测试落点。
 *
 * 跑法：`npx --no-install tsx tools/selftest.ts`（本文件不在 tsconfig 的 include 里，
 * 不参与 `tsc -b`；tsx 只剥类型不做检查，所以帧字面量用断言构造成帧类型）。
 * @module @embody/dsh-speech-stream/tools/selftest
 */

import type { AssistantStreamFrame } from '@deepseek-ai/dsh-agent'
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { createServer, type Server, type Socket } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { SpeechMachine, type SpeechSink } from '../src/machine.ts'
import { sanitize, splitChunks, type FenceState } from '../src/sentence.ts'
import { readTextField } from '../src/toolArgs.ts'
import { Speaker, type DeliveryMode, type Logger } from '../src/tts.ts'

/** 断言失败数：非零即退出码非零。 */
let failures = 0

/**
 * 记一条断言结果。
 * @param label - 用例名。
 * @param ok - 是否通过。
 * @param detail - 失败时的现场（成功时不打）。
 */
function check(label: string, ok: boolean, detail = ''): void {
  if (ok) {
    console.log(`  ok   ${label}`)
    return
  }
  failures += 1
  console.log(`  FAIL ${label}${detail === '' ? '' : `\n       ${detail}`}`)
}

/**
 * 比较两个值（用 JSON 打印差异，够用且不用引断言库）。
 * @param label - 用例名。
 * @param actual - 实际值。
 * @param expected - 期望值。
 */
function same(label: string, actual: unknown, expected: unknown): void {
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  check(label, a === e, `actual   ${a}\nexpected ${e}`)
}

/** 假出口：把状态机交出来的每一件事记成一行文本，顺便盯住记账不变量。 */
class FakeSink implements SpeechSink {
  /** 按顺序记下的 `turn` / `record` / `speak` 事件。 */
  readonly events: string[] = []
  /** 记录条数（自测里用得着）。 */
  readonly records: Array<{ turn: number; index: number; tool: string; text: string; spoken: number }> = []
  /** 出声的句子。 */
  readonly spoken: string[] = []
  /** 记账不变量的违反信息（`spoken` 越界、`text` 前缀不稳）。 */
  readonly violations: string[] = []
  private lastText: string | undefined

  beginTurn(turn: number): void {
    this.events.push(`turn=${turn}`)
    this.lastText = undefined
  }

  record(item: { turn: number; index: number; tool: string; text: string; spoken: number }): void {
    this.events.push(`record tool=${item.tool} spoken=${item.spoken}/${item.text.length}`)
    this.records.push(item)
    if (item.spoken < 0 || item.spoken > item.text.length) {
      this.violations.push(`spoken=${item.spoken} 越界（text ${item.text.length} 字）`)
    }
    if (this.lastText !== undefined && !item.text.startsWith(this.lastText)) {
      this.violations.push(`同一次调用的 text 前缀变了：${JSON.stringify(this.lastText)} → ${JSON.stringify(item.text)}`)
    }
    this.lastText = item.text
  }

  speak(text: string): void {
    this.events.push(`speak ${JSON.stringify(text)}`)
    this.spoken.push(text)
  }

  log(): void {}
}

/** 帧序号计数器（`start`/`chunk`/`end` 共用，只影响日志）。 */
let seq = 0

/**
 * 造一个 `start` 帧。
 * @param turn - 轮次号。
 * @param revision - attempt 修订号（同轮重试用）。
 * @returns 帧。
 */
function startFrame(turn: number, revision = 1): AssistantStreamFrame {
  seq += 1
  return { type: 'start', attemptId: 'a1', revision, turn, step: 1 } as unknown as AssistantStreamFrame
}

/**
 * 造一个工具参数分片帧。
 * @param index - 工具调用在消息里的块下标。
 * @param delta - 本次到达的 `argumentsDelta`。
 * @param name - 工具名（只在首片带，与真实流量一致）。
 * @returns 帧。
 */
function chunkFrame(index: number, delta: string, name?: string): AssistantStreamFrame {
  seq += 1
  return {
    type: 'chunk',
    attemptId: 'a1',
    revision: 1,
    index: seq,
    time: seq,
    chunk: { type: 'tool-call-delta', index, id: 'call-1', ...(name === undefined ? {} : { name }), argumentsDelta: delta },
  } as unknown as AssistantStreamFrame
}

/**
 * 造一个 `end` 帧。
 * @param outcome - 结算方式。
 * @returns 帧。
 */
function endFrame(outcome: 'message' | 'attempt' | 'abandoned'): AssistantStreamFrame {
  seq += 1
  return {
    type: 'end',
    attemptId: 'a1',
    revision: 1,
    index: seq,
    outcome: outcome === 'abandoned' ? { kind: 'abandoned' } : { kind: 'committed', eventType: outcome === 'message' ? 'assistant/message' : 'assistant/attempt', seq },
  } as unknown as AssistantStreamFrame
}

/**
 * 把一次 `speak` 的参数分片喂给状态机（工具名只随首片）。
 * @param machine - 状态机。
 * @param fragments - 参数分片序列。
 * @param index - 块下标。
 * @param name - 工具名。
 */
function feed(machine: SpeechMachine, fragments: string[], index = 0, name = 'speak'): void {
  fragments.forEach((delta, i) => { machine.onFrame(chunkFrame(index, delta, i === 0 ? name : undefined)) })
}

console.log('toolArgs：参数分片的增量解码')

same('整段一次到达', readTextField('{"text":"你好。","play_now":true}'), { text: '你好。', complete: true })
same('键序颠倒（play_now 在前）', readTextField('{"play_now":true,"text":"你好。"}'), { text: '你好。', complete: true })
same('停在键名中间', readTextField('{"te'), { text: '', complete: false })
same('停在值中间（无标点）', readTextField('{"text":"你好'), { text: '你好', complete: false })
same('停在转义符中间（\\u 半截）', readTextField('{"text":"你好\\u30'), { text: '你好', complete: false })
same('\\uXXXX 解成中文标点', readTextField('{"text":"你好\\u3002"}'), { text: '你好。', complete: true })
same('转义引号与换行', readTextField('{"text":"他说\\"行\\"\\n好"}'), { text: '他说"行"\n好', complete: true })
same('text 不是字符串', readTextField('{"text":5}'), { text: '', complete: true })
same('嵌套对象里的 text 不算', readTextField('{"a":{"text":"x"},"text":"y"}'), { text: 'y', complete: true })
// 参数永远是对象：认到顶层数组说明参数坏了，此时既取不出 text 也不该冲刷尾巴
same('顶层数组不算', readTextField('[{"text":"x"}]'), { text: '', complete: false })

console.log('sentence：切句与净化')

same('句末标点连用带收尾引号', (() => {
  const fence: FenceState = { open: false }
  return splitChunks('真的吗？！”然后走了。', 60, fence).chunks
})(), ['真的吗？！”', '然后走了。'])
same('软边界在逗号处断，尾巴留着', (() => {
  const fence: FenceState = { open: false }
  const text = '这是很长的一句话，接着说了一大堆，还有更多内容在这里'
  const result = splitChunks(text, 12, fence)
  return [result.chunks, result.rest]
})(), [['这是很长的一句话，', '接着说了一大堆，'], '还有更多内容在这里'])
same('窗口内没有停顿就在上限处硬断', (() => {
  const fence: FenceState = { open: false }
  return splitChunks('一二三四五六七八九十一二三四五六七八九十', 12, fence).chunks
})(), ['一二三四五六七八九十一二'])
same('围栏内不播且不留', (() => {
  const fence: FenceState = { open: false }
  return splitChunks('看这个```\ncode()\n```完了。', 60, fence).chunks
})(), ['看这个', '完了。'])
same('净化 markdown 残余', sanitize('# 标题\n**粗体** 与 `代码`'), '标题 粗体 与 代码')

console.log('machine：增量出声、记账、重试去重')

{
  const sink = new FakeSink()
  const machine = new SpeechMachine(sink, 60)
  machine.onFrame(startFrame(1))
  feed(machine, ['{"text":"好的，', '我这就', '去。"', ',"play_now":true}'])
  machine.onFrame(endFrame('message'))
  same('分片到达也能成句出声', sink.spoken, ['好的，我这就去。'])
  same('记账不变量', sink.violations, [])
  same('收尾记录 spoken=全文', sink.records.at(-1), { turn: 1, index: 0, tool: 'speak', text: '好的，我这就去。', spoken: 8 })
}

{
  const sink = new FakeSink()
  const machine = new SpeechMachine(sink, 60)
  machine.onFrame(startFrame(1))
  feed(machine, ['{"text":"第一句。第二句。",'])
  same('一次到达多句：全部成句', sink.spoken, ['第一句。', '第二句。'])
  machine.onFrame(endFrame('message'))
}

{
  const sink = new FakeSink()
  const machine = new SpeechMachine(sink, 60)
  machine.onFrame(startFrame(1))
  feed(machine, ['{"text":"新的回答。还有半'])
  machine.onFrame(endFrame('attempt'))
  same('被中断：只播成句的，半句丢掉', sink.spoken, ['新的回答。'])
  same('被中断：半句也记进已处理（Python 不再补）', sink.records.at(-1)?.spoken, 8)
}

{
  const sink = new FakeSink()
  const machine = new SpeechMachine(sink, 60)
  machine.onFrame(startFrame(1))
  feed(machine, ['{"text":"第一句。第二'])
  machine.onFrame(endFrame('abandoned'))
  const afterAbandon = sink.spoken.length
  machine.onFrame(startFrame(1, 2))
  feed(machine, ['{"text":"第一句。第二句。"}'])
  machine.onFrame(endFrame('message'))
  same('重试补发不重复前一次已播', sink.spoken.slice(afterAbandon), ['句。'])
  same('记账不变量', sink.violations, [])
}

{
  const sink = new FakeSink()
  const machine = new SpeechMachine(sink, 60)
  machine.onFrame(startFrame(1))
  feed(machine, ['{"text":"第一句。",'])
  machine.onFrame(endFrame('message'))
  machine.onFrame(startFrame(2))
  feed(machine, ['{"text":"新一轮。",'])
  machine.onFrame(endFrame('message'))
  same('新一轮照样出声', sink.spoken, ['第一句。', '新一轮。'])
}

{
  const sink = new FakeSink()
  const machine = new SpeechMachine(sink, 60)
  machine.onFrame(startFrame(1))
  feed(machine, ['{"play_now":true,'])
  feed(machine, ['"text":"后来才写的。"}'])
  same('键序颠倒也能出声', sink.spoken, ['后来才写的。'])
  machine.onFrame(endFrame('message'))
}

{
  const sink = new FakeSink()
  const machine = new SpeechMachine(sink, 60)
  machine.onFrame(startFrame(1))
  feed(machine, ['{"text":"你好。"}'], 0, 'listen')
  same('非播报工具一个字都不播', sink.spoken, [])
  feed(machine, ['{"text":"我听着。"}'], 1, 'speak_listen')
  same('speak_listen 同一套逻辑', sink.spoken, ['我听着。'])
  machine.onFrame(endFrame('message'))
}

// =============================================================================
// Speaker（`src/tts.ts`）：这里用一个**假 socket 服务**验交接口径与回退路径。
// 唯一会起子进程的是回退那条用例，起的是本测试自己写的假脚本（**不是** run_tts_speak.sh），
// 所以这一套跑起来不出声、不碰 ROS。
// =============================================================================

/** 等一会儿（异步断言之间的让路）。 */
const sleep = (ms: number): Promise<void> => new Promise((resolve) => { setTimeout(resolve, ms) })

/**
 * 等到条件成立（超时后照样返回，由断言去判）。
 * @param predicate - 条件。
 * @param timeoutMs - 上限毫秒。
 */
async function until(predicate: () => boolean, timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline && !predicate()) await sleep(10)
}

/** 假常驻服务：收 `speak` 行、按脚本回 `accepted`、可广播 `drained`。 */
class FakeTts {
  /** 按到达顺序记下的句子（交付顺序的唯一证据）。 */
  readonly received: string[] = []
  private readonly sockets = new Set<Socket>()
  private server: Server | undefined

  /**
   * @param acceptMs - 隔多久回 `accepted`（0 = 立即）。
   * @param autoDrainMs - 隔多久自动广播一次 `drained`；不给就只手动广播。
   */
  constructor(private readonly opts: { acceptMs?: number; autoDrainMs?: number } = {}) {}

  /**
   * 在一个 unix socket 路径上开始服务。
   * @param path - socket 路径。
   */
  async listen(path: string): Promise<void> {
    const server = createServer((socket) => { this.serve(socket) })
    this.server = server
    await new Promise<void>((resolve) => { server.listen(path, resolve) })
  }

  /** 广播一次 `drained`（模拟设备把交出去的都放完了）。 */
  broadcastDrained(): void {
    for (const socket of this.sockets) {
      socket.write(`${JSON.stringify({ event: 'drained', at: Date.now() / 1000 })}\n`)
    }
  }

  /** 把所有连接踹掉（模拟服务中途挂掉）。 */
  dropConnections(): void {
    for (const socket of this.sockets) socket.destroy()
  }

  /** 收摊。 */
  async close(): Promise<void> {
    this.dropConnections()
    const server = this.server
    this.server = undefined
    if (server !== undefined) await new Promise<void>((resolve) => { server.close(() => { resolve() }) })
  }

  /**
   * 一条连接：逐行收请求、逐条回 `accepted`。
   * @param socket - 连接。
   */
  private serve(socket: Socket): void {
    this.sockets.add(socket)
    socket.setEncoding('utf8')
    socket.on('close', () => { this.sockets.delete(socket) })
    let buffer = ''
    socket.on('data', (chunk: string) => {
      buffer += chunk
      let index = buffer.indexOf('\n')
      while (index >= 0) {
        const line = buffer.slice(0, index)
        buffer = buffer.slice(index + 1)
        this.handle(socket, line)
        index = buffer.indexOf('\n')
      }
    })
  }

  /**
   * 处理一条请求行。
   * @param socket - 来源连接。
   * @param line - 一行 JSON。
   */
  private handle(socket: Socket, line: string): void {
    if (line.trim() === '') return
    const message = JSON.parse(line) as { id: number; op: string; text?: string }
    if (message.op !== 'speak') return
    this.received.push(message.text ?? '')
    const reply = (): void => {
      socket.write(`${JSON.stringify({ id: message.id, event: 'accepted', section_id: 's', error_code: 0 })}\n`)
    }
    const acceptMs = this.opts.acceptMs ?? 0
    if (acceptMs > 0) setTimeout(reply, acceptMs)
    else reply()
    const drainMs = this.opts.autoDrainMs
    if (drainMs !== undefined) setTimeout(() => { this.broadcastDrained() }, drainMs)
  }
}

/**
 * 假 TTS 脚本：把收到的位置参数追加到一份日志里，然后立刻退出 0（**不出声**）。
 * @param dir - 临时目录。
 * @returns 脚本路径与「读已收到的参数表」的函数（每次起脚本一条，按顺序）。
 */
function fakeScript(dir: string): { path: string; calls: () => string[][] } {
  const logPath = join(dir, 'spawn.jsonl')
  const script = join(dir, 'fake-tts.sh')
  writeFileSync(script, `#!/bin/bash\npython3 -c 'import json,sys;open("${logPath}","a").write(json.dumps(sys.argv[1:])+"\\n")' "$@"\n`, 'utf8')
  chmodSync(script, 0o755)
  return {
    path: script,
    calls: () => {
      try {
        return readFileSync(logPath, 'utf8').trim().split('\n')
          .filter((line) => line !== '')
          .map((line) => JSON.parse(line) as string[])   // 脚本那边 json.dumps 转义了非 ASCII
      } catch {
        return []                                    // 还没起过脚本
      }
    },
  }
}

/** 收日志的假出口。 */
function collector(): { lines: string[]; log: Logger } {
  const lines: string[] = []
  return { lines, log: (message: string) => { lines.push(message) } }
}

/**
 * Speaker 的四组用例（假服务、假脚本，见上方说明）。
 */
async function speakerTests(): Promise<void> {
  const dir = mkdtempSync(join(tmpdir(), 'speech-selftest-'))
  const stub = fakeScript(dir)

  // ---- ① pipeline：交一句就交下一句，不靠 drained ------------------------------
  {
    const sock = join(dir, 'pipeline.sock')
    const fake = new FakeTts()
    await fake.listen(sock)
    const { lines, log } = collector()
    let drains = 0
    const speaker = new Speaker(stub.path, sock, 'pipeline', log, () => { drains += 1 })
    speaker.enqueue('第一句。')
    await until(() => fake.received.length === 1)
    check('pipeline：连上服务并用 socket 交句（没有起脚本）', fake.received.length === 1 && stub.calls().length === 0)
    check('pipeline：日志说明走了常驻服务', lines.some((line) => line.includes('已连上常驻 TTS 服务')))
    speaker.enqueue('第二句。')
    speaker.enqueue('第三句。')
    await until(() => fake.received.length === 3)
    same('pipeline：三句按序交出（不等 drained）', fake.received, ['第一句。', '第二句。', '第三句。'])
    check('pipeline：还没收到 drained 就不报见底', drains === 0)
    fake.broadcastDrained()
    await until(() => drains === 1)
    check('pipeline：收到 drained → 报一次见底（markDrained）', drains === 1)
    // 之后每一条 drained 都是真的「交出去的都没在放了」（服务端要等它自己的队列见底才广播），
    // 所以照报不误：drained_at 单调前进，多报一次不影响读方（plugin_tools/audio_tools.py 比的是 >=）。
    fake.broadcastDrained()
    await until(() => drains === 2)
    check('pipeline：又一条 drained → 再报一次（它这会儿同样是真的）', drains === 2)
    speaker.enqueue('第四句。')
    await until(() => fake.received.length === 4)
    await sleep(30)
    check('pipeline：新交接不靠旧 drained 报见底', drains === 2)
    fake.broadcastDrained()
    await until(() => drains === 3)
    check('pipeline：新交接之后的 drained 再报', drains === 3)
    speaker.dispose()
    await fake.close()
  }

  // ---- ② serial：等这一句播完（drained）才交下一句 -----------------------------
  {
    const sock = join(dir, 'serial.sock')
    const fake = new FakeTts({ autoDrainMs: 60 })
    await fake.listen(sock)
    const { log } = collector()
    let drains = 0
    const speaker = new Speaker(stub.path, sock, 'serial', log, () => { drains += 1 })
    speaker.enqueue('甲。')
    speaker.enqueue('乙。')
    await until(() => fake.received.length === 1)
    await sleep(20)                                  // 远小于 autoDrainMs：这段时间里不该交第二句
    same('serial：drained 之前只交第一句', fake.received, ['甲。'])
    check('serial：那一刻还没报见底', drains === 0)
    await until(() => fake.received.length === 2)
    same('serial：drained 之后才交第二句', fake.received, ['甲。', '乙。'])
    await until(() => drains === 1)
    check('serial：两句都交完、服务见底后才报一次见底', drains === 1)
    speaker.dispose()
    await fake.close()
  }

  // ---- ③ 见底先到、受理后到：serial 的拦阻不能只看「等没等到 drained」-------------
  {
    const sock = join(dir, 'drained-first.sock')
    // 40ms 就广播 drained、200ms 才回 accepted —— 设备先播完一句、应答才回来是实测见过的
    const fake = new FakeTts({ acceptMs: 200, autoDrainMs: 40 })
    await fake.listen(sock)
    const { log } = collector()
    const speaker = new Speaker(stub.path, sock, 'serial', log, () => {})
    speaker.enqueue('甲。')
    await until(() => fake.received.length === 1)
    await sleep(300)                                  // drained 与 accepted 都已到达
    speaker.enqueue('乙。')
    await until(() => fake.received.length === 2, 500)
    same('serial：见底先到也不卡住（换一句照样交出去）', fake.received, ['甲。', '乙。'])
    speaker.dispose()
    await fake.close()
  }

  // ---- ④ 服务连不上：整体退回逐句 spawn ---------------------------------------
  {
    const { lines, log } = collector()
    let drains = 0
    const speaker = new Speaker(stub.path, join(dir, 'missing.sock'), 'pipeline', log, () => { drains += 1 })
    speaker.enqueue('回退的一句。')
    await until(() => stub.calls().length === 1)
    same('回退：起了脚本并把它交给脚本（文本 / multi_wheel / play_now / wait_done）',
      stub.calls(), [['回退的一句。', 'false', 'false', 'true']])
    check('回退：日志说明服务不可用', lines.some((line) => line.includes('退回逐句 spawn')))
    await until(() => drains === 1)
    check('回退：脚本退出 → 报见底', drains === 1)
    speaker.dispose()
  }

  // ---- ⑤ 中途断开：在飞的那句不重发，剩下的退回脚本 ---------------------------
  {
    const stub2 = fakeScript(mkdtempSync(join(tmpdir(), 'speech-selftest-drop-')))
    const sock = join(dir, 'drop.sock')
    const fake = new FakeTts({ acceptMs: 50 })
    await fake.listen(sock)
    const { lines, log } = collector()
    const speaker = new Speaker(stub2.path, sock, 'pipeline', log, () => {})
    speaker.enqueue('断线前那句。')
    await until(() => fake.received.length === 1)
    speaker.enqueue('断线后那句。')
    fake.dropConnections()
    await until(() => stub2.calls().length === 1)
    same('中途断开：在飞的那句不重发，队里那句退回脚本',
      stub2.calls(), [['断线后那句。', 'false', 'false', 'true']])
    check('中途断开：日志说明为什么', lines.some((line) => line.includes('不再重发')))
    speaker.dispose()
    await fake.close()
  }
}

console.log('Speaker：常驻服务（socket）/ serial / 见底先到 / 回退逐句 spawn')

await speakerTests()

console.log(failures === 0 ? '\n全部通过' : `\n${failures} 条失败`)
process.exitCode = failures === 0 ? 0 : 1
