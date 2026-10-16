"""plugin_tools 的 stdio 桥 —— 把 Python 侧的工具面暴露给 dsh 进程。

dsh 侧的工具注册需要「工具名 + 描述 + JSON Schema」与一个执行入口；本文件把已经
存在的 Python 实现（`tool_parser` 读 available_tools.yaml、`tool_executor.ToolExecutor`
执行 tool_calls）暴露成两条子命令，避免在 TS 侧重复一份工具定义或执行逻辑。

调用方是 dsh 插件 `@embody/dsh-tools-bridge`（角色 profile 的 tools-bridge 行给 role）：
load 时 `list` 一次，逐条 ctx.tools.register；模型每次调用再 `call <tool>`。原调用方
`@embody/dsh-embody-tools` 已删除。

    python -m plugin_tools.bridge --role interactor list
        → stdout 输出 JSON 数组 [{name, description, parameters}, ...]
          `waypoint_nav` 那条在 list 时现注入当前的「支持地点」清单（见
          `_inject_supported_targets`）：清单是活数据，写不进 yaml，而这里是模型
          看到合法地名的唯一渠道（PTC 模式下 description 就是生成 SDK 里的 docstring）。

    python -m plugin_tools.bridge --role interactor call <tool_name>
        → stdin 读 arguments JSON（一个对象），执行后 stdout 输出
          {"result": <string>}；工具自身失败输出 {"error": <string>}，两者都
          以 0 退出（退出码只表示协议层成败，工具报错是正常协议结果）。
          白名单外工具、参数非法等协议层错误才以非零退出并打 stderr。
          **stdout 恰好一个 JSON 值**：工具是就地执行的，工具里（含其 import 的库）
          的 print 会被 `_protocol_stdout()` 拨到 stderr，不然会挤坏协议。
          参数原样透传：模型填什么就传什么，桥不替模型编参数。

角色过滤的唯一事实源是 `configs.agents` 的 `ROLE_TOOLS` / `role_tools()`
（interactor → INTERACTOR_TOOLS）。每次 `list` 都是一个新进程，配置改动无需重启引擎。

依赖：PyYAML（见 plugin_tools/requirements.txt）。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List

# 与 tool_parser/tool_executor 一致：把仓库根（embody/）放进搜索路径，
# 这样 `python plugin_tools/bridge.py`（直接执行）与 `-m plugin_tools.bridge`
# 两种入口都能 import configs.* 与 plugin_tools.*。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from configs.agents import UnknownRoleError  # noqa: E402
from plugin_tools.tool_executor import (  # noqa: E402
    ToolExecutor,
    waypoint_nav_supported_targets,
)
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


# waypoint_nav 的 target 只能填「支持地点」里的地名，而那份清单在导航 app 的数据文件里
# （configs.configs.load_supported_locations 读 SUPPORTED_LOCATIONS_FILE），yaml 里写不下。
# 清单是活的（新增地标不用改代码），所以每次 list 现取现注入。
_WAYPOINT_NAV_TOOL = "waypoint_nav"


def _inject_supported_targets(tools: List[Dict[str, Any]]) -> None:
    """就地把当前的「支持地点」清单写进 waypoint_nav 的说明与 target 的参数说明。

    清单与 `waypoint_nav()` 的校验集合同源（同一个 waypoint_nav_supported_targets()），
    免得模型把「518」补全成「518房间」再被判不合法。

    清单取不到（空元组）时**保持原文案不动**并出声：那时候写出「支持地点：」等于告诉
    模型一个地点都没有，比不写更糟。
    """
    targets = waypoint_nav_supported_targets()
    if not targets:
        print("[bridge] 警告：读不到支持地点清单，waypoint_nav 的说明保持原文案", file=sys.stderr)
        return
    hint = "、".join(targets)
    for tool in tools:
        if tool.get("name") != _WAYPOINT_NAV_TOOL:
            continue
        tool["description"] = f"{tool.get('description', '')}（支持地点：{hint}）"
        parameters = tool.get("parameters")
        properties = parameters.get("properties") if isinstance(parameters, dict) else None
        target = properties.get("target") if isinstance(properties, dict) else None
        if isinstance(target, dict):
            target["description"] = f"目标位置地点名称，必须是支持地点之一：{hint}"


@contextmanager
def _protocol_stdout() -> Iterator[Any]:
    """执行期间把本进程的 stdout 借走，yield 出真实 stdout 供协议 JSON 使用。

    协议是「stdout 恰好一个 JSON 值」——dsh 侧 `@embody/dsh-tools-bridge` 的 callTool
    对整个 stdout 做一次 JSON.parse。而工具是**就地执行**的（同一进程、同一 sys.stdout），
    工具里（含它 import 的库）任何一句 print 都会混进协议：2026-10-09 报的
    「调用 audio_interact 的 stdout 不是 JSON」就是 Communicator/sender.py 上报成功后
    那句 `[上报成功] task_progress …`，模型只能从错误文本里把结果捡回来。

    期间 sys.stdout 指向 sys.stderr，工具照常 print、日志一行不丢（dsh 侧已透传 stderr）；
    退出时恢复。工具的子进程都自己收 stdout（tool_executor / audio_tools 里的 subprocess.run），
    不经这条路。
    """
    real = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield real
    finally:
        sys.stdout = real


def _cmd_list(role: str, yaml_path: Path) -> int:
    with _protocol_stdout() as out:
        tools = [_describe(e) for e in load_role_entries(role, yaml_path)]
        _inject_supported_targets(tools)
        json.dump(tools, out, ensure_ascii=False)
        out.write("\n")
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

    # entries 全部来自 yaml，而 _build_executor 对每个条目都给出可调用对象：内置执行器
    # 或 extra.execute 的包装。两者都没有时它**在注册时就抛**（构造下面这个 ToolExecutor
    # 的那一刻），所以走到这里的执行器必然存在——yaml 里多出一个没人实现的工具名是配置
    # 错误，会在第一次 list / call 时暴露，而不是静默返回空串。
    executor = ToolExecutor(entries)

    with _protocol_stdout() as out:
        try:
            result = executor.execute_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001 —— 工具失败要原样回给模型，附栈便于定位
            json.dump(
                {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()},
                out,
                ensure_ascii=False,
            )
            out.write("\n")
            return 0

        json.dump({"result": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)},
                  out, ensure_ascii=False)
        out.write("\n")
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
