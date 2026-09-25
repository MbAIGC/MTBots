"""统一面板层：一条会话一个模块只保留一条面板消息 + 面包屑 + 🏠 返回 + 两步确认。

交互设计（three-bots-merge-ux.md 图 1）的三条硬规则都在这里实现：

1. **原地编辑**：模块面板永远 `edit_message_text` 同一条消息，不刷屏；
   超长 / 编辑失败才新发（并自动分片）。
2. **面包屑常驻**：头部 `🏠 › 🐳 Docker 管理`，`🏠 返回` 永远在键盘角落里，
   于是三个模块能和平共处而不需要命令前缀。
3. **两步确认**：破坏性操作的确认按钮绑定发起人 + 60 秒过期，过期后作废。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from .text import MESSAGE_LIMIT, SECTION_SEP, esc, now_stamp, split_message

log = logging.getLogger("mtbots.panels")

CONFIRM_TTL = 60.0
_CB_LIMIT = 800
_CB_TRIM = 200

#: 回调载荷内存表（Telegram 回调数据限 64 字节，装不下规则名/项目名这类真实载荷）
_CB_PAYLOAD: dict[str, dict] = {}

#: 各模块回调命名空间
PREFIX_DOCKER = "d"
PREFIX_LITEPAN = "p"
PREFIX_CLINE = "c"
PREFIX_NAV = "nav"
PREFIX_JOB = "job"


# ==================== 回调数据 ====================
def cb(prefix: str, action: str, payload: Optional[dict] = None) -> str:
    """生成短回调数据：'d|page_turn|1a2b3c4d'，真实载荷存内存表。"""
    if payload is None:
        return "%s|%s" % (prefix, action)
    key = "%s|%s|%s" % (prefix, action, uuid.uuid4().hex[:8])
    _CB_PAYLOAD[key] = dict(payload)
    if len(_CB_PAYLOAD) > _CB_LIMIT:
        for stale in list(_CB_PAYLOAD.keys())[:_CB_TRIM]:
            _CB_PAYLOAD.pop(stale, None)
    return key


def cb_simple(prefix: str, action: str, *parts: Any) -> str:
    """不需要内存表的短回调：'d|page_turn|2'（纯数字/短 id 用这个）。"""
    items = [str(p) for p in parts]
    return "|".join([prefix, action, *items])


def cb_payload(data: str) -> Optional[dict]:
    return _CB_PAYLOAD.get(data)


def cb_args(data: str) -> tuple[str, str]:
    """('d', 'page_turn')；解析不出来时返回 ('', data)。"""
    parts = (data or "").split("|")
    if len(parts) < 2:
        return "", data or ""
    return parts[0], parts[1]


def cb_parts(data: str) -> list[str]:
    return (data or "").split("|")


def cb_parse(data: str) -> tuple[str, str, Optional[dict]]:
    prefix, action = cb_args(data)
    return prefix, action, _CB_PAYLOAD.get(data or "")


def cb_expired(data: str) -> bool:
    """带内存载荷的回调，若载荷已被清理就是「菜单已过期」。"""
    prefix, action = cb_args(data)
    if data.count("|") >= 2 and _CB_PAYLOAD.get(data) is None:
        return True
    return False


def nav_home() -> str:
    return cb_simple(PREFIX_NAV, "home")


def nav_open(module_id: str) -> str:
    return cb_simple(PREFIX_NAV, "open", module_id)


def nav_status(module_id: str) -> str:
    return cb_simple(PREFIX_NAV, "status", module_id)


def nav_jobs() -> str:
    return cb_simple(PREFIX_NAV, "jobs")


def nav_help() -> str:
    return cb_simple(PREFIX_NAV, "help")


def job_cancel(job_id: str) -> str:
    return cb_simple(PREFIX_JOB, "cancel", job_id)


BACK_LABEL = "🏠 返回"


# ==================== 面板管理 ====================
class PanelManager:
    def __init__(self, core: Any = None):
        self._core = core
        self._panels: dict[tuple[int, str], int] = {}
        self._pending: dict[str, tuple[int, float]] = {}

    def attach(self, core: Any) -> None:
        self._core = core

    # ---------- 发送 / 编辑 ----------
    async def send(
        self,
        chat_id: int,
        text: str,
        keyboard: Optional[InlineKeyboardMarkup] = None,
        *,
        parse_mode: str = ParseMode.HTML,
        bot: Any = None,
        disable_preview: bool = True,
        limit: int = MESSAGE_LIMIT,
    ):
        if bot is None:
            raise RuntimeError("PanelManager.send 需要 bot（或从 update 取）")
        chunks = split_message(text, limit)
        message = None
        for idx, chunk in enumerate(chunks):
            last = idx == len(chunks) - 1
            message = await bot.send_message(
                chat_id,
                chunk,
                parse_mode=parse_mode,
                reply_markup=keyboard if last else None,
                disable_web_page_preview=disable_preview,
            )
        return message

    async def render(
        self,
        module_id: str,
        update: Update,
        text: str,
        keyboard: Optional[InlineKeyboardMarkup] = None,
        *,
        force_new: bool = False,
        chat_id: Optional[int] = None,
        parse_mode: str = ParseMode.HTML,
        footer: bool = True,
        limit: int = MESSAGE_LIMIT,
    ) -> None:
        """把 text 渲染成本会话本模块的唯一面板消息。"""
        chat = update.effective_chat if update is not None else None
        target_chat = chat_id if chat_id is not None else (chat.id if chat else None)
        if target_chat is None:
            log.warning("render 缺少 chat_id，已丢弃：module=%s", module_id)
            return

        bot = None
        if update is not None:
            try:
                bot = update.get_bot()
            except RuntimeError:
                bot = None
        if bot is None:
            log.warning("render 拿不到 bot，已丢弃：module=%s", module_id)
            return

        body = self._decorate(module_id, text, footer=footer)
        markup = self._with_back(module_id, keyboard)

        key = (int(target_chat), module_id)
        existing = None if force_new else self._panels.get(key)

        if existing is not None and len(body) <= limit:
            try:
                await bot.edit_message_text(
                    chat_id=target_chat,
                    message_id=existing,
                    text=body,
                    parse_mode=parse_mode,
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
                return
            except BadRequest as exc:
                reason = str(exc).lower()
                if "not modified" in reason:
                    return
                log.info("面板编辑失败，改为新发：module=%s chat=%s（%s）", module_id, target_chat, exc)
            except TelegramError as exc:
                log.info("面板编辑异常，改为新发：module=%s chat=%s（%s）", module_id, target_chat, exc)

        message = await self.send(target_chat, body, markup, parse_mode=parse_mode, bot=bot, limit=limit)
        if message is not None:
            self._panels[key] = message.message_id

    def forget(self, chat_id: int, module_id: Optional[str] = None) -> None:
        if module_id is None:
            for key in [k for k in self._panels if k[0] == int(chat_id)]:
                self._panels.pop(key, None)
        else:
            self._panels.pop((int(chat_id), module_id), None)

    def tracked(self, chat_id: int, module_id: str) -> Optional[int]:
        return self._panels.get((int(chat_id), module_id))

    # ---------- 两步确认 ----------
    async def ask_confirm(
        self,
        module_id: str,
        update: Update,
        text: str,
        confirm_data: str,
        *,
        cancel_data: Optional[str] = None,
        confirm_label: str = "✅ 确认",
        cancel_label: str = "❌ 取消",
        ttl: float = CONFIRM_TTL,
        owner_id: Optional[int] = None,
    ) -> None:
        user = update.effective_user if update is not None else None
        owner = owner_id if owner_id is not None else (user.id if user else 0)
        self.expire_pending()
        self._pending[confirm_data] = (int(owner), time.monotonic() + ttl)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(confirm_label, callback_data=confirm_data),
                    InlineKeyboardButton(cancel_label, callback_data=cancel_data or nav_home()),
                ]
            ]
        )
        await self.render(module_id, update, text, keyboard)

    def validate_confirm(self, query, data: str, *, consume: bool = True) -> tuple[bool, str]:
        """校验确认回调：本人 + 未过期。返回 (ok, 失败提示)。"""
        entry = self._pending.get(data)
        if entry is None:
            return False, "⚠️ 该确认已失效（可能已使用或 Bot 重启），请重新发起操作。"
        owner_id, deadline = entry
        user = getattr(query, "from_user", None)
        if user is not None and int(user.id) != owner_id:
            return False, "⚠️ 只有发起本次操作的用户可以确认。"
        if time.monotonic() > deadline:
            self._pending.pop(data, None)
            return False, "⏱️ 确认已超时（60 秒），请重新发起操作。"
        if consume:
            self._pending.pop(data, None)
        return True, ""

    def expire_pending(self) -> int:
        now = time.monotonic()
        stale = [k for k, (_, deadline) in self._pending.items() if deadline < now]
        for key in stale:
            self._pending.pop(key, None)
        return len(stale)

    # ---------- 键盘 ----------
    def home_keyboard(self, user_id: int) -> InlineKeyboardMarkup:
        """首页按钮：只渲染有权限的模块（无权限的模块直接不显示）。"""
        rows: list[list[InlineKeyboardButton]] = []
        if self._core is None:
            return InlineKeyboardMarkup(rows)
        row: list[InlineKeyboardButton] = []
        for spec in self._core.modules.values():
            if not self._core.acl.can(user_id, spec.id):
                continue
            row.append(InlineKeyboardButton("%s %s" % (spec.icon, spec.title), callback_data=nav_open(spec.id)))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append(
            [
                InlineKeyboardButton("🧰 任务中心", callback_data=nav_jobs()),
                InlineKeyboardButton("❓ 帮助", callback_data=nav_help()),
            ]
        )
        return InlineKeyboardMarkup(rows)

    # ---------- 内部 ----------
    def _decorate(self, module_id: str, text: str, *, footer: bool = True) -> str:
        if module_id == "home":
            header = "🏠 <b>控制台</b> · MTBots"
        elif module_id == "help":
            header = "🏠 › ❓ 帮助"
        else:
            spec = None
            if self._core is not None and self._core.has(module_id):
                spec = self._core.get(module_id)
            if spec is not None:
                header = "🏠 › %s <b>%s</b>" % (spec.icon, esc(spec.title))
            else:
                header = "🏠 › %s" % esc(module_id)
        parts = [header, SECTION_SEP, text.rstrip()]
        if footer:
            parts.extend([SECTION_SEP, "🔄 %s" % now_stamp()])
        return "\n".join(parts)

    def _with_back(
        self, module_id: str, keyboard: Optional[InlineKeyboardMarkup]
    ) -> Optional[InlineKeyboardMarkup]:
        if module_id in ("home", "help"):
            return keyboard
        rows: list[list[InlineKeyboardButton]] = []
        if keyboard is not None:
            rows = [list(row) for row in keyboard.inline_keyboard]
        flat = [btn.callback_data for row in rows for btn in row]
        if nav_home() in flat:
            return InlineKeyboardMarkup(rows)
        back = InlineKeyboardButton(BACK_LABEL, callback_data=nav_home())
        if rows:
            rows[-1] = [*rows[-1], back]
        else:
            rows = [[back]]
        return InlineKeyboardMarkup(rows)


__all__ = [
    "CONFIRM_TTL",
    "PanelManager",
    "cb",
    "cb_simple",
    "cb_payload",
    "cb_args",
    "cb_parts",
    "cb_parse",
    "cb_expired",
    "nav_home",
    "nav_open",
    "nav_status",
    "nav_jobs",
    "nav_help",
    "job_cancel",
    "BACK_LABEL",
    "PREFIX_DOCKER",
    "PREFIX_LITEPAN",
    "PREFIX_CLINE",
    "PREFIX_NAV",
    "PREFIX_JOB",
]
