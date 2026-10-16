#!/usr/bin/env bash
# lib-local-env.sh —— 供 embody/scripts/* 与两个启动入口复用的模型服务取值器。
#
# 只按行取 install/secret.env 里 `NAME=` 或 `export NAME=` 的第一个 '=' 之后的值，再去掉一层
# 成对引号；不 source、不 eval（那个文件含真实凭据与任意 shell 语句，执行它会运行无关代码）。
# 显式导出的同名环境变量优先于文件值——临时覆盖（`LOCAL_MODEL_ds=… bash starter.sh`）走这条。
#
# 依赖 EMBODY_INSTALL_DIR（configs/scripts-env.sh 设，默认 <仓库根>/install）。
# 用法：local_env_value LOCAL_BASE_URL   # 打印值；两者都没有时返回 1。

local_env_value() {
  local name="$1" file="${EMBODY_INSTALL_DIR:?EMBODY_INSTALL_DIR 未设置}/secret.env"
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
