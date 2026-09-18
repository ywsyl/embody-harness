#!/usr/bin/env bash
# settings-seed.sh —— 把 configs/settings-model.yaml 展开成 $DSH_HOME/settings.yaml 的模型覆盖段。
#
# 值来源：configs/exp.bashrc 的 LOCAL_BASE_URL / LOCAL_MODEL_ds（LOCAL_DEVICE_ID 可选，
# 缺省 device_00123555）；LOCAL_API_KEY 只以环境变量名被引用（apiKeyEnv: LOCAL_API_KEY），
# 密钥值不写进 settings.yaml，由 credentials-local 在请求时从启动环境解析。
#
# 落盘语义：$DSH_HOME/settings.yaml 不存在则直接写入展开结果；已存在则按顶层命名空间合并
# ——只替换 llm-pi-ai / agent-default-model 两段，保留 ui-* / agent-presets 等其余段落。
#
# 可覆盖环境变量：ENGINE_DIR / NODE22_BIN / DSH_HOME / EMBODY_CONFIGS_DIR

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EMBODY_CONFIGS_DIR="${EMBODY_CONFIGS_DIR:-$REPO_ROOT/configs}"
export EMBODY_CONFIGS_DIR
ENGINE_DIR="${ENGINE_DIR:-$(cd "$REPO_ROOT/.." && pwd)}"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
TEMPLATE="$EMBODY_CONFIGS_DIR/settings-model.yaml"
SETTINGS="$DSH_HOME/settings.yaml"

NODE22_BIN="${NODE22_BIN:-$(cd "$REPO_ROOT/../.." && pwd)/harness/.runtime/node22/bin}"
[ -d "$NODE22_BIN" ] && export PATH="$NODE22_BIN:$PATH"
NODE="$(command -v node)"

# shellcheck source=lib-local-env.sh
source "$REPO_ROOT/scripts/lib-local-env.sh"

BASE_URL="$(local_env_value LOCAL_BASE_URL)" || { echo "settings-seed: 缺少 LOCAL_BASE_URL（configs/exp.bashrc 或环境）" >&2; exit 1; }
MODEL="$(local_env_value LOCAL_MODEL_ds)" || { echo "settings-seed: 缺少 LOCAL_MODEL_ds（configs/exp.bashrc 或环境）" >&2; exit 1; }
DEVICE_ID="$(local_env_value LOCAL_DEVICE_ID || true)"
DEVICE_ID="${DEVICE_ID:-device_00123555}"
BASE_URL="${BASE_URL%/}" # settings 的 baseURL 不带尾斜杠，llm-pi-ai 自行拼 /chat/completions

[ -f "$TEMPLATE" ] || { echo "settings-seed: 模板缺失 $TEMPLATE" >&2; exit 1; }

mkdir -p "$DSH_HOME"
EXPANDED="$(mktemp)"
trap 'rm -f "$EXPANDED"' EXIT
sed -e "s|{{LOCAL_BASE_URL}}|$BASE_URL|g" \
    -e "s|{{LOCAL_MODEL_ds}}|$MODEL|g" \
    -e "s|{{LOCAL_DEVICE_ID}}|$DEVICE_ID|g" \
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
