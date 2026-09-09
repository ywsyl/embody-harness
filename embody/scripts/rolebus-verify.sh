#!/usr/bin/env bash
# rolebus-verify.sh —— 里程碑 0：用 --dump-config 验证“每角色 profile/进程”的配置分层。
#
# 对每个角色跑 `dsh --profile <role> --dump-config`（免 boot、免模型 key，只做行合成），
# 断言分层正确：
#   1) 输出包含 provenance 注释 `# == @deepseek-ai/dsh-base, patched by .../cordis.patch.yml`
#      —— 证明 base 行被“本角色 profile 层”覆盖，而非 bundle 默认。
#   2) system-prompt 的 persona 是本角色自己的（含角色名），不是空 persona。
#   3) rolebus 行（由 @embody/dsh-rolebus bundle 插入）的 config.role 被改写为本角色。
#
# 任一条断言失败 → 非零退出，供 CI / 手动回归使用。
#
# 可覆盖环境变量：ENGINE_DIR / DSH_HOME（与 rolebus-setup.sh 相同）。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_ENGINE="$(cd "$REPO_ROOT/.." && pwd)/deepseek-harness"
ENGINE_DIR="${ENGINE_DIR:-$DEFAULT_ENGINE}"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
ROLES="${ROLES:-interactor orchestrator}"

NODE22_BIN="$(cd "$REPO_ROOT/.." && pwd)/.runtime/node22/bin"
[ -d "$NODE22_BIN" ] && export PATH="$NODE22_BIN:$PATH"
export CI=true npm_config_verify_deps_before_run=false

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0

for role in $ROLES; do
  echo "======== $role --dump-config ========"
  dump="$(cd "$ENGINE_DIR" && pnpm dsh --profile "$role" --dump-config 2>&1)"
  printf '%s\n' "$dump" > "$TMP/$role.yml"

  # 断言 1：本 profile 层确实覆盖了 base 行（有 provenance 注释指向该角色的 cordis.patch.yml）
  if grep -q "profiles/$role/cordis.patch.yml" "$TMP/$role.yml"; then
    echo "  [ok] base 行被 profile 层覆盖（provenance 指向 profiles/$role/cordis.patch.yml）"
  else
    echo "  [FAIL] 未见 profile 层覆盖注释 profiles/$role/cordis.patch.yml"
    fail=1
  fi

  # 断言 2：persona 已替换为本角色 —— 折叠块里应含角色专有的 English 称谓
  # （与 role-profiles/<role>/cordis.patch.yml 中 persona 文本对应）。
  case "$role" in
    interactor)  expect_persona='You are the Interactor' ;;
    orchestrator) expect_persona='You are the Orchestrator' ;;
    supervisor)  expect_persona='You are the Supervisor' ;;
    *)           expect_persona="$role" ;;
  esac
  if grep -qF "$expect_persona" "$TMP/$role.yml"; then
    echo "  [ok] persona 已改写为本角色（含“$expect_persona”）"
  else
    echo "  [FAIL] persona 未改写为本角色（缺“$expect_persona”）"
    fail=1
  fi

  # 断言 3：rolebus 行的 config.role == 本角色（bundle 插入，profile 层整块改写）
  if grep -q "role: $role" "$TMP/$role.yml"; then
    echo "  [ok] rolebus.config.role == $role"
  else
    echo "  [FAIL] rolebus.config.role 未被设为 $role"
    fail=1
  fi
  echo
done

echo "========================================="
if [ "$fail" -eq 0 ]; then
  echo "ALL PASS: 各角色 profile 分层符合预期。"
  echo "可交互查看：DSH_HOME=$DSH_HOME 下 profiles/<role>/cordis.patch.yml 即为各角色 profile 层。"
else
  echo "SOME FAILED（见上）。"
fi
exit "$fail"
