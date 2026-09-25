"""统一命令菜单：模块只提交「片段」，由这里合成后一次下发。

LitePan 原来独占 `setMyCommands`（每刷新一次菜单就把别的 bot 的命令擦掉），
合并后 Telegram 的 100 条命令额度是三个模块**共享**的，所以必须集中管理：

* 模块调用 `set_module_commands()` 只贡献自己的命令，**不再碰 Telegram API**；
* `apply()` 合成「全局命令 + 各模块片段」，内容没变就不发请求（保留原去重逻辑）；
* 需要隐藏的模块（如 Docker 运维命令）用 `scope_chats` 走 `BotCommandScopeChat` 按会话下发。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional, Sequence

from telegram import BotCommand, BotCommandScopeChat

log = logging.getLogger("mtbots.menu")

MAX_COMMANDS = 100
_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def _clean(entries: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_name, raw_desc in entries:
        name = (raw_name or "").strip().lstrip("/").lower()
        desc = (raw_desc or "").strip()[:256]
        if not name or not desc or name in seen or not _NAME_RE.match(name):
            continue
        seen.add(name)
        out.append((name, desc))
    return out


class MenuManager:
    def __init__(self, core: Any, base: Sequence[tuple[str, str]] = ()):
        self._core = core
        self._base = _clean(base)

    def attach(self, core: Any) -> None:
        """两阶段构造：Core 需要 MenuManager，MenuManager 也需要 Core。"""
        self._core = core
        self._module: dict[str, list[tuple[str, str]]] = {}
        self._scopes: dict[str, list[int]] = {}
        self._applied: dict[str, tuple[tuple[str, str], ...]] = {}

    # ---------- 片段登记 ----------
    def set_module_commands(
        self,
        module_id: str,
        entries: Iterable[tuple[str, str]],
        *,
        scope_chats: Optional[Sequence[int]] = None,
    ) -> None:
        self._module[module_id] = _clean(entries)
        if scope_chats is not None:
            self._scopes[module_id] = [int(c) for c in scope_chats]
        else:
            self._scopes.pop(module_id, None)

    def clear_module(self, module_id: str) -> None:
        self._module.pop(module_id, None)
        self._scopes.pop(module_id, None)
        self.invalidate()

    def invalidate(self) -> None:
        self._applied.clear()

    def module_entries(self, module_id: str) -> list[tuple[str, str]]:
        return list(self._module.get(module_id, []))

    def _entries_for(self, module_id: str, user_id: int) -> list[tuple[str, str]]:
        """显式登记的片段优先；模块没登记时退回 ModuleSpec.commands（更稳）。"""
        if module_id in self._module:
            return list(self._module[module_id])
        spec = self._core.modules.get(module_id) if self._core is not None else None
        if spec is None or spec.commands is None:
            return []
        try:
            return list(spec.commands(self._core, user_id))
        except Exception as exc:  # 模块自己的命令列表出错不该拖垮整个菜单
            log.warning("模块 %s 的命令片段生成失败：%s", module_id, exc)
            return []

    # ---------- 合成 ----------
    def render_for(self, user_id: int) -> list[BotCommand]:
        entries: list[tuple[str, str]] = list(self._base)
        for module_id, spec in self._core.modules.items():
            if not self._core.acl.can(user_id, module_id):
                continue
            entries.extend(self._entries_for(module_id, user_id))
        return [BotCommand(name, desc) for name, desc in _clean(entries)[:MAX_COMMANDS]]

    def _default_entries(self) -> list[tuple[str, str]]:
        """全局作用域：基础命令 + 未指定会话作用域的模块片段。"""
        entries: list[tuple[str, str]] = list(self._base)
        for module_id, spec in self._core.modules.items():
            if module_id in self._scopes:
                continue  # 该模块按会话下发
            entries.extend(self._entries_for(module_id, 0))
        return _clean(entries)[:MAX_COMMANDS]

    # ---------- 下发 ----------
    async def apply(
        self,
        bot,
        *,
        force: bool = False,
        chats: Optional[Sequence[int]] = None,
    ) -> bool:
        """把菜单下发到 Telegram；返回 False 表示至少一个作用域失败（不致命）。"""
        payloads: list[tuple[str, Any, list[tuple[str, str]]]] = [
            ("default", None, self._default_entries())
        ]

        chat_scope: set[int] = set(int(c) for c in (chats or []))
        for ids in self._scopes.values():
            chat_scope.update(ids)
        for chat_id in sorted(chat_scope):
            entries = _clean(
                [
                    (cmd.command, cmd.description)
                    for cmd in self.render_for(chat_id)
                ]
            )[:MAX_COMMANDS]
            payloads.append(("chat:%d" % chat_id, BotCommandScopeChat(chat_id=chat_id), entries))

        ok = True
        for key, scope, entries in payloads:
            signature = tuple(entries)
            if not force and self._applied.get(key) == signature:
                continue
            commands = [BotCommand(name, desc) for name, desc in entries]
            try:
                await bot.set_my_commands(commands, scope=scope)
            except Exception as exc:
                ok = False
                log.warning("下发命令菜单失败（scope=%s）：%s", key, exc)
                continue
            self._applied[key] = signature
            log.info("命令菜单已更新：scope=%s，%d 条", key, len(commands))
        return ok


__all__ = ["MenuManager", "MAX_COMMANDS"]
