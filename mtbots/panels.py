"""统一面板层：一条会话只保留一条面板消息 + 面包屑 + 🏠 返回 + 两步确认。

交互设计（three-bots-merge-ux.md 图 1）的三条硬规则都在这里实现：

1. **面板唯一 + 视线内更新**：home / docker / litepan / cline 共用同一条面板消息，
   所以「🏠 返回」改的就是用户正在看的那条消息；点按钮时原地编辑不刷屏，
   命令（`/start`、`/d_list`…）触发时新发一条到最底部并删掉旧面板——
   否则旧面板停在命令上方，编辑了也看不见（「第二次 /start 没反应」就是这么来的）。
2. **面包屑常驻**：头部 `🏠 › 🐳 Docker 管理`，`🏠 返回` 永远在键盘角落里，
   于是三个模块能和平共处而不需要命令前缀。
3. **两步确认**：破坏性操作的确认按钮绑定发起人 + 60 秒过期，过期后作废。

出站 HTML 的兜底（非法 `<` 转义、解析失败降级纯文本）在 :class:`mtbots.bot.SafeBot`，
不在这里——那样连模块自己的 `reply_text` 也一起兜住了。
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

#: 完成面上「下一步」一行最多放几个按钮（放不下就整体不显示，退回只有 🏠 返回）
NEXT_ACTIONS_LIMIT = 3


def merge_keyboards(*markups: Optional[InlineKeyboardMarkup]) -> Optional[InlineKeyboardMarkup]:
    """把几个键盘按顺序拼成一个（`None` 直接跳过）；全空则返回 `None`。

    收尾面既要给本模块的动作（🔄 再跑一次），又要给一行跨模块入口，分成两个来源拼起来最省事。
    """
    rows: list[list[InlineKeyboardButton]] = []
    for markup in markups:
        if markup is not None:
            rows.extend(list(row) for row in markup.inline_keyboard)
    return InlineKeyboardMarkup(rows) if rows else None


def next_actions_keyboard(
    core: Any,
    user_id: Optional[int],
    current: Optional[str] = None,
    *,
    limit: int = NEXT_ACTIONS_LIMIT,
) -> Optional[InlineKeyboardMarkup]:
    """长任务收尾时的一行「下一步」：其他模块的入口。

    合并后三个 Bot 共用一个面板，任务跑完顺手跳到另一条线是最常走的路，所以收尾面上
    直接给按钮，而不是让用户先 🏠 返回 再找。规则：

    * 只列**已启用**且**本人有权限**的模块，当前模块除外（面板就在眼前，再给一个按钮没意义）；
    * 不放 🧰 任务中心：收尾行紧跟着 `🏠 返回` 被追加到同一行，再加一个就是 4 个按钮的按钮墙，
      而 首页 本来就有 任务中心，一步可达；
    * 模块入口超过 `limit` 个就返回 ``None``，调用方退回只有 🏠 返回 的键盘——
      宁可少给按钮，也不让键盘挤成两行乱糟糟的。
    """
    if core is None:
        return None
    icons = core.icons()
    titles = core.titles()
    buttons: list[InlineKeyboardButton] = []
    for module_id in core.module_ids():
        if module_id == current:
            continue
        if not core.can(user_id, module_id):  # 认不出用户就当没权限（默认拒绝）
            continue
        title = (titles.get(module_id) or module_id).split()[0]
        buttons.append(
            InlineKeyboardButton(
                "%s %s" % (icons.get(module_id, "•"), title),
                callback_data=nav_open(module_id),
            )
        )
    if not buttons or len(buttons) > limit:
        return None
    return InlineKeyboardMarkup([buttons])


# ==================== 面板管理 ====================
class PanelManager:
    def __init__(self, core: Any = None):
        self._core = core
        self._panels: dict[int, int] = {}
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

        key = int(target_chat)
        previous = self._panels.get(key)
        # 一条会话只保留**一条**面板消息（home / docker / litepan / cline 共用同一条），于是
        # 「🏠 返回」就是把用户正在看的那条消息原地改成首页，一定看得见。
        #
        # 只有「点按钮」触发的渲染才就地编辑：用户视线就在这条消息上。
        # 命令（/start、/d_list…）触发的渲染一律新发到聊天最底部，并删掉旧面板——否则旧面板
        # 停在用户刚发的命令**上方**，编辑了也看不见，表现就是「第二次 /start 没反应」。
        in_place = (not force_new) and previous is not None
        if in_place and update is not None and update.callback_query is None:
            in_place = False

        if in_place and len(body) <= limit:
            try:
                await bot.edit_message_text(
                    chat_id=target_chat,
                    message_id=previous,
                    text=body,
                    parse_mode=parse_mode,
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
                return
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return
                log.info("面板编辑失败，改为新发：module=%s chat=%s（%s）", module_id, target_chat, exc)
            except TelegramError as exc:
                log.info("面板编辑异常，改为新发：module=%s chat=%s（%s）", module_id, target_chat, exc)

        message = await self.send(target_chat, body, markup, parse_mode=parse_mode, bot=bot, limit=limit)
        if message is not None:
            self._panels[key] = message.message_id
            if previous is not None and previous != message.message_id:
                await self._delete_quietly(bot, target_chat, previous)

    async def _delete_quietly(self, bot: Any, chat_id: int, message_id: int) -> None:
        """删旧面板：失败就算了（群里没删消息权限、消息超过 48 小时都会失败）。"""
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError as exc:
            log.debug("旧面板删除失败（忽略）：chat=%s msg=%s（%s）", chat_id, message_id, exc)

    def forget(self, chat_id: int, module_id: Optional[str] = None) -> None:
        """forget: 忘掉本会话的面板（module_id 参数保留仅为兼容旧调用）。"""
        self._panels.pop(int(chat_id), None)

    def tracked(self, chat_id: int, module_id: Optional[str] = None) -> Optional[int]:
        return self._panels.get(int(chat_id))

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
    "NEXT_ACTIONS_LIMIT",
    "merge_keyboards",
    "next_actions_keyboard",
    "PREFIX_DOCKER",
    "PREFIX_LITEPAN",
    "PREFIX_CLINE",
    "PREFIX_NAV",
    "PREFIX_JOB",
]
