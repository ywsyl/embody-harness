#!/usr/bin/env bash
# rolebus-setup.sh —— 里程碑 0：把“每角色独立 profile/进程”的配置文件分层物化到 DSH_HOME。
#
# 对每个角色：
#   1) 若 $DSH_HOME/profiles/<role> 尚不存在 → `dsh plugin --profile <role> add <rolebus dir>`
#      （引擎会初始化该 profile：bundles = [@deepseek-ai/dsh-base, @embody/dsh-rolebus]，pnpm link 本地目录）
#   2) 把本仓库 role-profiles/<role>/cordis.patch.yml 覆盖为 profile 自身的 cordis.patch.yml
#      （这是“profile 层”：改写 base 的 system-prompt.persona 与 rolebus 的角色身份）
#
# 幂等：profile 已存在且 bundle 已含 rolebus 时跳过 add，只刷新 profile 层文件。
#
# 可覆盖环境变量：
#   ENGINE_DIR  引擎（默认 = 本仓库父目录，即 fork 根 projects/embody-harness）
#   DSH_HOME    角色 profile 根（默认 $HOME/.dsh；隔离验证时传临时目录）
#   NODE22_BIN  本机 node22 运行时 bin（默认 = 旧 harness/.runtime，可覆盖）
# 运行配方见 ENGINE-VERSION.md / 本机 node22+CI=true。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_ENGINE="$(cd "$REPO_ROOT/.." && pwd)"   # fork 根即引擎（embody/ 的父目录）
ENGINE_DIR="${ENGINE_DIR:-$DEFAULT_ENGINE}"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
ROLEBUS_DIR="$REPO_ROOT/dsh-plugins/rolebus"
ROLES="${ROLES:-interactor orchestrator}"

# 本机配方：node22 前置到 PATH（pnpm 会解析 scripts 里的 node 为 node22），
# CI=true 避开 lefthook 安装器，跳过 pnpm 每次跑前的 deps 检查。
NODE22_BIN="${NODE22_BIN:-$(cd "$REPO_ROOT/../.." && pwd)/harness/.runtime/node22/bin}"
[ -d "$NODE22_BIN" ] && export PATH="$NODE22_BIN:$PATH"
export CI=true npm_config_verify_deps_before_run=false

echo "ENGINE_DIR=$ENGINE_DIR"
echo "DSH_HOME=$DSH_HOME"
echo "ROLES=$ROLES"
echo

for role in $ROLES; do
  dir="$DSH_HOME/profiles/$role"
  if [ ! -f "$dir/package.json" ]; then
    echo "== [$role] profile 不存在 → 创建并加入 rolebus bundle =="
    (cd "$ENGINE_DIR" && pnpm dsh plugin --profile "$role" add "$ROLEBUS_DIR")
  elif ! grep -q '@embody/dsh-rolebus' "$dir/package.json"; then
    echo "== [$role] profile 存在但缺 rolebus bundle → 补 add =="
    (cd "$ENGINE_DIR" && pnpm dsh plugin --profile "$role" add "$ROLEBUS_DIR")
  else
    echo "== [$role] profile 已就绪（bundles 含 rolebus），跳过 add =="
  fi

  echo "== [$role] 写入 profile 层覆盖（来自仓库 role-profiles/$role/cordis.patch.yml）=="
  cp "$REPO_ROOT/role-profiles/$role/cordis.patch.yml" "$dir/cordis.patch.yml"

  echo "-- [$role] 当前 bundles:"
  node -e "console.log(JSON.stringify(require('$dir/package.json').dsh.profile.bundles, null, 2))"
  echo
done

echo "OK: 角色 profiles 已就绪。用 scripts/rolebus-verify.sh 验证配置分层。"
