"""Core 容器与 ModuleSpec —— 三个 feature 包唯一的对接口。

模块只做三件事：注册自己的 handler、回答"我有哪些命令/一行总览/帮助章节"、
在被路由点名时打开自己的面板。**不允许**自己调 `set_my_commands`、
不允许自己维护首页、不允许绕过 `PanelManager` 直接刷屏。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from .acl import ACL
from .config import Settings
from .jobs import JobCenter
from .menu import MenuManager
from .panels import PanelManager

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from telegram import Application, Update
    from telegram.ext import ContextTypes

log = logging.getLogger("mtbots.core")

#: 模块 handler 的统一签名：(core, update, context) -> awaitable
ModuleHandler = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class ModuleSpec:
    """一个 feature 包对外暴露的全部信息。"""

    id: str
    icon: str
    title: str
    description: str
    callback_prefix: str
    register: Callable[[Any, "Core"], None]

    commands: Callable[["Core", int], list[tuple[str, str]]] = lambda core, uid: []
    summary: Optional[Callable[["Core", int], Awaitable[str]]] = None
    help_text: Optional[Callable[["Core", int], str]] = None
    id_lines: Optional[Callable[["Core", int], Awaitable[list[str]]]] = None

    open_panel: Optional[ModuleHandler] = None
    show_status: Optional[ModuleHandler] = None
    show_list: Optional[ModuleHandler] = None

    rescue: dict[str, Callable[..., Awaitable[None]]] = field(default_factory=dict)
    startup: Optional[Callable[["Core", Any], Awaitable[None]]] = None
    #: LitePan 这类「命令需要按会话下发」的模块，在这里给出已知会话
    scope_chats: Callable[["Core"], list[int]] = lambda core: []
    #: `python -m mtbots --check` 的自检项（返回待打印的行，不要在这里连网络）
    check: Optional[Callable[["Core"], list[str]]] = None


@dataclass
class Core:
    settings: Settings
    acl: ACL
    panels: PanelManager
    jobs: JobCenter
    menu: MenuManager
    modules: dict[str, ModuleSpec] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    #: chat_id -> 当前模块 id（面包屑路由的"当前位置"）
    active_module: dict[int, str] = field(default_factory=dict)
    #: chat_id -> 等待用户输入的临时状态（/cancel 清空）
    pending: dict[int, Any] = field(default_factory=dict)

    # ---------- 模块注册 ----------
    def register(self, spec: ModuleSpec) -> None:
        if spec.id in self.modules:
            log.warning("模块 %s 重复注册，已忽略后者", spec.id)
            return
        self.modules[spec.id] = spec
        self.data.setdefault(spec.id, {})
        log.debug("模块已注册：%s(%s)", spec.id, spec.title)

    def has(self, module_id: str) -> bool:
        return module_id in self.modules

    def get(self, module_id: str) -> ModuleSpec:
        return self.modules[module_id]

    def state(self, module_id: str) -> dict:
        """模块私有状态槽（dict）。

        若该模块把 `data[module_id]` 用作自定义对象（例如 docker 放的是 `DockerState`），
        就退回到不会覆盖它的独立槽位 `data["state:<id>"]`，避免误伤。
        """
        bucket = self.data.get(module_id)
        if bucket is None:
            bucket = {}
            self.data[module_id] = bucket
            return bucket
        if isinstance(bucket, dict):
            return bucket
        key = "state:%s" % module_id
        fallback = self.data.get(key)
        if not isinstance(fallback, dict):
            fallback = {}
            self.data[key] = fallback
        return fallback

    def module_ids(self) -> list[str]:
        return list(self.modules.keys())

    # ---------- 当前位置 ----------
    def module_of_chat(self, chat_id: Optional[int]) -> Optional[str]:
        if chat_id is None:
            return None
        module_id = self.active_module.get(int(chat_id))
        if module_id and module_id not in self.modules:
            return None
        return module_id

    def set_module(self, chat_id: int, module_id: Optional[str]) -> None:
        if module_id is None:
            self.active_module.pop(int(chat_id), None)
        else:
            self.active_module[int(chat_id)] = module_id

    # ---------- 权限 ----------
    def can(self, user_id: Optional[int], module_id: str) -> bool:
        return self.acl.can(user_id, module_id)

    def icons(self) -> dict[str, str]:
        return {spec.id: spec.icon for spec in self.modules.values()}

    def titles(self) -> dict[str, str]:
        return {spec.id: spec.title for spec in self.modules.values()}

    # ---------- 等待输入 ----------
    def set_pending(self, chat_id: int, value: Any) -> None:
        if value is None:
            self.pending.pop(int(chat_id), None)
        else:
            self.pending[int(chat_id)] = value

    def pop_pending(self, chat_id: int) -> Any:
        return self.pending.pop(int(chat_id), None)

    def get_pending(self, chat_id: int) -> Any:
        return self.pending.get(int(chat_id))


def core_of(context: "ContextTypes.DEFAULT_TYPE") -> Core:
    return context.application.bot_data["core"]


def core_from_app(application: "Application") -> Core:
    return application.bot_data["core"]


__all__ = ["Core", "ModuleSpec", "ModuleHandler", "core_of", "core_from_app"]
