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
    register,
    show_list,
    show_status,
    summary,
)

MODULE = ModuleSpec(
    id="cline",
    icon="🤖",
    title="Cline 额度",
    description="Cline/ClinePass 多账号额度面板",
    callback_prefix="c",
    register=register,
    commands=commands,
    summary=summary,
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
    "register",
    "show_status",
    "show_list",
    "open_panel",
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
