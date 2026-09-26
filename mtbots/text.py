"""统一文本工具：转义 / 分片 / 进度条 / 时间人性化 / 面板符号。

三个模块的渲染函数都从这里取，保证同一套符号语义：

    ✅ 成功   ❌ 失败   ⚠️ 接近阈值   ⛔️ 超阈值   ⏳ 进行中   ⏱️ 超时
"""

from __future__ import annotations

import html
import re
from datetime import datetime

#: 消息长度上限（Telegram 硬上限 4096，留安全余量）
MESSAGE_LIMIT = 3800
SECTION_SEP = "───────────────"

#: 面板状态符号语义（各模块渲染时必须复用，不要自造）
OK = "✅"
FAIL = "❌"
WARN = "⚠️"
OVER = "⛔️"
PENDING = "⏳"
TIMEOUT = "⏱️"

_TAG_RE = re.compile(r"</?([A-Za-z][A-Za-z0-9]*)[^>]*?(/?)>")
_VOID_TAGS = {"br"}

#: Telegram HTML 模式真正支持的标签，其余 `<xxx>` 一律当字面量转义（见 :func:`safe_html`）
TELEGRAM_TAGS = (
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ins",
    "s",
    "strike",
    "del",
    "span",
    "tg-spoiler",
    "tg-emoji",
    "a",
    "code",
    "pre",
    "blockquote",
)
#: 匹配「不是合法标签开头」的那个 `<`（零宽断言，只替换 `<` 本身）
_BAD_LT_RE = re.compile(
    r"<(?!/?(?:%s)(?:[ />]))" % "|".join(TELEGRAM_TAGS),
    re.IGNORECASE,
)


def safe_html(text: str) -> str:
    """转义白名单之外的 `<`，让任意文案都能安全地用 HTML 模式发出去。

    典型踩坑：文案里出现 `<盘名>`、`a < b`、`<你的数据目录>`，Telegram 会整条消息
    返回 `BadRequest: Can't parse entities: unsupported start tag "盘名"`，
    于是面板刷不出来、按钮看起来像失灵。出口处统一兜住这类字面量。
    """
    if not text or "<" not in text:
        return text or ""
    return _BAD_LT_RE.sub("&lt;", text)


def strip_tags(text: str) -> str:
    """去掉 HTML 标签并反转义实体：给 `parse_mode=None` 的兜底文本用。"""
    return html.unescape(_TAG_RE.sub("", text or ""))


def esc(value: object) -> str:
    """HTML 转义（None -> 空串）。"""
    if value is None:
        return ""
    return html.escape(str(value))


def now_stamp() -> str:
    """面板底部时间戳：HH:MM:SS。"""
    return datetime.now().strftime("%H:%M:%S")


def section() -> str:
    return SECTION_SEP


def progress_bar(percent: float, length: int = 16, filled: str = "▓", empty: str = "░") -> str:
    """文本进度条；percent 会被夹在 0–100。"""
    try:
        pct = float(percent)
    except (TypeError, ValueError):
        pct = 0.0
    pct = max(0.0, min(100.0, pct))
    done = int(round(length * pct / 100.0))
    return filled * done + empty * (length - done)


def humanize_delta(seconds: float) -> str:
    """把秒差变成人话：'刚刚' / '3 分钟前' / '1 小时 5 分钟前'。"""
    try:
        sec = float(seconds)
    except (TypeError, ValueError):
        return "未知"
    if sec < 0:
        sec = 0.0
    if sec < 45:
        return "刚刚"
    if sec < 3600:
        return "%d 分钟前" % int(sec // 60)
    if sec < 86400:
        hours = int(sec // 3600)
        minutes = int((sec % 3600) // 60)
        return ("%d 小时 %d 分钟前" % (hours, minutes)) if minutes else ("%d 小时前" % hours)
    days = int(sec // 86400)
    return "%d 天前" % days


def humanize_duration(seconds: float) -> str:
    """把「已经跑了多久」变成人话：'1 分 20 秒'。"""
    try:
        sec = int(float(seconds))
    except (TypeError, ValueError):
        return "未知"
    sec = max(0, sec)
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        minutes, rest = divmod(sec, 60)
        return ("%d 分 %d 秒" % (minutes, rest)) if rest else ("%d 分钟" % minutes)
    hours, rest = divmod(sec, 3600)
    minutes = rest // 60
    return ("%d 小时 %d 分" % (hours, minutes)) if minutes else ("%d 小时" % hours)


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


def _open_tags(chunk: str) -> list[str]:
    """返回 chunk 结束时仍未闭合的标签名（栈序）。"""
    stack: list[str] = []
    for match in _TAG_RE.finditer(chunk):
        name = match.group(1).lower()
        if name in _VOID_TAGS:
            continue
        if match.group(0).startswith("</"):
            if name in stack:
                # 只弹出最近的同名标签，容忍 <b><code>…</b></code> 这类乱序
                idx = len(stack) - 1 - stack[::-1].index(name)
                del stack[idx:]
        elif not match.group(2):
            stack.append(name)
    return stack


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """按长度分片，尽量在换行处断开，并补齐/重开 HTML 标签。

    面板里大量使用 <b>/<code>，硬切会把标签切一半导致 Telegram 直接报错，
    所以这里在分片边界做闭合与重开。
    """
    if limit <= 0:
        limit = MESSAGE_LIMIT
    text = text or ""
    if len(text) <= limit:
        return [text] if text else [""]

    chunks: list[str] = []
    rest = text
    carry: list[str] = []  # 上一片未闭合、需要在下一片重开的标签
    while rest:
        prefix = "".join("<%s>" % t for t in carry)
        budget = max(1, limit - len(prefix))
        if len(prefix) + len(rest) <= limit:
            piece = prefix + rest
            rest = ""
        else:
            window = rest[:budget]
            cut = window.rfind("\n")
            if cut < budget // 2:  # 换行太靠前就不值得断行，退化为硬切
                cut = budget
            piece_body = rest[:cut]
            rest = rest[cut:]
            if rest.startswith("\n"):
                rest = rest[1:]
                piece_body = piece_body.rstrip("\n")
            piece = prefix + piece_body
            open_now = _open_tags(piece)
            if open_now:
                piece += "".join("</%s>" % t for t in reversed(open_now))
            carry = open_now
        chunks.append(piece)
    return chunks or [""]


def chunk_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """纯文本分片（不加 HTML 修复），兼容旧的 LitePan `_chunk_text` 契约。"""
    text = text or ""
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


__all__ = [
    "MESSAGE_LIMIT",
    "SECTION_SEP",
    "OK",
    "FAIL",
    "WARN",
    "OVER",
    "PENDING",
    "TIMEOUT",
    "TELEGRAM_TAGS",
    "esc",
    "safe_html",
    "strip_tags",
    "now_stamp",
    "section",
    "progress_bar",
    "humanize_delta",
    "humanize_duration",
    "truncate",
    "split_message",
    "chunk_text",
]
