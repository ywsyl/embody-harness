"""plugin_tools 的 stdio 桥 —— 把 Python 侧的工具面暴露给 dsh 进程。

dsh 侧的工具注册需要「工具名 + 描述 + JSON Schema」与一个执行入口；本文件把已经
存在的 Python 实现（`tool_parser` 读 available_tools.yaml、`tool_executor.ToolExecutor`
执行 tool_calls）暴露成两条子命令，避免在 TS 侧重复一份工具定义或执行逻辑。

调用方是 dsh 插件 `@embody/dsh-tools-bridge`（角色 profile 的 tools-bridge 行给 role）：
load 时 `list` 一次，逐条 ctx.tools.register；模型每次调用再 `call <tool>`。原调用方
`@embody/dsh-embody-tools` 已删除。

    python -m plugin_tools.bridge --role interactor list
        → stdout 输出 JSON 数组 [{name, description, parameters}, ...]

    python -m plugin_tools.bridge --role interactor call <tool_name>
        → stdin 读 arguments JSON（一个对象），执行后 stdout 输出
          {"result": <string>}；工具自身失败输出 {"error": <string>}，两者都
          以 0 退出（退出码只表示协议层成败，工具报错是正常协议结果）。
          白名单外工具、参数非法等协议层错误才以非零退出并打 stderr。

角色过滤的唯一事实源是 `configs.agent_plugin` 的 `ROLE_TOOLS` / `role_tools()`
（interactor → INTERACTOR_TOOLS）。每次 `list` 都是一个新进程，配置改动无需重启引擎。

依赖：PyYAML（见 plugin_tools/requirements.txt）。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List

# 与 tool_parser/tool_executor 一致：把仓库根（embody/）放进搜索路径，
# 这样 `python plugin_tools/bridge.py`（直接执行）与 `-m plugin_tools.bridge`
# 两种入口都能 import configs.* 与 plugin_tools.*。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.agent_plugin import UnknownRoleError  # noqa: E402
from plugin_tools.tool_executor import ToolExecutor  # noqa: E402
from plugin_tools.tool_parser import load_role_entries  # noqa: E402

# 角色身份不可能是 unset（bundle 中性默认）——与 @embody/dsh-rolebus 同约定。
UNSET_ROLE = "unset"


def _describe(entry: Dict[str, Any]) -> Dict[str, Any]:
    """把一个 yaml 条目压成 dsh 侧注册需要的最小字段。"""
    func = entry.get("function", {})
    return {
        "name": func.get("name", ""),
        "description": func.get("description", ""),
        "parameters": func.get("parameters", {}),
    }


def _cmd_list(role: str, yaml_path: Path) -> int:
    tools = [_describe(e) for e in load_role_entries(role, yaml_path)]
    json.dump(tools, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _cmd_call(role: str, yaml_path: Path, name: str) -> int:
    entries = load_role_entries(role, yaml_path)
    if not any(e.get("function", {}).get("name") == name for e in entries):
        print(f"[bridge] 工具 {name!r} 不在 {role} 的白名单内", file=sys.stderr)
        return 1

    try:
        arguments = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        print(f"[bridge] 参数不是合法 JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(arguments, dict):
        print("[bridge] 参数必须是 JSON 对象", file=sys.stderr)
        return 1

    # entries 全部来自 yaml，而 _build_executor 对每个条目都给出可调用对象
    # （builtin 或其 extra.execute 的包装，后者语法错误也只在调用时抛），
    # 所以白名单校验通过后执行器必然存在；get_executor 的 Optional 是给
    # parse_and_execute 用的（那里名字来自 LLM，可能不在注册表里）。
    executor = ToolExecutor(entries)
    run = executor.get_executor(name)
    assert run is not None, f"工具 {name!r} 无执行器 —— 白名单与 {yaml_path} 不一致"

    try:
        result = run(arguments)
    except Exception as exc:  # noqa: BLE001 —— 工具失败要原样回给模型，附栈便于定位
        json.dump(
            {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()},
            sys.stdout,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
        return 0

    json.dump({"result": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)},
              sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def main(argv: List[str] | None = None) -> int:
    """解析子命令并执行；返回进程退出码。"""
    parser = argparse.ArgumentParser(prog="plugin_tools.bridge", description=__doc__)
    parser.add_argument("--role", required=True, help="角色身份（决定用哪个 <ROLE>_TOOLS 白名单）")
    parser.add_argument("--yaml", default=None, help="工具清单路径（缺省用 configs.paths.AVAILABLE_TOOLS_FILE）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出该角色可见的工具定义")
    call = sub.add_parser("call", help="执行一次工具调用（arguments 走 stdin）")
    call.add_argument("tool", help="工具名")

    args = parser.parse_args(argv)

    if args.role == UNSET_ROLE:
        parser.error(f"--role 不能是 {UNSET_ROLE!r}（bundle 中性默认，表示尚未赋予角色身份）")

    if args.yaml:
        yaml_path = Path(args.yaml)
    else:
        from configs.paths import AVAILABLE_TOOLS_FILE

        yaml_path = AVAILABLE_TOOLS_FILE

    # 未知角色是协议层错误（白名单里没有它，拿不到任何工具名），与工具自身失败区分开：
    # 前者非零退出，后者以 0 退出走 {"error": ...}。
    try:
        if args.command == "list":
            return _cmd_list(args.role, yaml_path)
        return _cmd_call(args.role, yaml_path, args.tool)
    except UnknownRoleError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
