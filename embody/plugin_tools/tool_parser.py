
from __future__ import annotations

import pprint
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# 确保项目根目录在 Python 搜索路径中（支持 python Loop/tool_parser.py 直接运行）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml

from configs.agent_plugin import role_tools
from configs.paths import AVAILABLE_TOOLS_FILE

from plugin_tools.tool_executor import ToolExecutor, waypoint_nav_supported_targets


# ─────────────────────────────────────────────────────────────────────────────
# 独立加载函数 — 按需加载工具说明文本（不依赖 ToolParser 实例）
# ─────────────────────────────────────────────────────────────────────────────

def _load_tool_entries(yaml_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """读取 available_tools.yaml，返回 tools 列表；加载失败返回空列表。"""
    yaml_path = yaml_path or AVAILABLE_TOOLS_FILE
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        print(f"[tool_parser] 加载 {yaml_path} 失败: {e}", file=sys.stderr)
        return []
    return data.get("tools", []) if isinstance(data, dict) else []


def load_role_entries(
    role: str,
    yaml_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """读取 available_tools.yaml 并留下该角色白名单内的条目（顺序即白名单顺序）。

    这是「哪些工具属于哪个角色」的唯一落地处：dsh 侧的注册（bridge.list/call）与
    Python 侧的 prompt 文本拼接都从这里取，过滤规则只写一份。

    Args:
        role:      角色身份，决定用 configs.agent_plugin 的哪份白名单。
        yaml_path: 工具清单路径，缺省 AVAILABLE_TOOLS_FILE。

    Returns:
        过滤后的 yaml 条目列表。

    Raises:
        UnknownRoleError: 角色不在 configs.agent_plugin 的 ROLE_TOOLS 里。
    """
    wanted = role_tools(role)
    by_name = {
        entry.get("function", {}).get("name"): entry
        for entry in _load_tool_entries(yaml_path)
    }

    # 白名单里清单没有的名字要出声：否则只是「该角色少了一份工具」，没有线索可查。
    missing = [name for name in wanted if name not in by_name]
    if missing:
        print(
            f"[tool_parser] 警告：{role} 的工具白名单里 {', '.join(missing)} 在工具清单中不存在",
            file=sys.stderr,
        )
    return [by_name[name] for name in wanted if name in by_name]


def _build_tools_text(tools: List[Dict[str, Any]]) -> str:
    """
    构造工具说明文本：每条包含 OpenAI schema 字段（type/name/description/parameters）
    与 extra 字段，供需要完整字段说明的场景使用。
    """
    parts: List[str] = []
    for entry in tools:
        func_def = entry.get("function", {})
        extra = entry.get("extra", {})
        name = func_def.get("name", "?")
        lines = [f"工具: {name}"]

        # waypoint_nav 工具：注入支持地点清单，约束 LLM 生成的 target 为合法地标。
        # 与 ToolExecutor.waypoint_nav 的校验集合 waypoint_nav_supported_targets() 保持同步，
        # 避免 LLM 把"518"补全成"518房间"导致导航校验失败。
        description = func_def.get("description", "")
        parameters = func_def.get("parameters", {})
        if name == "waypoint_nav":
            nav_targets = "、".join(waypoint_nav_supported_targets())
            description = f"{description}（支持的目标地点：{nav_targets}）"
            tgt = parameters.get("properties", {}).get("target")
            if isinstance(tgt, dict):
                tgt["description"] = (
                    f"目标位置地点名称，必须是支持地点之一：{nav_targets}"
                )

        # OpenAI schema 字段
        schema_fields = {
            "type": entry.get("type", "function"),
            "name": name,
            "description": description,
            "parameters": parameters,
        }
        for k, v in schema_fields.items():
            if isinstance(v, (dict, list)):
                indented = "\n".join(f"      {l}" for l in pprint.pformat(v).splitlines())
                lines.append(f"  {k}:\n{indented}")
            else:
                lines.append(f"  {k}: {v}")

        # extra 字段
        if extra:
            for k, v in extra.items():
                if isinstance(v, (dict, list)):
                    indented = "\n".join(f"      {l}" for l in pprint.pformat(v).splitlines())
                    lines.append(f"  {k}:\n{indented}")
                else:
                    lines.append(f"  {k}: {v}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)



# ─────────────────────────────────────────────────────────────────────────────
# ToolParser — 唯一核心类
# ─────────────────────────────────────────────────────────────────────────────

class ToolParser:
    """
    读取 available_tools.yaml，构造 prompt_tools、tools 文本，
    并持有 ToolExecutor 实例负责 tool_calls 的分发执行。

    本类只暴露角色白名单内的工具，且角色必须显式给出——没有「不过滤」的构造方式，
    因为拼进 prompt 的工具说明是模型判断「能调什么」的唯一依据。

    Attributes:
        role         — 本实例的角色身份，决定用哪份白名单
        prompt_tools — extra 字段拼接的文本（供 prompt 使用）
        tools        — 允许的工具完整说明文本（OpenAI schema + extra 字段，仅含白名单内的工具）
        _executor    — ToolExecutor 实例，负责执行器注册与 tool_calls 执行

    Args:
        role:      角色身份，决定用 configs.agent_plugin 的哪份白名单
        yaml_path: 工具清单 YAML 路径，默认 AVAILABLE_TOOLS_FILE
    """

    def __init__(self, role: str, yaml_path: Optional[Path] = None) -> None:
        self.role = role
        self._yaml_path = yaml_path or AVAILABLE_TOOLS_FILE
        self._yaml_entries: List[Dict[str, Any]] = []
        self._executor = ToolExecutor()
        self.tools = ""
        self._load()

    # ---- 私有：加载 YAML 并注册执行器 ----------------------------------------

    def _load(self) -> None:
        """按角色白名单取工具条目，注册执行器并重建说明文本。"""
        tools = load_role_entries(self.role, self._yaml_path)
        self._yaml_entries = tools
        self._executor.register_from_yaml(tools)
        self.tools = _build_tools_text(tools)

    # ---- 公开：属性 ----------------------------------------------------------

    @property
    def prompt_tools(self) -> str:
        """返回可读的工具说明文本（供 ContextBuilder prompt 使用）。"""
        parts: List[str] = []
        for entry in self._yaml_entries:
            func_def = entry.get("function", {})
            extra = entry.get("extra", {})
            name = func_def.get("name", "?")
            lines = [f"工具: {name}"]
            if extra:
                for k, v in extra.items():
                    if isinstance(v, (dict, list)):
                        indented = "\n".join(f"      {l}" for l in pprint.pformat(v).splitlines())
                        lines.append(f"  {k}:\n{indented}")
                    else:
                        lines.append(f"  {k}: {v}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    # ---- 公开：执行器转发 ----------------------------------------------------

    def get_executor(self, name: str) -> Optional[callable]:
        """返回指定工具的执行函数（转发给 ToolExecutor）。"""
        return self._executor.get_executor(name)

    def has(self, name: str) -> bool:
        """检查工具是否已注册（转发给 ToolExecutor）。"""
        return self._executor.has(name)

    def reload(self) -> None:
        """重新加载 available_tools.yaml，重置所有执行器（tools 由 _load 一并重建）。"""
        self._load()

    # ---- 公开：解析 & 执行 --------------------------------------------------

    def parse_and_execute(
        self,
        tool_calls: Dict[str, Any],
        task_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        从 tool_calls 逐个执行并返回结构化结果列表（转发给 ToolExecutor）。

        Args:
            tool_calls:  request_llm_with_messages 的返回值
            task_id:       当前任务 ID（透传给执行器）

        Returns:
            工具执行结果字典列表，每项包含 tool_name、params、result、status
        """
        return self._executor.parse_and_execute(tool_calls, task_id)
