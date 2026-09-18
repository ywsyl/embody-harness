#!/usr/bin/env bash
# rolebus-emit.sh —— 手工往共享 spool 投一条事件：唤醒常驻角色、或做端到端验证。
#
# 走的路径与真发件一致：写 <DSH_HOME>/rolebus/inbox/<target>/<event_id>.json，收件方每
# pollIntervalMs（缺省 200ms）扫一次收件箱。为了不让扫描读到半截文件而被判非法（改名成
# .bad-* 留在收件箱），先写 <event_id>.json.tmp 再 mv 到位 —— 收件侧只看 .json 结尾。
#
# source 必须显式给：本脚本不查事件目录（目录是 TS 模块，shell 侧不重复一份）。投一条
# 目录里真实存在的组合才有意义，例如 cloud → interactor 的 cloud_content_update。
#
# 用法：
#   scripts/rolebus-emit.sh <target-role> <event_type> --source <role> [选项]
#
# 选项：
#   --task-id <id>            信封头 task_id
#   --correlation-id <id>     信封头 correlation_id
#   --severity <v>            info | warning | blocking | safety
#   --event-id <id>           显式 event_id（缺省 manual-<时间戳>-<pid>，去重键）
#   --payload k=v             可重复；v 能解析成 JSON 就按 JSON 存，否则当字符串
#
# 例：
#   scripts/rolebus-emit.sh interactor interaction_request --source orchestrator \
#     --task-id task-7 --payload "question=出门前确认是否带伞"
#
# 可覆盖环境变量：EMBODY / DSH_HOME（与 rolebus-setup.sh 相同）

set -euo pipefail

EMBODY="${EMBODY:-/Users/sylviej/Documents/projects/embody-harness/embody}"
NODE22_BIN="${NODE22_BIN:-/Users/sylviej/Documents/projects/harness/.runtime/node22/bin}"
[ -d "$NODE22_BIN" ] && export PATH="$NODE22_BIN:$PATH"
export CI=true npm_config_verify_deps_before_run=false

if [ $# -lt 2 ]; then
  sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 2
fi

TARGET="$1"; EVENT_TYPE="$2"; shift 2

SOURCE=""
TASK_ID=""
CORRELATION_ID=""
SEVERITY=""
EVENT_ID=""
PAIRS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --source)         SOURCE="${2:-}"; shift 2 ;;
    --task-id)        TASK_ID="${2:-}"; shift 2 ;;
    --correlation-id) CORRELATION_ID="${2:-}"; shift 2 ;;
    --severity)       SEVERITY="${2:-}"; shift 2 ;;
    --event-id)       EVENT_ID="${2:-}"; shift 2 ;;
    --payload)        PAIRS+=("${2:-}"); shift 2 ;;
    -h|--help)        sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "rolebus-emit: 未知参数 $1" >&2; exit 2 ;;
  esac
done

[ -n "$SOURCE" ] || { echo "rolebus-emit: 缺 --source（发出该事件的角色）" >&2; exit 2; }
EVENT_ID="${EVENT_ID:-manual-$(date +%s)-$$}"

DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
ROOT="${ROLEBUS_ROOT:-$DSH_HOME/rolebus}"

# 信封组装与投递在 node 侧完成：JSON 转义、路径拼接、先写 .tmp 再 rename 都在一处。
# 载荷对经 argv 传入（node -e 的 argv 从 1 开始就是后续参数）。
ROLEBUS_EVENT_ID="$EVENT_ID" \
ROLEBUS_EVENT_TYPE="$EVENT_TYPE" \
ROLEBUS_SOURCE="$SOURCE" \
ROLEBUS_TARGET="$TARGET" \
ROLEBUS_TASK_ID="$TASK_ID" \
ROLEBUS_CORRELATION_ID="$CORRELATION_ID" \
ROLEBUS_SEVERITY="$SEVERITY" \
ROLEBUS_ROOT="$ROOT" \
node -e '
const fs = require("node:fs")
const path = require("node:path")

const env = process.env
const eventId = env.ROLEBUS_EVENT_ID
const eventType = env.ROLEBUS_EVENT_TYPE
if (!/^[A-Za-z0-9_-]+$/.test(eventId)) {
  console.error(`rolebus-emit: event_id 只允许 [A-Za-z0-9_-]，收到 ${JSON.stringify(eventId)}`)
  process.exit(2)
}
if (!/^[a-z][a-z0-9_]*$/.test(eventType)) {
  console.error(`rolebus-emit: event_type 必须是 snake_case，收到 ${JSON.stringify(eventType)}`)
  process.exit(2)
}
const SEVERITIES = ["info", "warning", "blocking", "safety"]
const severity = env.ROLEBUS_SEVERITY
if (severity !== "" && !SEVERITIES.includes(severity)) {
  console.error(`rolebus-emit: severity 必须是 ${SEVERITIES.join("/")} 之一，收到 ${JSON.stringify(severity)}`)
  process.exit(2)
}

const envelope = {
  event_id: eventId,
  event_type: eventType,
  source: env.ROLEBUS_SOURCE,
  target: env.ROLEBUS_TARGET,
  timestamp: Math.floor(Date.now() / 1000),
}
if (env.ROLEBUS_TASK_ID !== "") envelope.task_id = env.ROLEBUS_TASK_ID
if (env.ROLEBUS_CORRELATION_ID !== "") envelope.correlation_id = env.ROLEBUS_CORRELATION_ID
if (severity !== "") envelope.severity = severity

const payload = {}
for (const pair of process.argv.slice(1)) {
  const eq = pair.indexOf("=")
  if (eq < 1) {
    console.error(`rolebus-emit: --payload 需要 k=v，收到 ${JSON.stringify(pair)}`)
    process.exit(2)
  }
  const key = pair.slice(0, eq)
  const raw = pair.slice(eq + 1)
  try { payload[key] = JSON.parse(raw) } catch { payload[key] = raw }
}
if (Object.keys(payload).length > 0) envelope.payload = payload

const dir = path.join(env.ROLEBUS_ROOT, "inbox", env.ROLEBUS_TARGET)
fs.mkdirSync(dir, { recursive: true })
const finalPath = path.join(dir, `${eventId}.json`)
if (fs.existsSync(finalPath)) {
  console.error(`rolebus-emit: event_id ${JSON.stringify(eventId)} 已存在（去重键冲突）：${finalPath}`)
  process.exit(1)
}
// 先写 .tmp 再 rename：收件侧只认 .json 结尾，扫到半截文件会把它当非法事件丢掉。
const pendingPath = `${finalPath}.tmp`
fs.writeFileSync(pendingPath, `${JSON.stringify(envelope)}\n`)
fs.renameSync(pendingPath, finalPath)
console.log(`posted ${eventType} ${envelope.source} → ${envelope.target} as ${eventId}`)
console.log(`  ${finalPath}`)
' -- ${PAIRS[@]+"${PAIRS[@]}"}
