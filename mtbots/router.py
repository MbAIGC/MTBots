"""路由层：首页 / 面包屑导航 / 4 个冲突命令的消歧 / 帮助 / 任务中心 / 兜底救援。

三边命令取并集后**只有 4 个真冲突**（`/start` `/help` `/status` `/list`），
其余天然不冲突、原样保留（设计文档 §3）。所以这里做的事很少但很关键：

* 全局命令唯一化，冲突命令按「当前所处的模块」解释（设计文档 §5.4）；
* 首页是一张聚合卡片，按权限渲染（无权限的模块直接不显示）；
* `nav|…` 回调负责面包屑导航，`job|…` 负责任务中心；
* 最后注册一个兜底 MessageHandler，把全角斜杠 / 零宽字符 / 代码块包住的命令救回来。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .acl import normalize_module
from .core import Core, core_of
from .panels import cb_args, job_cancel, nav_help, nav_home, nav_jobs, nav_open, nav_status
from .text import SECTION_SEP, esc

log = logging.getLogger("mtbots.router")

#: 全局命令 -> 说明（菜单里排在模块片段之前）
BASE_COMMANDS: list[tuple[str, str]] = [
    ("start", "🏠 控制台首页"),
    ("help", "帮助（按权限显示）"),
    ("status", "当前模块状态 / 首页总览"),
    ("list", "当前模块列表"),
    ("jobs", "🧰 任务中心"),
    ("id", "我的 ID 与运行环境"),
]

#: 冲突命令的显式别名 -> (模块, 该模块的哪个面板)
ALIASES: dict[str, tuple[str, str]] = {
    "d_status": ("docker", "status"),
    "d_list": ("docker", "list"),
    "p_status": ("litepan", "status"),
    "p_list": ("litepan", "list"),
    "c_status": ("cline", "status"),
    "c_list": ("cline", "list"),
}


class _ArgsOverride:
    """只替换 context.args，其余属性转发给真实的 PTB context（沿用 ClinePass 的救援机制）。"""

    def __init__(self, context: ContextTypes.DEFAULT_TYPE, args: list[str]):
        self._context = context
        self.args = args

    def __getattr__(self, name: str) -> object:
        return getattr(self._context, name)


# ==================== 鉴权 ====================
async def _deny(update: Update, reason: str = "你不在白名单中，无法使用本 Bot。") -> None:
    user = update.effective_user
    text = "⛔️ %s\n你的用户 ID：<code>%s</code>\n（把它填进 <code>ALLOWED_USER_IDS</code> 即可启用）" % (
        esc(reason),
        esc(user.id if user else "未知"),
    )
    query = update.callback_query
    if query is not None:
        await query.answer("⛔️ 无权限", show_alert=True)
        return
    message = update.effective_message
    if message is not None:
        await message.reply_text(text, parse_mode=ParseMode.HTML)


async def ensure_allowed(core: Core, update: Update, *, module_id: Optional[str] = None) -> bool:
    """统一守卫：默认拒绝 + 模块权限。返回 True 表示可以继续。"""
    user = update.effective_user
    user_id = user.id if user else None
    if not core.acl.is_allowed(user_id):
        await _deny(update)
        return False
    if module_id and not core.acl.can(user_id, module_id):
        if update.callback_query is not None:
            await update.callback_query.answer("⛔️ 你没有该模块的权限", show_alert=True)
        else:
            await update.effective_message.reply_text(  # type: ignore[union-attr]
                "⛔️ 你没有 <b>%s</b> 模块的权限（角色：<code>%s</code>）。"
                % (esc(module_id), esc(core.acl.role(user_id) or "-")),
                parse_mode=ParseMode.HTML,
            )
        return False
    return True


# ==================== 首页 / 帮助 / ID / 任务中心 ====================
#: 模块没声明 `refresh_ttl` 时首页刷新的兜底间隔（秒）
DEFAULT_REFRESH_TTL = 15.0

#: 后台刷新任务（持有强引用，避免被 GC 掉导致「刷新跑一半没了」）
_HOME_TASKS: set[Any] = set()


def _refresh_key(module_id: str, user_id: int) -> str:
    """刷新记账的键：Cline 的额度是**按用户**的，所以模块 id 还要带上用户。"""
    return "%s|%s" % (module_id, int(user_id))


def _refresh_book(core: Core) -> dict[str, dict]:
    book = core.data.get("home_refresh")
    if not isinstance(book, dict):
        book = {}
        core.data["home_refresh"] = book
    return book


def _refresh_ttl(spec: Any) -> float:
    ttl = getattr(spec, "refresh_ttl", DEFAULT_REFRESH_TTL)
    try:
        ttl = float(ttl)
    except (TypeError, ValueError):
        ttl = DEFAULT_REFRESH_TTL
    return ttl if ttl > 0 else 0.0


def _pending_refreshes(core: Core, user_id: int, *, force: bool = False) -> list[Any]:
    """挑出「该刷新」的模块：有 refresh 钩子 + 本人有权限 + 缓存已过 TTL（或强制）。"""
    now = time.monotonic()
    book = _refresh_book(core)
    pending: list[Any] = []
    for spec in core.modules.values():
        if spec.refresh is None or not core.acl.can(user_id, spec.id):
            continue
        entry = book.get(_refresh_key(spec.id, user_id)) or {}
        if force or (now - float(entry.get("at") or 0.0)) >= _refresh_ttl(spec):
            pending.append(spec)
    return pending


def _mark_refreshing(text: str, mark: str = "⏳ 刷新中") -> str:
    """把「正在刷新」标在模块那一块的第一行（多行摘要也不会标到奇怪的位置）。"""
    head, sep, tail = (text or "").partition("\n")
    return "%s  %s%s%s" % (head, mark, sep, tail)


async def _home_text(core: Core, user_id: int, pending: Optional[list[Any]] = None) -> str:
    """首页正文：每行一个模块；`pending` 里的模块追加「⏳ 刷新中」。"""
    pending_ids = {spec.id for spec in (pending or [])}
    lines: list[str] = []
    for spec in core.modules.values():
        if not core.acl.can(user_id, spec.id):
            continue
        line = None
        if spec.summary is not None:
            try:
                line = await spec.summary(core, user_id)
            except Exception as exc:  # 首页绝不能因为某个模块出错而打不开
                log.warning("模块 %s 的首页摘要失败：%s", spec.id, exc)
        if not line:
            line = "%s <b>%s</b> · 点击进入" % (spec.icon, esc(spec.title))
        lines.append(_mark_refreshing(line) if spec.id in pending_ids else line)
    return "\n".join(lines) if lines else "没有你可用的模块，请联系管理员检查权限配置。"


async def _refresh_one(core: Core, spec: Any, user_id: int, force: bool) -> None:
    await spec.refresh(core, user_id, force)


async def _run_home_refresh(
    core: Core,
    bot: Any,
    chat_id: int,
    user_id: int,
    specs: list[Any],
    force: bool,
    expect_message: Optional[int] = None,
) -> None:
    """后台刷新 + 回填面板：先让首页秒开，数据到位后再原地改这一条消息。"""
    book = _refresh_book(core)
    try:
        results = await asyncio.gather(
            *(_refresh_one(core, spec, user_id, force) for spec in specs),
            return_exceptions=True,
        )
        for spec, result in zip(specs, results):
            if isinstance(result, BaseException):
                log.warning("首页刷新 %s 失败：%s", spec.id, result)
    finally:
        # 无论成败都记时间：失败的主机/接口不该被每次 /start 反复捶
        now = time.monotonic()
        for spec in specs:
            entry = book.setdefault(_refresh_key(spec.id, user_id), {})
            entry["busy"] = False
            entry["at"] = now

    # 刷新期间用户可能已经翻到别的面板：只有「人还停在首页」且「看的还是我发起时那条面板」
    # 才回填——否则会把人家正在看的 docker 列表原地改回首页。
    #
    # 注意顺序：`_home_text()` 里含 await（摘要可能读线程），所以它必须在检查**之前**算完，
    # 检查到 render 之间不能再有 await（render 读 `_panels` 之前也是纯同步代码）。
    try:
        text = await _home_text(core, user_id)
        if core.module_of_chat(chat_id) is not None:
            return
        if expect_message is not None and core.panels.tracked(chat_id) != expect_message:
            return
        await core.panels.render(
            "home", None, text, core.panels.home_keyboard(user_id), chat_id=chat_id, bot=bot
        )
    except Exception as exc:  # 回填失败不影响用户（他手上那份只是旧一点）
        log.warning("首页刷新回填失败：%s", exc)


def _task_finished(task: Any) -> None:
    """收尾后台刷新任务：取走异常（否则 asyncio 会打 "Task exception was never retrieved"）。"""
    _HOME_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("首页刷新任务异常：%s", exc)


def _claim_refreshes(core: Core, user_id: int, pending: list[Any]) -> list[Any]:
    """给还没在刷新的模块占上坑（busy），返回真正要刷的那些。

    同一个模块同时在飞的刷新只留一个；跨会话也去重（数据是模块级的，刷一次就够）。
    """
    book = _refresh_book(core)
    todo: list[Any] = []
    for spec in pending:
        entry = book.setdefault(_refresh_key(spec.id, user_id), {})
        if entry.get("busy"):
            continue
        entry["busy"] = True
        todo.append(spec)
    return todo


def _release_refreshes(core: Core, user_id: int, specs: list[Any]) -> None:
    """起不了后台任务时把占的坑退回去（否则那个模块会永远显示「⏳ 刷新中」）。"""
    book = _refresh_book(core)
    for spec in specs:
        book.setdefault(_refresh_key(spec.id, user_id), {})["busy"] = False


def _start_home_refresh(
    core: Core,
    bot: Any,
    chat_id: int,
    user_id: int,
    todo: list[Any],
    force: bool,
    expect_message: Optional[int] = None,
) -> bool:
    """把占好坑的模块交给一个后台任务；起不来就退坑并返回 False。"""
    if not todo:
        return False
    try:
        task = asyncio.get_running_loop().create_task(
            _run_home_refresh(core, bot, int(chat_id), int(user_id), todo, force, expect_message)
        )
    except RuntimeError as exc:  # 没有事件循环（离线调用）：放弃后台刷新，首页照常显示
        log.debug("首页刷新无法起后台任务：%s", exc)
        _release_refreshes(core, user_id, todo)
        return False
    _HOME_TASKS.add(task)
    task.add_done_callback(_task_finished)
    return True


async def home_panel(
    core: Core, update: Update, context: ContextTypes.DEFAULT_TYPE, *, force: bool = False
) -> None:
    """首页：先秒开（读缓存），再后台刷新 + 原地回填。

    `/start`、`/status`（无模块上下文时）都会走到这里，所以「打开首页 = 顺手刷新一遍数据」；
    `/list` 无模块上下文时按契约直接开 docker 列表，不经过这里。
    `force=True`（点 🔄 刷新）无视各模块的 `refresh_ttl`，无条件重拉。
    """
    if not await ensure_allowed(core, update):
        return
    user = update.effective_user
    chat = update.effective_chat
    core.set_module(chat.id, None)

    bot = getattr(context, "bot", None)
    # 拿不到 bot 就压根不占坑、也不标 ⏳：标记必须只反映「真的在刷」的模块，
    # 否则占坑没人退（后台任务起不来），那个模块会永远卡在「⏳ 刷新中」。
    todo = (
        _claim_refreshes(core, user.id, _pending_refreshes(core, user.id, force=force))
        if bot is not None
        else []
    )
    await core.panels.render(
        "home", update, await _home_text(core, user.id, todo), core.panels.home_keyboard(user.id)
    )
    if not todo:
        return
    # 记下「我发起刷新时用户在看的哪条面板」，回填前要确认还是它
    _start_home_refresh(
        core,
        bot,
        chat.id,
        user.id,
        todo,
        force,
        expect_message=core.panels.tracked(chat.id),
    )


async def help_panel(core: Core, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(core, update):
        return
    user = update.effective_user
    blocks: list[str] = [
        "<b>MTBots 控制台</b>：一个 Bot，三个模块。",
        "",
        "<b>🌐 全局命令</b>",
        "· <code>/start</code> 或 <code>/menu</code> — 🏠 首页总览",
        "· <code>/status</code> — 当前模块状态（无模块时=首页总览）",
        "· <code>/list</code> — 当前模块列表",
        "· <code>/jobs</code> — 🧰 任务中心（跨模块长任务）",
        "· <code>/id</code> — 我的 ID 与运行环境",
        "· <code>/cancel</code> — 取消当前操作",
        "",
        "<b>🧭 消歧别名</b>",
        "· <code>/d_status</code> <code>/d_list</code> — Docker",
        "· <code>/p_status</code> <code>/p_list</code> — LitePan",
        "· <code>/c_status</code> — Cline 额度",
    ]
    for spec in core.modules.values():
        if not core.acl.can(user.id, spec.id) or spec.help_text is None:
            continue
        try:
            block = spec.help_text(core, user.id)
        except Exception as exc:
            log.warning("模块 %s 的帮助渲染失败：%s", spec.id, exc)
            continue
        if block:
            blocks.extend(["", SECTION_SEP, block])
    await core.panels.render(
        "help",
        update,
        "\n".join(blocks),
        InlineKeyboardMarkup([[InlineKeyboardButton("🏠 返回", callback_data=nav_home())]]),
    )


async def id_panel(core: Core, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(core, update):
        return
    import socket

    user = update.effective_user
    chat = update.effective_chat
    lines = ["🆔 用户 ID：<code>%s</code>" % esc(user.id)]
    if chat is not None:
        lines.append("💬 会话 ID：<code>%s</code>（%s）" % (esc(chat.id), esc(chat.type)))
    lines.append("🎭 权限：%s" % esc(core.acl.describe(user.id)))
    lines.append("🏠 容器：<code>%s</code>　🗂 数据目录：<code>%s</code>" % (esc(socket.gethostname()), esc(core.settings.data_dir)))

    tasks = []
    specs = [
        spec
        for spec in core.modules.values()
        if spec.id_lines is not None and core.acl.can(user.id, spec.id)
    ]
    for spec in specs:
        tasks.append(spec.id_lines(core, user.id))  # type: ignore[misc]
    if tasks:
        for spec, result in zip(specs, await asyncio.gather(*tasks, return_exceptions=True)):
            if isinstance(result, Exception):
                lines.append("%s ❌ 自检失败：<code>%s</code>" % (spec.icon, esc(result)))
                continue
            lines.extend(result or [])
    await core.panels.render(
        "help",
        update,
        "\n".join(lines),
        InlineKeyboardMarkup([[InlineKeyboardButton("🏠 返回", callback_data=nav_home())]]),
    )


async def jobs_panel(core: Core, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(core, update):
        return
    rows: list[list[InlineKeyboardButton]] = []
    for job in core.jobs.running():
        rows.append(
            [
                InlineKeyboardButton(
                    "🛑 取消 %s %s" % (core.icons().get(job.module, ""), job.title[:20]),
                    callback_data=job_cancel(job.id),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("🔄 刷新", callback_data=nav_jobs()),
            InlineKeyboardButton("🏠 返回", callback_data=nav_home()),
        ]
    )
    await core.panels.render(
        "home", update, core.jobs.render(core.icons()), InlineKeyboardMarkup(rows)
    )


async def cancel_panel(core: Core, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(core, update):
        return
    chat = update.effective_chat
    core.pop_pending(chat.id)
    core.set_module(chat.id, None)
    message = update.effective_message
    if message is not None:
        await message.reply_text("🛑 已取消当前操作与等待中的输入。")
    await home_panel(core, update, context)


# ==================== 冲突命令消歧 ====================
def _spec_or_none(core: Core, module_id: Optional[str]):
    if not module_id or not core.has(module_id):
        return None
    return core.get(module_id)


async def status_command(core: Core, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/status`：有模块上下文就按模块解释，否则回首页总览。"""
    if not await ensure_allowed(core, update):
        return
    user = update.effective_user
    module_id = core.module_of_chat(update.effective_chat.id)
    spec = _spec_or_none(core, module_id)
    if spec is not None and spec.show_status is not None and core.acl.can(user.id, spec.id):
        await spec.show_status(core, update, context)
        return
    await home_panel(core, update, context)


