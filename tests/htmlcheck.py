"""离线校验一段文本能不能被 Telegram 的 HTML 解析器接受。

Telegram 的规则：标签必须来自白名单、必须闭合，`<` 后面不是合法标签就整条消息报
`BadRequest: Can't parse entities: unsupported start tag "xxx"`。这里用同样的规则离线检查，
于是「文案里写了 `<盘名>`」这类事故在单元测试里就会挂，而不是等上线后用户点不动面板。
"""

from __future__ import annotations

import re
import unittest

from mtbots.text import TELEGRAM_TAGS, safe_html, split_message

_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)((?:\s[^<>]*)?)(/?)>")


def html_problems(text: str) -> list[str]:
    """返回所有问题（空列表 = 能安全发出）。"""
    problems: list[str] = []
    stack: list[str] = []
    pos = 0
    for match in _TAG_RE.finditer(text):
        if "<" in text[pos : match.start()]:
            start = max(0, match.start() - 24)
            problems.append("裸 `<`：%r" % text[start : match.start() + 24])
        pos = match.end()
        closing, name, _attrs, self_closing = match.groups()
        name = name.lower()
        if name not in TELEGRAM_TAGS:
            problems.append("不支持的标签 <%s>" % name)
            continue
        if self_closing:
            continue
        if closing:
            if not stack or stack[-1] != name:
                problems.append("闭合不匹配 </%s>（当前 %s）" % (name, stack[-1] if stack else "空"))
            else:
                stack.pop()
        else:
            stack.append(name)
    if "<" in text[pos:]:
        idx = text.index("<", pos)
        problems.append("尾部裸 `<`（不是合法标签）：%r" % text[idx : idx + 24])
    problems.extend("未闭合 <%s>" % name for name in stack)
    return problems


def assert_html_valid(case: unittest.TestCase, text: str, note: str = "") -> None:
    """按「实际发出去的样子」（safe_html + 分片）断言。"""
    safe = safe_html(text)
    for index, chunk in enumerate(split_message(safe) or [safe]):
        problems = html_problems(chunk)
        case.assertFalse(
            problems,
            "%s（第 %d 片）\n%s\n---\n%s" % (note, index + 1, "；".join(problems), chunk[:400]),
        )


__all__ = ["assert_html_valid", "html_problems"]
