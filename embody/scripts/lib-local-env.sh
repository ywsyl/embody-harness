#!/usr/bin/env bash
# lib-local-env.sh —— 供 embody/scripts/* 复用的 LOCAL_* 取值器。
#
# 只按行取 configs/exp.bashrc 里 `LOCAL_*=` 或 `export LOCAL_*=` 的第一个 '=' 之后的值，
# 再去掉一层成对引号；不 source、不 eval（exp.bashrc 含真实凭据与任意 shell 语句，执行它
# 会运行无关代码）。显式导出的同名环境变量优先于文件值。
#
# 依赖 EMBODY_CONFIGS_DIR（默认由调用方设为 <本目录>/../configs）。
# 用法：local_env_value LOCAL_BASE_URL   # 打印值；两者都没有时返回 1。

local_env_value() {
  local name="$1" file="${EMBODY_CONFIGS_DIR:?EMBODY_CONFIGS_DIR 未设置}/exp.bashrc"
  local value="${!name:-}"
  if [ -z "$value" ] && [ -f "$file" ]; then
    local line
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${name}=" "$file" | tail -n1 || true)"
    if [ -n "$line" ]; then
      value="${line#*=}"
      case "$value" in
        \"*\") value="${value#\"}"; value="${value%\"}" ;;
        \'*\') value="${value#\'}"; value="${value%\'}" ;;
      esac
    fi
  fi
  [ -n "$value" ] || return 1
  printf '%s' "$value"
}
