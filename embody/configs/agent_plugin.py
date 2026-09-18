# =============================================================================
# Tools — 可用工具过滤器
# =============================================================================
# 这里只放 available_tools.yaml 里的自定义工具。角色间的收发不在本表：那些工具名
# （rolebus_send_*）由 @embody/dsh-rolebus 按事件目录与本角色的 peers 生成，信封形状
# 也归它，Python 侧不再实现第二份发件工具（原 embody_execute 即因此废弃）。
INTERACTOR_TOOLS = (
    "speak",
    "speak_listen",
)

ORCHESTRATOR_TOOLS = (
    "do_action",
    "look",
    "waypoint_nav",
    # "vision_language_nav",
    "move",
    # "search_and_approach",
)

PLANNER_TOOLS = (
    "speak",
    "speak_listen",
    "do_action",
    "look",
    "waypoint_nav",
    # "vision_language_nav",
    "move",
    # "search_and_approach",
)

# 角色 → 工具白名单。键即 profile / 桥 `--role` 的取值，值是 available_tools.yaml
# 里的工具名。任何把工具交给模型的途径（dsh 注册、prompt 文本拼接）都经 role_tools()
# 取名单，不在本文件之外另写一份子集。
ROLE_TOOLS = {
    "interactor": INTERACTOR_TOOLS,
    "orchestrator": ORCHESTRATOR_TOOLS,
    "planner": PLANNER_TOOLS,
}


class UnknownRoleError(LookupError):
    """请求的角色不在 ROLE_TOOLS 里。

    取不到名单的调用方必须失败：当成空集处理会让该角色「一个工具都没有」却不报错。
    """


def role_tools(role: str) -> tuple:
    """取该角色的工具白名单。

    Args:
        role: 角色身份，取值同 profile / stdio 桥的 `--role`。

    Returns:
        该角色可见的工具名元组，顺序即白名单顺序。

    Raises:
        UnknownRoleError: 角色不在 ROLE_TOOLS 里。
    """
    try:
        return tuple(ROLE_TOOLS[role])
    except KeyError:
        raise UnknownRoleError(
            f"configs/agent_plugin.py 的 ROLE_TOOLS 无角色 {role!r}，已知: {sorted(ROLE_TOOLS)}"
        ) from None
