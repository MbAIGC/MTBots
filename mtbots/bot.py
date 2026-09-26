"""出站安全网：HTML 消息在发出前统一转义非法 `<`，解析失败自动降级成纯文本。

**为什么要单开一层 Bot。** 模块里有几十处 `reply_text(..., parse_mode=HTML)` / `edit_message_text(...)`，
靠「记得调 esc()」是防不住的：LitePan 的 `/refresh <盘名>` 就把
`Can't parse entities: unsupported start tag "盘名"` 发到了线上——整条消息被 Telegram 拒绝，
面板刷不出来，用户看到的就是「点了没反应 / 返回无效」。

`ExtBot.send_message` / `edit_message_text` 是所有出口的汇聚点（`Message.reply_text`、
`CallbackQuery.edit_message_text` 内部都调这两个方法），在这里兜一层最省事、也不可能漏。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from telegram.error import BadRequest
from telegram.ext import ExtBot

from .text import safe_html, strip_tags

log = logging.getLogger("mtbots.bot")

#: Telegram 因为 HTML 不合法而拒绝整条消息时的错误特征
_PARSE_ERRORS = (
    "can't parse entities",
    "unsupported start tag",
    "can't find end tag",
    "unsupported tag",
    "unclosed",
)


def _is_html(parse_mode: Any) -> bool:
    return parse_mode is not None and str(parse_mode).upper() == "HTML"


def _is_parse_error(exc: BaseException) -> bool:
    reason = str(exc).lower()
    return any(mark in reason for mark in _PARSE_ERRORS)


class SafeBot(ExtBot):
    """:class:`telegram.ext.ExtBot` + 出站 HTML 兜底。

    行为只加不减：转义在**发出前**做，`parse_mode` 不是 HTML 时原样透传；
    真的还是解析失败（例如标签不闭合）就退化成纯文本再发一次，绝不把整条消息丢掉。
    """

    async def send_message(  # type: ignore[override]
        self,
        chat_id: Any,
        text: str,
        parse_mode: Optional[str] = None,
        **kwargs: Any,
    ):
        html = _is_html(parse_mode)
        body = safe_html(text) if html else text
        try:
            return await super().send_message(chat_id, body, parse_mode=parse_mode, **kwargs)
        except BadRequest as exc:
            if not (html and _is_parse_error(exc)):
                raise
            log.warning("HTML 解析失败，降级为纯文本重发：%s", exc)
            return await super().send_message(chat_id, strip_tags(body), parse_mode=None, **kwargs)

    async def edit_message_text(  # type: ignore[override]
        self,
        text: str,
        chat_id: Any = None,
        message_id: Optional[int] = None,
        inline_message_id: Optional[str] = None,
        parse_mode: Optional[str] = None,
        **kwargs: Any,
    ):
        html = _is_html(parse_mode)
        body = safe_html(text) if html else text
        try:
            return await super().edit_message_text(
                body,
                chat_id=chat_id,
                message_id=message_id,
                inline_message_id=inline_message_id,
                parse_mode=parse_mode,
                **kwargs,
            )
        except BadRequest as exc:
            if not (html and _is_parse_error(exc)):
                raise
            log.warning("HTML 解析失败，降级为纯文本重发（编辑）：%s", exc)
            return await super().edit_message_text(
                strip_tags(body),
                chat_id=chat_id,
                message_id=message_id,
                inline_message_id=inline_message_id,
                parse_mode=None,
                **kwargs,
            )


__all__ = ["SafeBot"]
