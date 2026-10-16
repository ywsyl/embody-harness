/**
 * 播报稿切句：把参数流里逐片到达的 `text` 值切成「一句一次 TTS 调用」的粒度。
 *
 * 为什么粒度上限就是一句：`/audio_center/play_tts` 一次只收一整串文本，没有增量接口
 * （见 skills/tts-speak/SKILL.md），所以能提前的只有「句」这一级——第一句生成完就出声，
 * 而不是等整段写完。切得再细（半句）服务侧也吃不进去。
 *
 * 两种边界：
 *   · **硬边界**：句末标点（允许连续重复，如「？！」）之后即成一 句，紧随的收尾引号/括号
 *     一并带进来，免得下一句以引号开头。
 *   · **软边界**：一个句子长到 maxChars 还没说完时，在语气停顿（逗号、顿号、冒号）处断开。
 *     没有停顿就在上限处硬断。这一条是为了不让「一句话说了 80 个字」把出声拖到最后。
 *
 * 还有两件净化：代码围栏（``` 之间的整段）**不播**——播报稿里写代码/表格是模型偶尔的
 * 串味，念出来毫无意义；以及去掉 markdown 的强调符与行首标记。
 *
 * 本模块是纯函数、无 IO、不依赖 cordis：切句与净化是这套东西里最容易出错、也最该单独
 * 验的一部分（工具层与插件层都不好测，见 tools/selftest.ts）。
 * @module @embody/dsh-speech-stream/sentence
 */

/** 句末标点：出现在这些字符之后即认为一句说完。 */
const TERMINATORS = new Set(['。', '！', '？', '!', '?', '；', ';', '…'])

/** 句末标点后允许跟随的收尾符号：跟着就一起带进这一句。 */
const CLOSERS = new Set(['”', '"', '）', ')', '】', ']', '」', '』', '’', "'", '》', '〉'])

/** 软切点：超长句子里可以断句的地方（语气停顿）。 */
const SOFT_BREAKS = new Set(['，', ',', '、', '：', ':'])

/** 代码围栏标记。 */
const FENCE = '```'

/** 跨片到达的围栏状态：`true` 表示当前正处在围栏内（这段文字不播）。 */
export interface FenceState {
  /** 是否已进入围栏。调用方持有一份，逐次切句时传进来累进。 */
  open: boolean
}

/** 一次切句的结果。 */
export interface SplitResult {
  /** 现在就可以交给 TTS 的句子（已净化、已去空）。 */
  readonly chunks: readonly string[]
  /** 还没成句的尾巴：与下一次的增量拼起来再切。 */
  readonly rest: string
}

/** 去掉 markdown 残余与冗余空白：这些字符念出来没有意义，留在文本里只会占用发音。 */
export function sanitize(text: string): string {
  return text
    .replace(/[`*]/g, '')
    .replace(/^[ \t]*[#>-]+[ \t]*/gm, '')
    .replace(/\s+/g, ' ')
    .trim()
}

/** 净化后非空才收进结果；空片段（纯标点、纯标记）不该占一次 TTS 调用。 */
function collect(out: string[], raw: string): void {
  const text = sanitize(raw)
  if (text !== '') out.push(text)
}

/**
 * 在一个**围栏外**的片段里尽可能多地切出完整的句子。
 * @param text - 不含围栏标记的片段。
 * @param maxChars - 软边界阈值（字数）。
 * @param out - 切出的句子追加到这里。
 * @returns 片段末尾没成句的尾巴。
 */
function takeSentences(text: string, maxChars: number, out: string[]): string {
  let start = 0
  let i = 0
  while (i < text.length) {
    if (TERMINATORS.has(text[i]!)) {
      let j = i
      while (j + 1 < text.length && TERMINATORS.has(text[j + 1]!)) j += 1
      while (j + 1 < text.length && CLOSERS.has(text[j + 1]!)) j += 1
      collect(out, text.slice(start, j + 1))
      i = j + 1
      start = i
      continue
    }
    i += 1
    if (i - start < maxChars) continue
    // 到软边界上限：优先在最近的语气停顿处断，找不到就硬断在当前下标。
    const window = text.slice(start, i)
    let cut = -1
    for (let k = window.length - 1; k >= 0; k -= 1) {
      if (SOFT_BREAKS.has(window[k]!)) {
        cut = k
        break
      }
    }
    if (cut >= 0) {
      collect(out, window.slice(0, cut + 1))
      start += cut + 1
      i = start
    } else {
      collect(out, window)
      start = i
    }
  }
  return text.slice(start)
}

/**
 * 切句：吃下当前缓冲，吐出可播的句子与剩下的尾巴。
 *
 * 围栏语义：`FenceState` 跨次累进。围栏**内**的文字全部丢弃（既不播也不留进 rest——
 * 它不可能再变成正文）。围栏开始前残留的半句会当场吐出：它后面紧跟围栏，不会再有内容
 * 把它补完。
 * @param buffer - 已经解码出来、还没播出去的文本。
 * @param maxChars - 软边界阈值（字数，正整数）。
 * @param fence - 跨次累进的围栏状态。
 * @returns 可以播的句子与剩余尾巴。
 */
export function splitChunks(buffer: string, maxChars: number, fence: FenceState): SplitResult {
  const chunks: string[] = []
  let rest = ''
  let cursor = 0
  while (cursor < buffer.length) {
    const at = buffer.indexOf(FENCE, cursor)
    if (at === -1) {
      // 剩下的都在围栏外：切句，尾巴留到下次。围栏内：整段丢。
      if (!fence.open) rest = takeSentences(buffer.slice(cursor), maxChars, chunks)
      break
    }
    if (!fence.open) {
      // 围栏之前那段：半句留在手里也不会再有下文，当场吐掉。
      collect(chunks, takeSentences(buffer.slice(cursor, at), maxChars, []))
      fence.open = true
    } else {
      fence.open = false
    }
    cursor = at + FENCE.length
  }
  return { chunks, rest }
}
