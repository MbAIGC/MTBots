"""Cline（Cline / ClinePass 多账号额度）feature 包。

本包唯一对接口就是 ``MODULE``：:class:`mtbots.core.ModuleSpec`。
核心逻辑在 :mod:`mtbots.features.cline.core`，Telegram 交互在
:mod:`mtbots.features.cline.handlers`。
"""

from __future__ import annotations

from mtbots.core import ModuleSpec
from mtbots.features.cline.handlers import (
    RESCUE,
    cmd_addkey,
    cmd_clear,
    cmd_delkey,
    cmd_keys,
    cmd_status,
    commands,
    help_text,
    id_lines,
    open_panel,
    refresh,
    register,
    show_list,
    show_status,
    summary,
)

#: 首页刷新的最短间隔（秒）：一个 Key 要打 3 个额度接口，比 docker 的本地扫描金贵得多
REFRESH_TTL = 60.0

MODULE = ModuleSpec(
    id="cline",
    icon="🤖",
    title="Cline 额度",
    description="Cline/ClinePass 多账号额度面板",
    callback_prefix="c",
    register=register,
    commands=commands,
    summary=summary,
    refresh=refresh,
    refresh_ttl=REFRESH_TTL,
    help_text=help_text,
    id_lines=id_lines,
    open_panel=open_panel,
    show_status=show_status,
    show_list=show_list,
    rescue=RESCUE,
    startup=None,
)

__all__ = [
    "MODULE",
    "REFRESH_TTL",
    "register",
    "show_status",
    "show_list",
    "open_panel",
    "refresh",
    "summary",
    "help_text",
    "id_lines",
    "commands",
    "cmd_status",
    "cmd_addkey",
    "cmd_delkey",
    "cmd_keys",
    "cmd_clear",
    "RESCUE",
]