async def list_command(core: Core, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/list`：Docker 中=项目、LitePan 中=规则、Cline 中/无模块=Docker 项目。"""
    if not await ensure_allowed(core, update):
        return
    user = update.effective_user
    module_id = core.module_of_chat(update.effective_chat.id)
    if module_id not in ("docker", "litepan"):
        module_id = "docker"
    spec = _spec_or_none(core, module_id)
    if spec is not None and spec.show_list is not None and core.acl.can(user.id, spec.id):
        await spec.show_list(core, update, context)
        return
    await home_panel(core, update, context)


def alias_handler(module_id: str, panel: str) -> Callable[..., Any]:
    """生成 `/d_status`、`/p_list` 这类永久别名处理器。"""

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        core = core_of(context)
        if not await ensure_allowed(core, update, module_id=module_id):
            return
        spec = _spec_or_none(core, module_id)
        target = None
        if spec is not None:
            target = spec.show_status if panel == "status" else spec.show_list
        if target is None:
            await update.effective_message.reply_text(  # type: ignore[union-attr]
                "⚠️ 模块 <b>%s</b> 未启用或暂不支持该面板。" % esc(module_id),
                parse_mode=ParseMode.HTML,
            )
            return
        core.set_module(update.effective_chat.id, module_id)
        await target(core, update, context)

    return handler


# ==================== 导航回调 ====================
async def callback_router(core: Core, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if not await ensure_allowed(core, update):
        return
    data = query.data or ""
    prefix, action = cb_args(data)

    if prefix == "nav":
        parts = data.split("|")
        if action == "home":
            await query.answer()
            await home_panel(core, update, context)
            return
        if action == "jobs":
            await query.answer()
            await jobs_panel(core, update, context)
            return
        if action == "help":
            await query.answer()
            await help_panel(core, update, context)
            return
        if action == "refresh":
            await query.answer("🔄 正在刷新…")
            await home_panel(core, update, context, force=True)
            return
        if action in ("open", "status") and len(parts) >= 3:
            module_id = normalize_module(parts[2])
            if not core.has(module_id):
                await query.answer("⚠️ 该模块未启用", show_alert=True)
                return
            if not core.acl.can(update.effective_user.id, module_id):
                await query.answer("⛔️ 你没有该模块的权限", show_alert=True)
                return
            spec = core.get(module_id)
            core.set_module(update.effective_chat.id, module_id)
            await query.answer()
            target = spec.open_panel if action == "open" else spec.show_status
            if target is None:
                target = spec.open_panel or spec.show_status or spec.show_list
            if target is None:
                await query.answer("⚠️ 该模块没有可打开的面板", show_alert=True)
                return
            await target(core, update, context)
            return
        await query.answer()
        return

    if prefix == "job":
        parts = data.split("|")
        if action == "cancel" and len(parts) >= 3:
            job_id = parts[2]
            job = core.jobs.get(job_id)
            if job is None:
                await query.answer("⚠️ 任务不存在或已结束", show_alert=True)
                return
            if job.chat_id is not None and job.chat_id != update.effective_chat.id:
                await query.answer("⛔️ 只能取消自己发起的任务", show_alert=True)
                return
            done = core.jobs.cancel(job_id)
            await query.answer("🛑 已发送取消信号" if done else "⏳ 任务已结束", show_alert=True)
            await jobs_panel(core, update, context)
            return
        await query.answer()
        return

    # 其它命名空间属于模块自己的 handler；这里只兜底应答，避免按钮一直转圈
    await query.answer()


# ==================== 命令文本修复（兜底救援用） ====================
# ClinePass 的那一套解析器最完整（全角斜杠 / 全角空格 / 零宽字符 / 代码块包裹）。
# 这里优先复用它；cline 模块未启用时退化为下面这份等价实现，保证兜底永远可用。
_INVISIBLE_CHARS = "\u200b\u200c\u200d\u2060\ufeff"
_SPACE_LIKE_CHARS = (
    "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u202f\u205f\u3000"
)
_COMMAND_WRAPPERS = "`'\"“”‘’"
_SUSPICIOUS_NAMES = {
    "\u3000": "全角空格 U+3000",
    "\u00a0": "不换行空格 U+00A0",
    "\u200b": "零宽空格 U+200B",
    "\u200c": "零宽不连字 U+200C",
    "\u200d": "零宽连字 U+200D",
    "\u2060": "词连接符 U+2060",
    "\ufeff": "BOM U+FEFF",
    "／": "全角斜杠 U+FF0F",
    "＼": "全角反斜杠 U+FF3C",
}


def _normalize_command_text(text: str, invisible: str = "delete") -> str:
    out = (text or "").strip()
    while out and out[:1] in _COMMAND_WRAPPERS:
        out = out[1:].lstrip()
    while out and out[-1:] in _COMMAND_WRAPPERS:
        out = out[:-1].rstrip()
    for ch in _SPACE_LIKE_CHARS:
        out = out.replace(ch, " ")
    for ch in _INVISIBLE_CHARS:
        out = out.replace(ch, " " if invisible == "space" else "")
    if out[:1] in ("／", "＼"):
        out = "/" + out[1:]
    return out.strip()


def _parse_command_tokens(text: str, invisible: str = "delete") -> tuple[str, list[str]]:
    tokens = _normalize_command_text(text, invisible).split()
    if not tokens:
        return "", []
    head = tokens[0]
    if head[:1] in ("/", "／", "＼"):
        head = head[1:]
    return head.split("@", 1)[0].lower(), tokens[1:]


def _parse_command_candidates(text: str) -> list[tuple[str, list[str]]]:
    first = _parse_command_tokens(text, "delete")
    second = _parse_command_tokens(text, "space")
    return [first] if first == second else [first, second]


def _suspicious_chars(text: str) -> list[str]:
    found: list[str] = []
    for ch in text or "":
        label = _SUSPICIOUS_NAMES.get(ch)
        if label is None and ord(ch) > 127 and not ch.isprintable():
            label = "U+%04X" % ord(ch)
        if label and label not in found:
            found.append(label)
    return found


try:  # pragma: no cover - 取决于 cline 模块是否启用
    from .features.cline.core import (  # type: ignore
        parse_command_candidates,
        parse_command_tokens,
        suspicious_chars,
    )
except Exception:  # cline 未启用 / 依赖缺失时用内置兜底
    parse_command_candidates = _parse_command_candidates  # type: ignore[assignment]
    parse_command_tokens = _parse_command_tokens  # type: ignore[assignment]
    suspicious_chars = _suspicious_chars  # type: ignore[assignment]


# ==================== 兜底救援 ====================
def _rescue_table(core: Core) -> dict[str, Callable[..., Any]]:
    table: dict[str, Callable[..., Any]] = {}
    for spec in core.modules.values():
        table.update(spec.rescue or {})
    # 全局命令优先级最高：模块的 rescue 只用来补齐「只有它认识」的命令
    table.update(
        {
            "start": home_panel,
            "home": home_panel,
            "menu": home_panel,
            "help": help_panel,
            "id": id_panel,
            "jobs": jobs_panel,
            "cancel": cancel_panel,
            "status": status_command,
            "list": list_command,
        }
    )
    for alias, (module_id, panel) in ALIASES.items():
        table[alias] = alias_handler(module_id, panel)
    return table


async def _call_rescue(
    handler: Callable[..., Any],
    core: Core,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    args: list[str],
) -> None:
    """救援调用兼容两种 handler 签名：

    * MTBots 风格 `(core, update, context)`（路由自己的处理器与 ModuleSpec.rescue 的契约）
    * PTB 风格 `(update, context)`（各模块从旧 bot 原样搬过来的 handler）
    """
    import inspect

    wrapped = _ArgsOverride(context, args)
    try:
        params = [
            p
            for p in inspect.signature(handler).parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):  # 内建/不可反射的 callable
        params = []
    if len(params) >= 3:
        await handler(core, update, wrapped)
    else:
        await handler(update, wrapped)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """看起来像指令却没被任何 handler 认领：先尝试救回来，救不了再给出可操作反馈。"""
    core = core_of(context)
    message = update.effective_message
    if message is None or update.effective_chat is None:
        return
    if not await ensure_allowed(core, update):
        return
    text = message.text or ""

    table = _rescue_table(core)
    name, _ = parse_command_tokens(text)
    for candidate, candidate_args in parse_command_candidates(text):
        handler = table.get(candidate)
        if handler is None:
            # 动态命令（LitePan 的 /refresh_<slug>）在表里只登记了前缀 "refresh_"，
            # 处理器自己会从消息正文里取 slug，这里按最长前缀兜住。
            for key in sorted((k for k in table if k.endswith("_")), key=len, reverse=True):
                if candidate.startswith(key):
                    handler = table[key]
                    break
        if handler is not None:
            log.info("兜底识别出指令：/%s（原始首字符=%r，长度=%d）", candidate, text[:1], len(text))
            await _call_rescue(handler, core, update, context, candidate_args)
            return

    log.warning(
        "没能识别的指令：chat=%s user=%s 首字符=%r 长度=%d 命令名=%r",
        update.effective_chat.id,
        update.effective_user.id if update.effective_user else "-",
        text[:1],
        len(text),
        name,
    )
    reasons: list[str] = []
    if text[:1] in ("／", "＼"):
        reasons.append("斜杠打成了全角 <code>／</code>，请用英文 <code>/</code>。")
        rest = text[1:]
    else:
        rest = text
    suspicious = suspicious_chars(rest)
    if suspicious:
        reasons.append("检测到看不见的字符：" + "、".join(esc(x) for x in suspicious) + "，建议删掉重打一遍。")
    if text.strip()[:1] in ("`", "'", '"', "“", "”", "‘", "’"):
        reasons.append("命令被代码块或引号包住了，去掉外层符号再发一次。")
    await message.reply_text(
        "🤔 没识别出这个指令。\n"
        + "".join("%s\n" % line for line in reasons)
        + "常用：<code>/start</code>、<code>/status</code>、<code>/jobs</code>、<code>/help</code>",
        parse_mode=ParseMode.HTML,
    )


async def orphan_callback(core: Core, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """group=1 的孤儿回调兜底：模块被下线 / 菜单过期时，按钮不能永远转圈。"""
    query = update.callback_query
    if query is None:
        return
    prefix, _action = cb_args(query.data or "")
    if not await ensure_allowed(core, update):
        return
    log.info("孤儿回调：prefix=%r data=%r", prefix, (query.data or "")[:40])
    try:
        await query.answer("⚠️ 该功能当前不可用（模块可能已下线，或菜单已过期），请重新 /start。", show_alert=True)
    except Exception:  # 回调查询可能已过期
        pass


async def log_incoming(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """group=-1 的旁观者：只记录「这条更新到底收到没有」，绝不拦截后续处理。"""
    if not isinstance(update, Update):
        log.info("收到更新：%s", type(update).__name__)
        return
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    text = (getattr(message, "text", None) or "").strip()
    if text.startswith("/") and len(text) > 1:
        cmd = text.split(maxsplit=1)[0].split("@", 1)[0]
    elif update.callback_query is not None:
        cmd = "callback:%s" % (update.callback_query.data or "")[:32]
    else:
        cmd = "(%s)" % type(message).__name__ if message is not None else "-"
    log.info(
        "收到更新：update_id=%s 指令=%s chat=%s(%s) user=%s",
        update.update_id,
        cmd,
        chat.id if chat else "-",
        chat.type if chat else "-",
        user.id if user else "-",
    )


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram.error import BadRequest, NetworkError, TimedOut

    error = context.error
    if isinstance(error, (NetworkError, TimedOut)):
        log.warning("🌐 临时网络波动：%s", error)
        return
    if isinstance(error, BadRequest) and "not modified" in str(error).lower():
        return
    log.error("❌ 未捕获的系统异常：", exc_info=error)
    if not isinstance(update, Update):
        return
    # 回调里抛异常时必须应答一次，否则客户端按钮一直转圈，看起来就是「点了没反应」
    if update.callback_query is not None:
        try:
            await update.callback_query.answer("⚠️ 处理失败，原因见下方提示")
        except Exception:
            pass
    chat = update.effective_chat
    if chat is None:
        return
    try:
        await context.bot.send_message(
            chat.id,
            "😵 处理时出错了（<code>%s</code>）。\n发送 <code>/id</code> 可查看运行环境与存储状态。"
            % esc(type(error).__name__ if error else "Unknown"),
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:  # 连出错提示都发不出去就只能记日志
        log.error("出错提示发送失败：%s", exc)


# ==================== 注册 ====================
#: 已知的模块回调前缀（模块未启用时，旧面板按钮要给用户一个明确反馈，而不是一直转圈）
CALLBACK_PREFIXES = {"d": "docker", "p": "litepan", "c": "cline"}


def register(app: Application, core: Core) -> None:
    """注册路由层 handler（必须早于各模块注册，冲突命令才归路由解释）。"""
    app.add_handler(CommandHandler(["start", "home", "menu"], lambda u, c: home_panel(core, u, c)))
    app.add_handler(CommandHandler("help", lambda u, c: help_panel(core, u, c)))
    app.add_handler(CommandHandler("id", lambda u, c: id_panel(core, u, c)))
    app.add_handler(CommandHandler("jobs", lambda u, c: jobs_panel(core, u, c)))
    app.add_handler(CommandHandler("cancel", lambda u, c: cancel_panel(core, u, c)))
    app.add_handler(CommandHandler("status", lambda u, c: status_command(core, u, c)))
    app.add_handler(CommandHandler("list", lambda u, c: list_command(core, u, c)))

    for alias, (module_id, panel) in ALIASES.items():
        app.add_handler(CommandHandler(alias, alias_handler(module_id, panel)))

    app.add_handler(CallbackQueryHandler(lambda u, c: callback_router(core, u, c), pattern=r"^(nav|job)\|"))


def register_fallback(app: Application, core: Core) -> None:
    """兜底 handler：必须由 :mod:`mtbots.app` 在**所有模块注册完之后**调用。

    为什么不能用 group=1 当兜底：PTB 的 `Application.process_update` 会遍历**每一个 group**
    （`break` 只跳出当前 group 的 handler 列表），所以第二个 group 里的 handler 在第一个 group
    已经处理过的情况下照样会执行——那会让每条命令都被执行两次。
    正确做法是留在同一个 group，靠「同组内首个匹配者执行后 break」来保证只跑一次，
    再用注册顺序把它排到最后。
    """
    # 1) 长得像命令但没人认领的消息（全角斜杠 / 代码块粘贴 / 手误）
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r"^\s*[`'\"“”‘’]*\s*[/／]"), unknown_command)
    )

    # 2) 已下线模块留下的按钮：给弹窗，别让用户对着转圈等
    disabled = "|".join(
        re.escape(prefix) for prefix, module_id in sorted(CALLBACK_PREFIXES.items()) if not core.has(module_id)
    )
    if disabled:
        app.add_handler(
            CallbackQueryHandler(lambda u, c: orphan_callback(core, u, c), pattern=r"^(%s)\|" % disabled)
        )


__all__ = [
    "BASE_COMMANDS",
    "ALIASES",
    "CALLBACK_PREFIXES",
    "register",
    "register_fallback",
    "home_panel",
    "help_panel",
    "jobs_panel",
    "orphan_callback",
    "log_incoming",
    "global_error_handler",
]
