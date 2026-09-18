#!/usr/bin/env bash
# rolebus-setup.sh —— 把“每角色独立 profile/进程”的配置文件分层物化到 DSH_HOME。
#
# 对每个角色：
#   1) 若 $DSH_HOME/profiles/<role> 尚不存在 → `dsh plugin --profile <role> add <bundle dir>`
#      （引擎会初始化该 profile，并把该本地 bundle pnpm link 进来）。本仓库 bundle 缺哪个
#      补哪个：@embody/dsh-rolebus（事件收发）、@embody/dsh-tools-bridge（自定义工具的注册方）
#      给每个角色；@embody/dsh-supervisor-loop（定时观察循环）只给 supervisor —— 它是本角色
#      的唤醒源，其余角色没有这一行（它们由 rolebus 事件唤醒，不对称是角色差异本身，不是漏给）。
#      已废弃的旧注册方 @embody/dsh-embody-tools 连同它的悬空 link 一并摘掉——留着会让启动
#      解析不到模块。
#      装进来的 bundle 只是“行存在”：tools-bridge 的中性默认 role=unset 不注册任何工具、
#      不起 Python 桥，supervisor-loop 插的是 disabled: true —— 都由各自 profile 层赋予角色差异。
#   2) 把该角色的 profile 覆盖层覆盖为 profile 自身的 cordis.patch.yml（这是“profile 层”）。
#      事实源按角色定位（角色 → 目录的唯一映射在 scripts/lib-roles.sh）：
#      interactor → Interactor/、orchestrator → Orchestrator/、planner → Planner/、
#      supervisor → Supervisor/，都是 <Role>/cordis.patch.yml ——本角色唯一事实源，含人设、
#      rolebus 身份、自定义工具的角色身份、原生工具 disable 列表；supervisor 另有一份常驻
#      叠加层 Supervisor/resident.patch.yml 打开定时观察行（经 --patch 叠，不进 profile 层）。
#
# 幂等：profile 已存在且 bundles 齐全时跳过 add，只刷新 profile 层文件。
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
TOOLS_BRIDGE_DIR="$REPO_ROOT/dsh-plugins/tools-bridge"
SUPERVISOR_LOOP_DIR="$REPO_ROOT/dsh-plugins/supervisor-loop"
# 旧的自定义工具注册方，已由 tools-bridge 取代。它的目录连同 link 都没了，profile 里
# 残留的依赖/bundles 条目会让启动解析不到模块。
RETIRED_TOOLS_BUNDLE='@embody/dsh-embody-tools'
ROLES="${ROLES:-interactor orchestrator planner supervisor}"

# 本机配方：node22 前置到 PATH（pnpm 会解析 scripts 里的 node 为 node22），
# CI=true 避开 lefthook 安装器，跳过 pnpm 每次跑前的 deps 检查。
NODE22_BIN="${NODE22_BIN:-$(cd "$REPO_ROOT/../.." && pwd)/harness/.runtime/node22/bin}"
[ -d "$NODE22_BIN" ] && export PATH="$NODE22_BIN:$PATH"
export CI=true npm_config_verify_deps_before_run=false

# shellcheck source=lib-roles.sh
source "$REPO_ROOT/scripts/lib-roles.sh"   # role_profile_patch：角色 → profile 层事实源

# 确保该 profile 的 bundles 含该本地 bundle（缺则 add）。$1=角色 $2=目录 $3=包名
ensure_bundle() {
  local role="$1" dir="$2" pkg="$3" profile_dir="$DSH_HOME/profiles/$role"
  if [ -f "$profile_dir/package.json" ] && grep -q "\"$pkg\"" "$profile_dir/package.json"; then
    echo "   [$pkg] 已就绪，跳过 add"
    return
  fi
  echo "   [$pkg] 缺失 → dsh plugin --profile $role add"
  (cd "$ENGINE_DIR" && pnpm dsh plugin --profile "$role" add "$dir")
}

# 摘掉已废弃 bundle 的残留（$1=角色 $2=包名）：`dsh plugin remove` 走 pnpm remove，并按
# 安装状态同步 dsh.profile.bundles 里的层条目。package.json 里没有它就什么都不做。
remove_bundle() {
  local role="$1" pkg="$2" profile_dir="$DSH_HOME/profiles/$role"
  [ -f "$profile_dir/package.json" ] || return 0
  grep -q "\"$pkg\"" "$profile_dir/package.json" || return 0
  echo "   [$pkg] 已废弃 → dsh plugin --profile $role remove"
  (cd "$ENGINE_DIR" && pnpm dsh plugin --profile "$role" remove "$pkg")
}

echo "ENGINE_DIR=$ENGINE_DIR"
echo "DSH_HOME=$DSH_HOME"
echo "ROLES=$ROLES"
echo

for role in $ROLES; do
  # 未登记的角色会让这里直接退出（lib-roles.sh 已把原因打到 stderr）：角色名拼错不该
  # 表现成「这个角色还没建置，跳过」。
  patch="$(role_profile_patch "$role")"
  if [ ! -f "$patch" ]; then
    echo "== [$role] 缺 profile 层事实源 $patch —— 跳过"
    continue
  fi

  echo "== [$role] 确保 profile 存在且 bundles 齐全（rolebus + tools-bridge）=="
  remove_bundle "$role" "$RETIRED_TOOLS_BUNDLE"
  ensure_bundle "$role" "$ROLEBUS_DIR" '@embody/dsh-rolebus'
  ensure_bundle "$role" "$TOOLS_BRIDGE_DIR" '@embody/dsh-tools-bridge'
  # 定时观察循环只装在 supervisor：那一行就是本角色的唤醒源，其余角色靠 rolebus 事件唤醒，
  # 装进来也没有任何 profile 会打开它（bundle 插的是 disabled: true）。
  if [ "$role" = supervisor ]; then
    ensure_bundle "$role" "$SUPERVISOR_LOOP_DIR" '@embody/dsh-supervisor-loop'
  fi

  echo "== [$role] 写入 profile 层覆盖（来自 ${patch#"$REPO_ROOT"/}）=="
  cp "$patch" "$DSH_HOME/profiles/$role/cordis.patch.yml"

  echo "-- [$role] 当前 bundles:"
  node -e "console.log(JSON.stringify(require('$DSH_HOME/profiles/$role/package.json').dsh.profile.bundles, null, 2))"
  echo
done

echo "OK: 角色 profiles 已就绪。"
