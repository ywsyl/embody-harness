#!/usr/bin/env bash
# settings-seed.sh —— 把 configs/settings-model.yaml 展开成 $DSH_HOME/settings.yaml 的模型覆盖段。
#
# 值来源：install/secret.env 的 LOCAL_BASE_URL / LOCAL_MODEL_ds（经 scripts/lib-local-env.sh）；
# X-Device-ID 的 SN 取 configs/paths.py 的 DEVICE_ID（设备配置文件 /etc/cmcc_robot/cmcc_dev.ini
# 的 factory/devSn）——不在这里另编一个设备号，云端 device_id 与模型端点 X-Device-ID 因此是同一个值。
# LOCAL_API_KEY 只以环境变量名被引用（apiKeyEnv: LOCAL_API_KEY），
# 密钥值不写进 settings.yaml，由 credentials-local 在请求时从启动环境解析。
#
# 落盘语义：$DSH_HOME/settings.yaml 不存在则直接写入展开结果；已存在则按顶层命名空间合并
# ——只替换 llm-pi-ai / agent-default-model 两段，保留 ui-* / agent-presets 等其余段落。
#
# 可覆盖环境变量：ENGINE_DIR / NODE_BIN_DIR / DSH_HOME / EMBODY_CONFIGS_DIR（见 configs/scripts-env.sh）

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../configs/scripts-env.sh
source "$REPO_ROOT/configs/scripts-env.sh"   # EMBODY / ENGINE_DIR / NODE_BIN_DIR / CI 等
# shellcheck source=../configs/lib-shell-paths.sh
source "$REPO_ROOT/configs/lib-shell-paths.sh"   # cmcc_path：设备 SN 只认 configs/paths.py 的 DEVICE_ID
embody_check_node settings-seed || exit 1
embody_apply_env
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
TEMPLATE="$EMBODY_CONFIGS_DIR/settings-model.yaml"
SETTINGS="$DSH_HOME/settings.yaml"

NODE="$(command -v node)"

# shellcheck source=lib-local-env.sh
source "$REPO_ROOT/scripts/lib-local-env.sh"

BASE_URL="$(local_env_value LOCAL_BASE_URL)" || { echo "settings-seed: 缺少 LOCAL_BASE_URL（install/secret.env 或环境）" >&2; exit 1; }
MODEL="$(local_env_value LOCAL_MODEL_ds)" || { echo "settings-seed: 缺少 LOCAL_MODEL_ds（install/secret.env 或环境）" >&2; exit 1; }
# 设备 SN：唯一来源是 configs/paths.py 的 DEVICE_ID（它自己读设备配置文件；同一模块的
# DEVICE_CONFIG_FILE 可用环境变量 CMCC_DEVICE_CONFIG 指到另一份）。这里不设第二处覆盖。
DEVICE_ID="$(cmcc_path DEVICE_ID)" || { echo "settings-seed: 取不到 configs/paths.py 的 DEVICE_ID" >&2; exit 1; }
# "unknown" 是 paths.py 读不到设备配置时的回落值：拿它当 X-Device-ID 等于报一个假设备号。
if [ "$DEVICE_ID" = "unknown" ]; then
  echo "settings-seed: 没读到设备 SN（$(cmcc_path DEVICE_CONFIG_FILE) 的 factory/devSn）——" >&2
  echo "    不写一份 X-Device-ID 为假值的 settings.yaml。可用 CMCC_DEVICE_CONFIG 指另一份设备配置。" >&2
  exit 1
fi
BASE_URL="${BASE_URL%/}" # settings 的 baseURL 不带尾斜杠，llm-pi-ai 自行拼 /chat/completions

[ -f "$TEMPLATE" ] || { echo "settings-seed: 模板缺失 $TEMPLATE" >&2; exit 1; }

mkdir -p "$DSH_HOME"
EXPANDED="$(mktemp)"
trap 'rm -f "$EXPANDED"' EXIT
sed -e "s|{{LOCAL_BASE_URL}}|$BASE_URL|g" \
    -e "s|{{LOCAL_MODEL_ds}}|$MODEL|g" \
    -e "s|{{DEVICE_ID}}|$DEVICE_ID|g" \
    "$TEMPLATE" > "$EXPANDED"

if [ -f "$SETTINGS" ]; then
  # merge 用引擎的 yaml 依赖：只有 settings-file 包自己的 node_modules 能解析到它。
  ( cd "$ENGINE_DIR/packages/settings/settings-file" && "$NODE" --input-type=module - "$SETTINGS" "$EXPANDED" <<'NODE'
import { readFileSync, writeFileSync } from 'node:fs'
import { parse, parseDocument } from 'yaml'

// 只替换覆盖文档提供的顶层命名空间，保留 settings.yaml 的其余段落。
const [settingsPath, overlayPath] = process.argv.slice(2)
const overlay = parse(readFileSync(overlayPath, 'utf8'))
const doc = parseDocument(readFileSync(settingsPath, 'utf8'))
for (const [key, value] of Object.entries(overlay)) doc.set(key, value)
const text = doc.toString()
writeFileSync(settingsPath, text.endsWith('\n') ? text : `${text}\n`)
process.stderr.write(`settings-seed: merged ${Object.keys(overlay).join(', ')} into ${settingsPath}\n`)
NODE
  )
else
  cp "$EXPANDED" "$SETTINGS"
  echo "settings-seed: wrote $SETTINGS (namespaces: llm-pi-ai, agent-default-model)"
fi

echo "settings-seed: baseURL=$BASE_URL model=$MODEL deviceId=$DEVICE_ID apiKeyEnv=LOCAL_API_KEY"
