"""LitePan 联动模块（← LitePan-TGBot `tgbot.py`）。

原独立进程的 getUpdates 长轮询 / offset / 线程全部删除，业务逻辑（自动发现、
slug 菜单、按规则 ID 精确执行、回执轮询、`render_result`）保留并异步化为 MTBots 的一个
feature 包；对外只暴露一个 `MODULE = ModuleSpec(...)`。
"""

from __future__ import annotations

from ...core import ModuleSpec
from . import handlers
from .client import TERMINAL_STATUSES, LitePanClient, LitePanError
from .config import ConfigError, LitePanConfig, UserProfile
from .discovery import Discovery

MODULE = ModuleSpec(
    id="litepan",
    icon="🎬",
    title="LitePan 联动",
    description="远程触发 LitePan 媒体自动化规则",
    callback_prefix="p",
    register=handlers.register,
    commands=handlers.commands,
    summary=handlers.summary,
    help_text=handlers.help_text,
    id_lines=handlers.id_lines,
    open_panel=handlers.open_panel,
    show_status=handlers.show_status,
    show_list=handlers.show_list,
    rescue=handlers.RESCUE,
    startup=handlers.startup,
    scope_chats=handlers.scope_chats,
    check=handlers.check,
)

__all__ = [
    "MODULE",
    "ConfigError",
    "UserProfile",
    "LitePanConfig",
    "Discovery",
    "LitePanClient",
    "LitePanError",
    "TERMINAL_STATUSES",
]
