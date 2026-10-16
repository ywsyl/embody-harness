/**
 * 从工具参数的原始 JSON 片段里增量取出 `text` 字段的明文。
 *
 * 为什么是这一层：模型每写几个字，引擎就把 `call.function.arguments` 的那一片原样发成一条
 * `tool-call-delta`（见 packages/llm/llm-deepseek/src/translate.ts），所以「要说什么」在
 * 工具调用被派发之前就是可读的——这正是流式出声的全部依据。工具调用本身要等整条消息结算
 * 才执行（packages/core/agent-loop/src/agent.ts），等它就晚了。
 *
 * 为什么自己扫而不是 JSON.parse 前缀：片段可能停在任何位置——键的名字中间、转义符中间、
 * `\uXXXX` 中间。JSON.parse 只能整体成功或整体失败，而我们要的是「已经确定的那部分」。
 * 所以这里逐字符走一遍，只做两件事：找**顶层**的 `text` 键，然后按 JSON 字符串规则解码
 * 它的值；解码到不完整的转义序列时就停在那里，等下一片。
 *
 * 键序不定（模型可能先给 `play_now`）、`text` 可能不是字符串（那就当作没有），两种都由
 * 这个扫描器自然处理；调用方拿到的 `text` 前缀稳定——下一次调用只会更长，且开头不变。
 * @module @embody/dsh-speech-stream/toolArgs
 */

/** 一次扫描的结果。 */
export interface TextField {
  /**
   * `text` 字段已经能确定的那部分明文。**前缀稳定**：同一段原始片段多次扫描，结果只会
   * 变长，已给出的部分不会改写。字段不存在或不是字符串时为空串。
   */
  readonly text: string
  /** 顶层对象已闭合（参数写完了）——调用方据此冲刷最后那个没有句末标点的半句。 */
  readonly complete: boolean
}

/** 一个 JSON 字符串的扫描结果：解出的明文、结束下标、是否见到了收尾引号。 */
interface StringScan {
  /** 已解出的明文（未闭合时是到当前为止的部分）。 */
  readonly value: string
  /** 下一个待扫描字符的下标（未闭合时为片段长度）。 */
  readonly end: number
  /** 是否见到了收尾引号。 */
  readonly closed: boolean
}

/** 十六进制半字节。`\uXXXX` 的合法性判断要用。 */
const HEX = /^[0-9a-fA-F]{4}$/

/**
 * 扫一个 JSON 字符串字面量（`start` 指向开引号）。
 *
 * 转义解到一半就断片时**不回退**：把已解出的部分交出去、`closed: false`，剩下的等下一位
 * 调用者把新片段接上再解。宁可这一轮少播一个字，也不能把 `。` 当成六个字符念出来。
 * @param raw - 原始片段。
 * @param start - 开引号的下标。
 * @returns 明文、结束下标与是否闭合。
 */
function scanString(raw: string, start: number): StringScan {
  let out = ''
  let i = start + 1
  while (i < raw.length) {
    const ch = raw[i]!
    if (ch === '\\') {
      const next = raw[i + 1]
      if (next === undefined) return { value: out, end: raw.length, closed: false }
      if (next === 'u') {
        const hex = raw.slice(i + 2, i + 6)
        if (hex.length < 4) return { value: out, end: raw.length, closed: false }
        if (!HEX.test(hex)) {
          // 不是合法转义：按 JSON 的话这一段就是错的。当字面量收下，不猜。
          out += next
          i += 2
          continue
        }
        out += String.fromCharCode(Number.parseInt(hex, 16))
        i += 6
        continue
      }
      switch (next) {
        case 'n': out += '\n'; break
        case 't': out += '\t'; break
        case 'r': out += '\r'; break
        case 'b': out += '\b'; break
        case 'f': out += '\f'; break
        default: out += next
      }
      i += 2
      continue
    }
    if (ch === '"') return { value: out, end: i + 1, closed: true }
    out += ch
    i += 1
  }
  return { value: out, end: raw.length, closed: false }
}

/**
 * 从累积的参数片段里取出顶层 `text` 字段的明文。
 * @param raw - 该次工具调用到目前为止收到的全部 `argumentsDelta` 拼起来的内容。
 * @returns 已能确定的 `text` 明文与「参数是否已写完」。
 */
export function readTextField(raw: string): TextField {
  let depth = 0
  let atTop = false
  let expectKey = false
  let key: string | undefined
  let value: string | undefined
  let complete = false
  let i = 0
  while (i < raw.length) {
    const ch = raw[i]!
    if (ch === '"') {
      const scan = scanString(raw, i)
      if (depth === 1 && expectKey) {
        key = scan.value
        expectKey = false
      } else if (depth === 1 && key === 'text' && value === undefined) {
        value = scan.value
        key = undefined
      }
      i = scan.end
      if (!scan.closed) return { text: value ?? '', complete: false }
      continue
    }
    if (ch === '{' || ch === '[') {
      depth += 1
      // 顶层对象的第一个字符串是键：这里不置位的话，`{"text": …` 会因为「当前不在键位」
      // 被当成值跳过，整段参数一个字都取不出来。
      if (depth === 1 && ch === '{') {
        atTop = true
        expectKey = true
      }
      i += 1
      continue
    }
    if (ch === '}' || ch === ']') {
      depth -= 1
      if (depth === 0 && atTop) complete = true
      i += 1
      continue
    }
    if (ch === ',') {
      if (depth === 1) expectKey = true
      i += 1
      continue
    }
    i += 1
  }
  return { text: value ?? '', complete }
}
