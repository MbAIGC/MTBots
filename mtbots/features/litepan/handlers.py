"""LitePan 模块的 PTB 异步 handler 层 + 回执轮询任务。

原 `tgbot.py` 的 `TelegramBot` 被拆成三块：

* **传输层删除**：getUpdates / offset / 线程池 / `tg_call` 全部去掉，改由 MTBots 的
  router 把 Update 送进来；
* **业务逻辑原样保留**：`/refresh`（含 `all` 全量规则优先）、`/refresh_<slug>`
  （按规则 ID 精确执行）、`/run`、`/info`、`/ping`、`/p_menu`、回执轮询与 `render_result`；
* **异步化**：所有阻塞调用（`LitePanClient.*`、`Discovery.fetch`）一律
  `await asyncio.to_thread(...)`，`threading.Thread(watch_run)` 改成
  `asyncio.create_task` + `core.jobs`，取消用 `asyncio.Event`。
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import warnings
from dataclasses import dataclass
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

from ...core import Core, core_of
from ...jobs import CANCELLED, DONE, FAILED
from ...panels import cb_parts, cb_simple
from ...text import esc, humanize_delta
from .client import TERMINAL_STATUSES, LitePanClient, LitePanError
from .config import LitePanConfig, UserProfile
from .discovery import Discovery

log = logging.getLogger("mtbots.litepan")

#: 菜单片段里的基础命令（要求 §5 指定的五个）
BASE_COMMANDS: list[tuple[str, str]] = [
    ("refresh", "触发所有规则"),
    ("info", "查看状态、规则与盘名"),
    ("p_menu", "刷新命令菜单"),
    ("run", "高级：触发任意事件"),
    ("strm", "触发所有规则（别名）"),
]

#: `/refresh_<slug>` 的动态命令（不能注册成 CommandHandler，见契约 §3）
REFRESH_SLUG_RE = re.compile(r"^/refresh_[A-Za-z0-9_]+", re.IGNORECASE)

#: 全角字符 / 零宽字符归一化（兜底救援用）
_ZERO_WIDTH = ("\u200b", "\u200c", "\u200d", "\ufeff")
_FULLWIDTH = {"／": "/", "＠": "@", "＿": "_", "－": "-"}

_DISCOVERY_UNBOUND_HINT = (
    "该会话未绑定 LitePan 实例：请在 {file} 中为该 chat_id 配置，"
    "或在 .env 中配置 LITEPAN_URL"
)


# ==================== 模块状态 ====================
class LitePanState:
    """挂在 `core.data["litepan"]["state"]` 上的模块私有状态。

    合并前这些字段散落在 `TelegramBot` 的实例属性里；多实例共用一个 asyncio 事件循环，
    所以进程内单例即可。`discovery_fetch_lock` / `receipt_lock` 仍是 `threading.Lock`：
    自动发现跑在 worker 线程里（`asyncio.to_thread`），必须用线程锁做单飞。
    """

    def __init__(self, config: LitePanConfig, *, discovery_ttl: float = 60.0):
        self.config = config
        self.discovery_ttl = discovery_ttl
        self.discovery_cache: dict[int, tuple[float, Optional[Discovery], bool]] = {}
        self.discovery_fetch_lock = threading.Lock()
        self.receipted_runs: set[tuple[Any, int]] = set()
        self.receipt_lock = threading.Lock()
        #: slug -> 使用次数（菜单预算排序用：常用在前、其余按规则 ID 稳定排序）
        self.usage: dict[str, int] = {}
        #: 后台任务集合（防止 asyncio 任务被 GC，同时便于 shutdown 取消）
        self.tasks: set[asyncio.Task] = set()
        self.seen_chats: set[int] = set()
        self.last_menu_refresh = 0.0
        self.job_queue: Any = None
        self._shutdown_hooked = False

    # ---------- 自动发现（同步，调用方负责 to_thread） ----------
    def cache_entry(self, chat_id: Any):
        """返回新鲜的缓存元组 `(ts, Discovery|None, failed)`；`None` = 没有缓存。

        注意「缓存了失败」也是缓存（元组非 None），这样 60s 内不会反复打 LitePan，
        与「从未发现」区分开。
        """
        try:
            key = int(chat_id)
        except (TypeError, ValueError):
            return None
        cached = self.discovery_cache.get(key)
        if cached is not None and time.time() - cached[0] < self.discovery_ttl:
            return cached
        return None

    def peek(self, chat_id: Any) -> Optional[Discovery]:
        entry = self.cache_entry(chat_id)
        return entry[1] if entry is not None else None

    def discovery(self, chat_id: Any, profile: UserProfile) -> Optional[Discovery]:
        """60s 缓存 + 单飞锁；失败与「未开启」用 `discovery_failed()` 区分。"""
        if not profile.receipt_enabled:
            return None
        key = int(chat_id)
        entry = self.cache_entry(key)
        if entry is not None:
            return entry[1]
        with self.discovery_fetch_lock:
            entry = self.cache_entry(key)
            if entry is not None:
                return entry[1]
            d = Discovery(profile)
            try:
                d.fetch()
                self.discovery_cache[key] = (time.time(), d, False)
                return d
            except LitePanError as exc:
                log.warning("自动发现失败 chat=%s: %s", chat_id, exc)
                self.discovery_cache[key] = (time.time(), None, True)
                return None

    def discovery_failed(self, chat_id: Any) -> bool:
        """自动发现已尝试但失败（区别于“未开启/无管理员账号”）。"""
        try:
            cached = self.discovery_cache.get(int(chat_id))
        except (TypeError, ValueError):
            return False
        return bool(cached and cached[2])

    # ---------- 任务跟踪 ----------
    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def shutdown(self) -> None:
        """bot 停止时取消所有后台任务（菜单定时刷新 + 回执轮询）。"""
        jq = self.job_queue
        if jq is not None:
            try:
                for job in jq.get_jobs_by_name("litepan_menu"):
                    job.schedule_removal()
            except Exception as exc:  # 定时任务清理失败不致命
                log.debug("清理 LitePan 定时任务失败：%s", exc)
        pending = [t for t in self.tasks if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.tasks.clear()
        self.receipted_runs.clear()


def get_state(core: Core) -> LitePanState:
    """取（或惰性创建）模块状态；`core.data` 是 Core 给模块的私有口袋。"""
    bucket = core.data.setdefault("litepan", {})
    state = bucket.get("state")
    if not isinstance(state, LitePanState):
        state = LitePanState(LitePanConfig(getattr(core, "settings", None)))
        bucket["state"] = state
    return state


def _peek_state(core: Core) -> Optional[LitePanState]:
    try:
        bucket = core.data.get("litepan") or {}
    except AttributeError:
        return None
    state = bucket.get("state") if isinstance(bucket, dict) else None
    return state if isinstance(state, LitePanState) else None


# ==================== 纯工具（可单测） ====================
def classify_refresh_arg(arg: str) -> tuple[str, str]:
    """`/refresh` 参数分类：('empty', '') / ('path', '/x') / ('name', '光鸭-A')。

    其余解析（手动 DRIVES 映射、账号规则匹配）需要网络数据，在 `do_refresh` 里做。
    """
    arg = (arg or "").strip()
    if not arg:
        return "empty", ""
    if arg.startswith("/"):
        return "path", arg
    return "name", arg


def paginate(items: list, page: Any, per_page: int) -> tuple[int, int, list]:
    """返回 `(当前页, 总页数, 当页条目)`；页码越界自动夹回有效范围。"""
    per_page = max(1, int(per_page or 1))
    total = max(1, (len(items) + per_page - 1) // per_page)
    try:
        page = int(page or 1)
    except (TypeError, ValueError):
        page = 1
    page = max(1, min(page, total))
    start = (page - 1) * per_page
    return page, total, list(items[start : start + per_page])


def menu_entries(
    base: list[tuple[str, str]],
    rules: list[tuple[str, str, int, Any]],
    budget: int,
) -> list[tuple[str, str]]:
    """拼菜单片段：基础命令 + 预算内的 `refresh_<slug>`（常用优先、ID 稳定排序）。

    `rules` 元素为 `(slug, name, usage, rule_id)`。
    """
    entries = list(base)
    seen = {name for name, _ in entries}
    budget = max(0, int(budget or 0))

    def sort_key(item: tuple[str, str, int, Any]):
        slug, name, usage, rid = item
        try:
            rid_key = int(rid)
        except (TypeError, ValueError):
            rid_key = 0
        return (-int(usage or 0), rid_key, slug)

    used = 0
    for slug, name, _usage, _rid in sorted(rules, key=sort_key):
        if used >= budget:
            break
        if not slug:
            continue
        command = "refresh_%s" % slug
        if command in seen:
            continue
        seen.add(command)
        entries.append((command, str(name or slug)))
        used += 1
    return entries


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ==================== 会话上下文 / 守卫 ====================
@dataclass
class _Ctx:
    core: Core
    state: LitePanState
    profile: UserProfile
    chat_id: int
    uid: Optional[int]
    update: Any
    context: Any
    bot: Any = None


def _bot_of(update: Any, context: Any) -> Any:
    bot = getattr(context, "bot", None)
    if bot is not None:
        return bot
    try:
        return update.get_bot()
    except Exception:
        return None


def _normalize_text(text: str) -> str:
    """归一化全角斜杠 / 零宽字符（兜底救援路径与正常命令路径共用）。"""
    if not text:
        return ""
    for zero in _ZERO_WIDTH:
        text = text.replace(zero, "")
    for src, dst in _FULLWIDTH.items():
        text = text.replace(src, dst)
    return text


def _message_text(update: Any) -> str:
    message = getattr(update, "effective_message", None)
    return _normalize_text(getattr(message, "text", "") or "")


def _arg_of(update: Any) -> str:
    parts = _message_text(update).split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _command_of(update: Any) -> str:
    text = _message_text(update).strip()
    if not text:
        return ""
    return text.split(None, 1)[0].lower().split("@")[0]


#: 从（可能被全角符号 / 零宽字符 / 反引号包裹的）正文里抠出 `/refresh_<slug>` 的 slug
_SLUG_IN_TEXT_RE = re.compile(r"/refresh_([A-Za-z0-9_]+)", re.IGNORECASE)


def _slug_of(update: Any) -> str:
    """救援路径用：不要求命令顶格，正文里出现 `/refresh_<slug>` 就能救回来。"""
    match = _SLUG_IN_TEXT_RE.search(_message_text(update))
    return match.group(1).lower() if match else ""


async def _deny(update: Any, context: Any, chat_id: Optional[int], text: str) -> None:
    """权限/绑定失败提示：回调走 alert，普通消息走面板发送。"""
    query = getattr(update, "callback_query", None)
    if query is not None:
        try:
            await query.answer(text, show_alert=True)
        except Exception:
            pass
        return
    core = _core_of_context(context)
    bot = _bot_of(update, context)
    if core is not None and bot is not None and chat_id is not None:
        try:
            await core.panels.send(int(chat_id), text, bot=bot)
        except Exception as exc:
            log.warning("发送提示失败 chat=%s: %s", chat_id, exc)


def _core_of_context(context: Any) -> Optional[Core]:
    try:
        return core_of(context)
    except Exception:
        application = getattr(context, "application", None)
        bot_data = getattr(application, "bot_data", None)
        return bot_data.get("core") if isinstance(bot_data, dict) else None


async def _guard(core: Core, update: Any, context: Any) -> Optional[_Ctx]:
    """权限（默认拒绝）+ chat -> profile 绑定检查；失败时已回复，返回 None。"""
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    uid = getattr(user, "id", None)
    chat_id = getattr(chat, "id", None)

    if not core.acl.can(uid, "litepan"):
        await _deny(update, context, chat_id, "⛔️ 你没有 LitePan 模块权限。")
        return None

    state = get_state(core)
    profile = state.config.profile_for(chat_id) if chat_id is not None else None
    # 非空 chat_ids 时必须是它自己：绝不渲染别的会话的配置（红线）
    if profile is None or (profile.chat_ids and int(chat_id) not in profile.chat_ids):
        await _deny(
            update,
            context,
            chat_id,
            _DISCOVERY_UNBOUND_HINT.format(file=state.config.users_file),
        )
        return None

    core.set_module(int(chat_id), "litepan")
    state.seen_chats.add(int(chat_id))
    return _Ctx(
        core=core,
        state=state,
        profile=profile,
        chat_id=int(chat_id),
        uid=uid,
        update=update,
        context=context,
        bot=_bot_of(update, context),
    )


async def say(ctx: _Ctx, text: str, keyboard: Optional[InlineKeyboardMarkup] = None) -> None:
    """原 `TelegramBot.say()`：分片发送，失败只告警。"""
    if ctx.bot is None:
        ctx.bot = _bot_of(ctx.update, ctx.context)
    if ctx.bot is None:
        log.warning("没有 bot，无法发送 chat=%s：%s", ctx.chat_id, text[:80])
        return
    try:
        await ctx.core.panels.send(ctx.chat_id, text, keyboard, bot=ctx.bot)
    except Exception as exc:
        log.warning("发送消息失败 chat=%s: %s", ctx.chat_id, exc)


async def get_discovery(state: LitePanState, chat_id: Any, profile: UserProfile) -> Optional[Discovery]:
    """取自动发现结果：缓存命中（含缓存的失败）直接返回，未命中丢进 worker 线程。"""
    if not profile.receipt_enabled:
        return None
    entry = state.cache_entry(chat_id)
    if entry is not None:
        return entry[1]
    return await asyncio.to_thread(state.discovery, chat_id, profile)


def _discovery_off_text(ctx: _Ctx) -> str:
    if not ctx.profile.receipt_enabled:
        return "自动发现未开启（需在 users.json / env 配置 admin_user / admin_password）。"
    if ctx.state.discovery_failed(ctx.chat_id):
        return "自动发现失败（LitePan 接口异常或管理员账号不可用），可稍后重试或发送 /p_menu 触发刷新。"
    return "自动发现未开启。"


def _page_size(core: Core) -> int:
    try:
        return max(1, min(12, int(getattr(core.settings, "page_size", 6) or 6)))
    except (TypeError, ValueError):
        return 6


# ==================== 触发 / 回执 ====================
async def trigger_and_report(ctx: _Ctx, event: str, source: str, path: str) -> None:
    """按事件触发（开放接口）：支持同名事件触发多条规则，并起回执轮询。"""
    profile = ctx.profile
    client = LitePanClient(profile)
    pre_base = 0
    if profile.receipt_enabled:
        try:
            pre_base = await asyncio.to_thread(client.max_run_id)
        except LitePanError as exc:
            log.warning("回执快照失败，退化为 0：%s", exc)
    try:
        data = await asyncio.to_thread(client.trigger, event, source, path)
    except LitePanError as exc:
        await say(ctx, "⚠️ 触发失败：%s" % esc(exc))
        return
    matched = int(data.get("matched") or 0)
    triggered = data.get("triggered") or []
    if matched == 0:
        await say(
            ctx,
            "未匹配到任何 Webhook 自动化规则。请在 LitePan「任务管理-自动联动」新建规则："
            "触发方式=Webhook，事件名=%s，再试一次。" % esc(event),
        )
        return
    names = "、".join("「%s」" % esc(t.get("name", "")) for t in triggered)
    await say(ctx, "✅ 已触发 %d 条规则：%s\n任务异步执行中。" % (matched, names))
    if profile.receipt_enabled and triggered:
        for t in triggered:
            _spawn_watch(ctx, t.get("id"), t.get("name") or "", pre_base, client)


async def run_rule_and_report(ctx: _Ctx, rule: dict) -> None:
    """精确执行单条规则（管理接口按规则 ID）；无管理员账号时退回事件触发。"""
    if not ctx.profile.receipt_enabled:
        await trigger_and_report(ctx, rule["event"], ctx.profile.source, ctx.profile.default_path)
        return
    client = LitePanClient(ctx.profile)
    pre_base = 0
    try:
        pre_base = await asyncio.to_thread(client.max_run_id)
    except LitePanError as exc:
        log.warning("回执快照失败，退化为 0：%s", exc)
    try:
        await asyncio.to_thread(client.run_rule, rule["id"])
    except LitePanError as exc:
        await say(
            ctx,
            "⚠️ 规则「%s」提交失败：%s\n可改用 /run %s 触发。"
            % (esc(rule["name"]), esc(exc), esc(rule["event"])),
        )
        return
    await say(ctx, "✅ 已提交执行规则：「%s」\n任务异步执行中。" % esc(rule["name"]))
    _spawn_watch(ctx, rule["id"], rule["name"], pre_base, client)


async def run_rules_and_report(ctx: _Ctx, rules: list[dict]) -> None:
    """精确执行账号下的多条单盘规则；无管理员账号时退回按事件触发。"""
    if not ctx.profile.receipt_enabled:
        events = sorted(set(r["event"] for r in rules))
        for ev in events:
            await trigger_and_report(ctx, ev, ctx.profile.source, ctx.profile.default_path)
        return
    pre_base = 0
    try:
        pre_base = await asyncio.to_thread(LitePanClient(ctx.profile).max_run_id)
    except LitePanError as exc:
        log.warning("回执快照失败，退化为 0：%s", exc)
    ok_names: list[str] = []
    failed: list[tuple[str, str]] = []
    for rule in rules:
        try:
            client = LitePanClient(ctx.profile)
            await asyncio.to_thread(client.run_rule, rule["id"])
            ok_names.append(rule["name"])
            _spawn_watch(ctx, rule["id"], rule["name"], pre_base, client)
        except LitePanError as exc:
            failed.append((rule["name"], str(exc)))
    if ok_names:
        await say(
            ctx,
            "✅ 已提交 %d 条规则执行：%s\n任务异步执行中。"
            % (len(ok_names), "、".join("「%s」" % esc(n) for n in ok_names)),
        )
    for name, err in failed:
        await say(ctx, "⚠️ 规则「%s」提交失败：%s" % (esc(name), esc(err)))


def _spawn_watch(ctx: _Ctx, rule_id: Any, rule_name: str, pre_base: int, client: LitePanClient) -> None:
    if rule_id is None:
        log.warning("触发结果缺少规则 ID，跳过 %s 的回执轮询", rule_name)
        return
    ctx.state.spawn(
        watch_run(
            ctx.core,
            ctx.state,
            ctx.bot,
            ctx.chat_id,
            ctx.profile,
            rule_id,
            rule_name,
            pre_base,
            client,
        )
    )


def _actions_for(rule_id: Any, status: str) -> InlineKeyboardMarkup:
    """回执卡片上的下一步按钮：失败去 Docker 升级，其余再跑一次。"""
    if status in ("failed", "error"):
        button = InlineKeyboardButton("⬆️ 升级 LitePan 容器", callback_data="nav|open|docker")
    else:
        button = InlineKeyboardButton(
            "🔄 再跑一次", callback_data=cb_simple("p", "run_rule", rule_id)
        )
    return InlineKeyboardMarkup([[button]])


async def watch_run(
    core: Core,
    state: LitePanState,
    bot: Any,
    chat_id: int,
    profile: UserProfile,
    rule_id: Any,
    rule_name: str,
    pre_base: int = 0,
    client: Optional[LitePanClient] = None,
) -> None:
    """异步版 `TelegramBot.watch_run`：等触发后新产生的运行并推回执（每个运行只回执一次）。

    `pre_base` 是触发前的全局最大运行 ID：运行创建后其 ID 必然大于快照，
    即使规则秒完成也能被识别并回执；同一规则短时间触发多次时各自独立回执。
    """
    client = client or LitePanClient(profile)
    stop = asyncio.Event()
    base_title = "LitePan 规则「%s」" % rule_name
    job = core.jobs.add(
        "litepan",
        "%s执行中" % base_title,
        cancel=stop.set,
        chat_id=chat_id,
    )

    def _finish(status: str, detail: str = "") -> None:
        # 完成后去掉「执行中」，/jobs 与回执卡片的标题才读得通
        job.title = base_title
        core.jobs.finish(job, status, detail)

    deadline = time.monotonic() + profile.receipt_timeout
    poll = max(1, int(profile.receipt_poll))
    try:
        while True:
            if stop.is_set():
                if job.running:
                    _finish(CANCELLED, "已取消等待回执")
                return
            if time.monotonic() >= deadline:
                detail = "⏱️ 规则「%s」仍在执行或排队，未在 %d 秒内收到完成回执。" % (
                    rule_name,
                    profile.receipt_timeout,
                )
                if job.running:
                    _finish(FAILED, detail)
                await _announce(core, bot, chat_id, job, _actions_for(rule_id, "timeout"))
                return
            try:
                runs = await asyncio.to_thread(client.list_runs, rule_id, 5)
            except LitePanError as exc:
                log.warning("轮询任务状态失败 rule=%s: %s", rule_id, exc)
                runs = []
            for r in runs:
                rid = _to_int(r.get("id"), 0)
                if rid <= int(pre_base or 0):
                    continue
                status = (r.get("status") or "").strip().lower()
                if status not in TERMINAL_STATUSES:
                    continue
                key = (rule_id, rid)
                with state.receipt_lock:
                    if key in state.receipted_runs:
                        continue
                    state.receipted_runs.add(key)
                detail = render_result(rule_name, r)
                if job.running:
                    _finish(DONE if status == "success" else FAILED, detail)
                await _announce(core, bot, chat_id, job, _actions_for(rule_id, status))
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        if job.running:
            _finish(CANCELLED, "Bot 停止，回执轮询已取消")
        raise


async def _announce(core: Core, bot: Any, chat_id: int, job: Any, actions: Any) -> None:
    if bot is None:
        log.warning("没有 bot，跳过回执推送 job=%s", job.id)
        return
    try:
        await core.jobs.announce(bot, chat_id, job, actions=actions)
    except Exception as exc:
        log.warning("回执推送失败 job=%s: %s", job.id, exc)


def render_result(rule_name: str, run: dict) -> str:
    """原样照搬 `render_result`：状态 + 运行 ID + 消息 + 步骤 ✓/✗。"""
    status = run.get("status") or "unknown"
    head = "✅" if status == "success" else "❌"
    lines = [
        "%s 规则「%s」执行完成" % (head, rule_name),
        "状态：%s" % status,
        "运行ID：%s" % run.get("id"),
    ]
    msg = (run.get("message") or "").strip()
    if msg:
        lines.append("消息：%s" % msg)
    result = run.get("result")
    steps = (result.get("steps") if isinstance(result, dict) else None) or []
    if steps:
        labels = []
        for s in steps:
            name = s.get("name") or s.get("type") or "?"
            mark = "✓" if s.get("status") == "success" else "✗"
            labels.append("%s%s" % (name, mark))
        lines.append("步骤：" + " / ".join(labels))
    return "\n".join(lines)


# ==================== /refresh 系列 ====================
async def default_events(ctx: _Ctx) -> list[str]:
    """无参 /refresh 在发现不可用时的兜底事件（原 `default_events`）。"""
    if ctx.profile.default_event:
        return [ctx.profile.default_event]
    d = await get_discovery(ctx.state, ctx.chat_id, ctx.profile)
    if d is not None:
        events = sorted(set(r["event"] for r in d.rules))
        if len(events) == 1:
            return events
    return []


async def do_refresh(ctx: _Ctx, arg: str) -> None:
    """`/refresh` 触发所有规则；存在全量规则 all 时只触发它；带盘名精确触发指定盘。"""
    kind, value = classify_refresh_arg(arg)
    profile = ctx.profile

    if kind == "empty":
        d = await get_discovery(ctx.state, ctx.chat_id, profile)
        if d is not None:
            if not d.rules:
                await say(ctx, "后台还没有 Webhook 自动化规则，请先在 LitePan「自动联动」里创建。")
                return
            # 全量规则约定：规则名为 all（不区分大小写，slug 为 all）时，
            # /refresh 无参数只触发它，避免和单盘/单任务规则重复执行。
            full = [
                r for r in d.rules if r.get("slug") == "all" or (r["name"] or "").strip().lower() == "all"
            ]
            if full:
                await say(
                    ctx,
                    "✅ 检测到全量规则「%s」，/refresh 本次只触发它（其他规则请用 /refresh_<规则> 单独触发）。"
                    % esc(full[0]["name"]),
                )
                await run_rules_and_report(ctx, full)
                return
            await run_rules_and_report(ctx, d.rules)
            return
        events = await default_events(ctx)
        if not events:
            await say(
                ctx,
                "未配置默认事件：可先 /info 查看规则，再用 /refresh <盘名>、/refresh_<规则> 或 /run <事件>。",
            )
            return
        for ev in events:
            await trigger_and_report(ctx, ev, profile.source, profile.default_path)
        return

    if kind == "path":
        await say(ctx, "路径参数已不再支持：/refresh 触发所有规则，/refresh <盘名> 触发指定盘。")
        return

    ev = profile.lookup_drive(value)
    if ev:
        await trigger_and_report(ctx, ev, profile.source, profile.default_path)
        return
    d = await get_discovery(ctx.state, ctx.chat_id, profile)
    rules = d.account_rules(value) if d is not None else []
    if not rules:
        hint = "可先 /info 查看 LitePan 里的盘名。"
        if not profile.receipt_enabled:
            hint = "未配置管理员账号，无法自动读取盘名；可配置 DRIVES 映射或在 users.json 填 admin 账号。" + hint
        await say(ctx, "未找到盘名「%s」。%s" % (esc(value), hint))
        return
    await run_rules_and_report(ctx, rules)


async def do_refresh_slug(ctx: _Ctx, slug: str) -> None:
    """处理 `/refresh_<slug>`：按规则 ID 精确执行，避免同名事件误触发。"""
    slug = (slug or "").strip().lower()
    d = await get_discovery(ctx.state, ctx.chat_id, ctx.profile)
    if d is None:
        await say(ctx, "未找到盘名命令「/refresh_%s」，可先 /info 查看。" % esc(slug))
        return
    rule = d.rule_by_slug.get(slug)
    if rule is not None:
        ctx.state.usage[slug] = ctx.state.usage.get(slug, 0) + 1
        await run_rule_and_report(ctx, rule)
        return
    account = d.slugs.get(slug)
    if account is not None:
        rules = d.account_rules(account)
        if rules:
            await run_rules_and_report(ctx, rules)
            return
        await say(ctx, "账号「%s」没有可触发的单盘规则。" % esc(account))
        return
    await say(ctx, "未找到规则命令「/refresh_%s」，可先 /info 查看。" % esc(slug))


async def do_run(ctx: _Ctx, arg: str) -> None:
    """`/run <事件> [path]`：高级触发接口。"""
    parts = (arg or "").split(None, 1)
    if not parts:
        await say(ctx, "用法：/run <事件名>，例如 /run quark01_refresh")
        return
    event = parts[0]
    path = parts[1] if len(parts) > 1 else ctx.profile.default_path
    await trigger_and_report(ctx, event, ctx.profile.source, path)


# ==================== 面板 ====================
async def build_info_text(ctx: _Ctx) -> str:
    """原 `info_text` 的异步版：连接状态 + 配置摘要 + 自动发现结果。"""
    profile = ctx.profile
    try:
        await asyncio.to_thread(LitePanClient(profile).health)
        status = "✅ LitePan 连接正常"
    except LitePanError as e:
        status = "⚠️ %s" % esc(e)
    d = await get_discovery(ctx.state, ctx.chat_id, profile)
    if d is not None:
        status_line = "自动发现已开启"
    elif not profile.receipt_enabled:
        status_line = "自动发现未开启（需管理员账号）"
    elif ctx.state.discovery_failed(ctx.chat_id):
        status_line = "自动发现失败（LitePan 接口异常，可稍后重试）"
    else:
        status_line = "自动发现未开启"

    lines = ["%s · %s" % (status, status_line), "", "📋 配置"]
    if profile.show_url:
        lines.append("· LitePan：%s" % esc(profile.lite_url))
    if not profile.receipt_enabled:
        lines.append("· 默认事件：%s（/refresh 兜底）" % esc(profile.default_event or "auto"))
    lines.append("· 回执模式：%s" % ("开启（管理员轮询）" if profile.receipt_enabled else "关闭"))
    if profile.drives:
        lines.append("· 手动映射：%s" % esc(profile.drive_list_text()))
    if d is None:
        if not profile.receipt_enabled:
            lines.append("")
            lines.append("💡 配置管理员账号后，可自动读取盘名和规则；未配置时 /refresh 使用默认事件触发。")
        elif ctx.state.discovery_failed(ctx.chat_id):
            lines.append("")
            lines.append("💡 自动发现失败：LitePan 接口异常或管理员账号不可用，可稍后重试或发送 /p_menu 触发刷新。")
        return "\n".join(lines)

    lines.append("")
    if d.rules:
        lines.append("📦 规则（%d 条）" % len(d.rules))
        for r in d.rules:
            task_part = "、".join(esc(t) for t in r["tasks"]) if r["tasks"] else "无任务"
            cmd = "/refresh_%s" % r.get("slug", "") if r.get("slug") else "/run %s" % r["event"]
            lines.append("· %s（事件 %s）" % (esc(r["name"]), esc(r["event"])))
            lines.append("  命令：%s" % esc(cmd))
            lines.append("  任务：%s" % task_part)
    else:
        lines.append("📦 规则（0 条）")
        lines.append("· 后台还没有 Webhook 自动化规则，请先在 LitePan「自动联动」里创建。")
    account_ids = sorted(d.by_account, key=lambda aid: d.accounts.get(aid, str(aid)))
    if account_ids:
        lines.append("")
        lines.append("💾 盘名")
        for aid in account_ids:
            name = d.accounts.get(aid, aid)
            lines.append("· %s：%s" % (esc(name), "、".join(sorted(esc(e) for e in d.by_account[aid]))))
    lines.append("")
    lines.append("💡 提示")
    lines.append("· /refresh 触发所有规则；/refresh <盘名>、/refresh_<规则> 精确执行；/run 为高级命令，同名事件会全部触发。")
    return "\n".join(lines)


def _info_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📜 规则列表", callback_data=cb_simple("p", "rule_page", 1)),
                InlineKeyboardButton("🔄 刷新菜单", callback_data=cb_simple("p", "menu")),
            ]
        ]
    )


async def do_info(ctx: _Ctx) -> None:
    """`/info` / `/p_status` / 面板首页：经典 info 文案 + 规则列表入口。"""
    text = await build_info_text(ctx)
    await ctx.core.panels.render("litepan", ctx.update, text, _info_keyboard())
    # 原 /info 的副作用：顺手刷新命令菜单（后台跑，不阻塞面板）
    if ctx.bot is not None:
        ctx.state.spawn(_safe_refresh_menu(ctx.core, ctx.bot, chat_id=ctx.chat_id))


async def do_list(ctx: _Ctx, page: Any = 1) -> None:
    """规则分页列表：每条规则一个 `[🎬 触发]` 按钮 + 翻页。"""
    d = await get_discovery(ctx.state, ctx.chat_id, ctx.profile)
    if d is None:
        await ctx.core.panels.render("litepan", ctx.update, _discovery_off_text(ctx), None)
        return
    rules = list(d.rules)
    page, total, items = paginate(rules, page, _page_size(ctx.core))
    lines = ["📜 <b>规则列表</b>（%d/%d，共 %d 条）" % (page, total, len(rules)), ""]
    if not rules:
        lines.append("后台还没有 Webhook 自动化规则，请先在 LitePan「自动联动」里创建。")
    for r in items:
        slug = r.get("slug") or ""
        event = esc(r.get("event") or "")
        tasks = "、".join(esc(t) for t in (r.get("tasks") or [])) or "无任务"
        cmd = "/refresh_%s" % slug if slug else "/run %s" % event
        lines.append("· <b>%s</b>（事件 %s）" % (esc(r.get("name") or "规则"), event))
        lines.append("  命令：<code>%s</code> · 任务：%s" % (esc(cmd), tasks))

    rows: list[list[InlineKeyboardButton]] = []
    for r in items:
        label = "🎬 %s" % _clip(r.get("name") or "规则", 28)
        rows.append([InlineKeyboardButton(label, callback_data=cb_simple("p", "run_rule", r.get("id")))])
    rows.append(
        [
            InlineKeyboardButton("◀️", callback_data=cb_simple("p", "rule_page", max(1, page - 1))),
            InlineKeyboardButton("%d/%d" % (page, total), callback_data=cb_simple("p", "rule_page", page)),
            InlineKeyboardButton("▶️", callback_data=cb_simple("p", "rule_page", min(total, page + 1))),
        ]
    )
    rows.append([InlineKeyboardButton("📋 状态 / 配置", callback_data=cb_simple("p", "info"))])
    await ctx.core.panels.render("litepan", ctx.update, "\n".join(lines), InlineKeyboardMarkup(rows))


# ==================== 菜单片段 ====================
def scope_chats(core: Core) -> list[int]:
    """已知会话：users.json 的 chat_ids + 实际交互过的 chat（env 单用户模式）。"""
    state = _peek_state(core)
    if state is None:
        return []
    chats: set[int] = set(int(c) for c in state.config.profiles.keys())
    chats.update(int(c) for c in state.seen_chats)
    return sorted(chats)


async def build_menu_entries(core: Core, state: LitePanState) -> list[tuple[str, str]]:
    """基础命令 + 预算内动态规则命令（跨 profile 合并，常用优先）。"""
    discovered: list[tuple[str, str, int, Any]] = []
    for profile in state.config.all_profiles():
        chat_id = profile.chat_ids[0] if profile.chat_ids else 0
        d = await get_discovery(state, chat_id, profile)
        if d is None:
            continue
        for slug, rule in d.rule_by_slug.items():
            discovered.append(
                (slug, rule.get("name") or slug, state.usage.get(slug, 0), rule.get("id") or 0)
            )
    return menu_entries(BASE_COMMANDS, discovered, state.config.menu_budget)


async def refresh_menu(core: Core, bot: Any, *, chat_id: Optional[int] = None, force: bool = False) -> bool:
    """把发现的规则提交给 `MenuManager` 并下发（不再自己调 setMyCommands）。

    内容未变化时 `MenuManager.apply()` 不会发请求（保留原去重）；
    失败只 warning、返回 False，**不影响 /refresh 触发**（原降级行为）。
    """
    state = get_state(core)
    try:
        entries = await build_menu_entries(core, state)
    except Exception as exc:
        log.warning("构建 LitePan 菜单片段失败：%s", exc)
        entries = list(BASE_COMMANDS)
    scope = scope_chats(core)
    if chat_id is not None and int(chat_id) not in scope:
        scope = sorted(set(scope) | {int(chat_id)})
    try:
        # scope 为空（env 单用户模式、还没交互）时先放全局，交互后再按会话收窄
        core.menu.set_module_commands("litepan", entries, scope_chats=(scope or None))
    except Exception as exc:
        log.warning("登记 LitePan 菜单片段失败：%s", exc)
        return False
    try:
        ok = await core.menu.apply(bot, force=force, chats=(scope or None))
    except Exception as exc:
        log.warning("LitePan 菜单下发失败：%s", exc)
        return False
    state.last_menu_refresh = time.time()
    if not ok:
        log.warning("LitePan 菜单更新失败（不影响触发，可稍后用 /p_menu 重试）")
    return ok


async def _safe_refresh_menu(core: Core, bot: Any, chat_id: Optional[int] = None, force: bool = False) -> bool:
    try:
        return await refresh_menu(core, bot, chat_id=chat_id, force=force)
    except Exception as exc:
        log.warning("LitePan 菜单刷新异常：%s", exc)
        return False


def _install_shutdown_hook(application: Any, state: LitePanState) -> None:
    """把「取消后台任务」挂到 Application.post_stop（不改 core，只包装现有回调）。"""
    if state._shutdown_hooked:
        return
    prev = getattr(application, "post_stop", None)

    async def _post_stop(app):
        await state.shutdown()
        if prev is not None:
            try:
                await prev(app)
            except Exception as exc:
                log.warning("调用原有 post_stop 失败：%s", exc)

    try:
        application.post_stop = _post_stop
        state._shutdown_hooked = True
    except Exception as exc:  # 某些 PTB 版本不允许赋值，退化为不清理
        log.debug("无法挂载 post_stop 清理钩子：%s", exc)


# ==================== ModuleSpec 回调 ====================
def commands(core: Core, uid: int) -> list[tuple[str, str]]:
    """LitePan 贡献的静态命令片段。"""
    return [
        ("refresh", "触发所有规则（/refresh <盘名> 指定盘）"),
        ("strm", "触发所有规则（别名）"),
        ("info", "LitePan 状态、规则与盘名"),
        ("ping", "LitePan 连接检测"),
        ("p_status", "状态面板（等同 /info）"),
        ("p_list", "规则列表（分页触发）"),
        ("p_menu", "刷新命令菜单"),
        ("run", "高级：触发任意 Webhook 事件"),
    ]


def help_text(core: Core, uid: int) -> str:
    return "\n".join(
        [
            "🎬 <b>LitePan 联动</b>",
            "/refresh 触发所有规则（存在全量规则 all 时只触发它）",
            "/refresh &lt;盘名&gt; 触发指定盘，如 /refresh 光鸭-A（盘名取自 LitePan 账号）",
            "/refresh_&lt;规则&gt; 菜单快捷命令，按规则 ID 精确触发",
            "/strm 同 /refresh",
            "/run &lt;事件&gt; [path] 高级：触发任意 Webhook 事件（同名事件会全部触发）",
            "/info、/ping、/p_status 查看连接状态、配置、规则与盘名",
            "/p_list 规则列表（分页，可点击触发）",
            "/p_menu 强制刷新命令菜单",
        ]
    )


async def summary(core: Core, uid: int) -> str:
    """首页一行总览：只读任务中心缓存，绝不发网络请求。"""
    try:
        running = core.jobs.running("litepan")
        recent = core.jobs.recent(1, module="litepan")
    except Exception:
        return "🎬 LitePan · 点击进入"
    if running:
        return "🎬 LitePan · 执行中 ⏳ %d 条" % len(running)
    if recent:
        job = recent[0]
        mark = {"done": "✅", "failed": "❌", "cancelled": "🛑"}.get(job.status, "•")
        delta = humanize_delta(time.time() - (job.finished_at or time.time()))
        return "🎬 LitePan · 最近任务 %s %s" % (mark, delta)
    return "🎬 LitePan · 点击进入"


async def id_lines(core: Core, uid: int) -> list[str]:
    """/id 的 LitePan 章节：配置路径、绑定会话、发现缓存（全部读内存）。"""
    lines = ["🎬 LitePan 联动"]
    state = _peek_state(core)
    if state is None:
        lines.append("· 模块尚未初始化")
        return lines
    cfg = state.config
    lines.append("· 用户配置：%s" % cfg.users_file)
    bound = sorted(cfg.profiles.keys())
    if bound:
        shown = "、".join(str(c) for c in bound[:5]) + ("…" if len(bound) > 5 else "")
        lines.append("· 已绑定会话：%d 个（%s）" % (len(bound), shown))
    if cfg.fallback is not None:
        lines.append("· 单用户 env 兜底：已启用")
    cached = state.discovery_cache
    if cached:
        rules = sum(len(d.rules) for _ts, d, _failed in cached.values() if d is not None)
        lines.append("· 发现缓存：%d 个会话、%d 条规则" % (len(cached), rules))
    if cfg.error:
        lines.append("· ⚠️ %s" % cfg.error)
    return lines


def check(core: Core) -> list[str]:
    """`python -m mtbots --check` 自检项：只读配置，绝不连网络。"""
    state = get_state(core)
    cfg = state.config
    if not cfg.enabled:
        return ["❌ %s" % cfg.error]
    lines: list[str] = []
    if cfg.profiles:
        lines.append("✅ LitePan 用户配置：%s（%d 个会话绑定）" % (cfg.users_file, len(cfg.profiles)))
    else:
        lines.append("✅ LitePan 单用户 env 兜底已启用")
    profiles = cfg.all_profiles()
    receipt = sum(1 for p in profiles if p.receipt_enabled)
    lines.append("· 回执/自动发现：%d/%d 个实例配置了管理员账号" % (receipt, len(profiles)))
    lines.append("· 动态规则菜单预算：%d 条（LITEPAN_MENU_BUDGET）" % cfg.menu_budget)
    if cfg.error:
        lines.append("· ⚠️ %s" % cfg.error)
    return lines


# ==================== PTB handler ====================
async def cmd_refresh(core: Core, update: Any, context: Any) -> None:
    ctx = await _guard(core, update, context)
    if ctx is None:
        return
    await do_refresh(ctx, _arg_of(update))


async def cmd_strm(core: Core, update: Any, context: Any) -> None:
    ctx = await _guard(core, update, context)
    if ctx is None:
        return
    await do_refresh(ctx, _arg_of(update))


async def cmd_refresh_slug(core: Core, update: Any, context: Any) -> None:
    """`/refresh_<slug>`：动态命令，按 slug 分发（也供兜底救援调用）。"""
    ctx = await _guard(core, update, context)
    if ctx is None:
        return
    token = _command_of(update)
    slug = _slug_of(update)
    if not slug and token.startswith("/refresh_"):
        slug = token[len("/refresh_") :]
    if not slug:
        await do_refresh(ctx, _arg_of(update))
        return
    await do_refresh_slug(ctx, slug)


async def cmd_run(core: Core, update: Any, context: Any) -> None:
    ctx = await _guard(core, update, context)
    if ctx is None:
        return
    await do_run(ctx, _arg_of(update))


async def cmd_info(core: Core, update: Any, context: Any) -> None:
    ctx = await _guard(core, update, context)
    if ctx is None:
        return
    await do_info(ctx)


async def cmd_p_menu(core: Core, update: Any, context: Any) -> None:
    ctx = await _guard(core, update, context)
    if ctx is None:
        return
    ok = await refresh_menu(core, ctx.bot, chat_id=ctx.chat_id, force=True)
    await say(ctx, "✅ 命令菜单已更新。" if ok else "⚠️ 菜单更新失败，请稍后再试。")


async def show_status(core: Core, update: Any, context: Any) -> None:
    """`/status` 在 LitePan 上下文里的解释 = /info 面板。"""
    await cmd_info(core, update, context)


async def show_list(core: Core, update: Any, context: Any, *, page: int = 1) -> None:
    """`/list` 在 LitePan 上下文里的解释 = 规则分页列表。"""
    ctx = await _guard(core, update, context)
    if ctx is None:
        return
    await do_list(ctx, page)


async def open_panel(core: Core, update: Any, context: Any, *, page: int = 1) -> None:
    """首页点进 LitePan = 状态面板。"""
    ctx = await _guard(core, update, context)
    if ctx is None:
        return
    await do_info(ctx)


async def on_callback(core: Core, update: Any, context: Any) -> None:
    """`p|...` 回调分发：run_rule / rule_page / info / menu。"""
    query = getattr(update, "callback_query", None)
    data = (getattr(query, "data", "") or "") if query is not None else ""
    parts = cb_parts(data)
    action = parts[1] if len(parts) > 1 else ""
    rest = parts[2] if len(parts) > 2 else ""
    ctx = await _guard(core, update, context)
    if ctx is None:
        return  # _guard 已经用 alert 明确拒绝，不再静默
    if query is not None:
        try:
            await query.answer()  # 关掉 loading 动画；过期/无权的情况由 _deny 处理
        except Exception:
            pass
    if action == "run_rule":
        await _cb_run_rule(ctx, rest)
    elif action == "rule_page":
        await do_list(ctx, _to_int(rest, 1))
    elif action == "info":
        await do_info(ctx)
    elif action == "menu":
        ok = await refresh_menu(core, ctx.bot, chat_id=ctx.chat_id, force=True)
        await say(ctx, "✅ 命令菜单已更新。" if ok else "⚠️ 菜单更新失败，请稍后再试。")
    else:
        await say(ctx, "⚠️ 菜单已过期，请重新发送 /p_list 打开规则列表。")


async def _cb_run_rule(ctx: _Ctx, rest: str) -> None:
    rule_id = _to_int(rest, -1)
    d = await get_discovery(ctx.state, ctx.chat_id, ctx.profile)
    rule = None
    if d is not None:
        rule = next((r for r in d.rules if _to_int(r.get("id"), -1) == rule_id), None)
    if rule is None:
        # 回调内存表被清理 / Bot 重启 / 规则已删：明确提示，不静默
        await ctx.core.panels.render(
            "litepan",
            ctx.update,
            "⚠️ 菜单已过期（规则不存在或自动发现失败），请重新 /p_list 打开规则列表。",
            None,
        )
        return
    slug = rule.get("slug") or ""
    if slug:
        ctx.state.usage[slug] = ctx.state.usage.get(slug, 0) + 1
    await run_rule_and_report(ctx, rule)


def _bind(handler):
    """把 `(core, update, context)` 适配成 PTB 的 `(update, context)`。"""

    async def _entry(update, context):
        await handler(core_of(context), update, context)

    return _entry


def register(application: Any, core: Core) -> None:
    """注册本模块的 PTB handler（router 先注册，冲突命令由 router 持有）。"""
    state = get_state(core)
    cfg = state.config
    if cfg.enabled:
        log.info(
            "LitePan 模块启用：%d 个会话绑定%s",
            len(cfg.profiles),
            "（env 单用户兜底）" if cfg.fallback is not None else "",
        )
    else:
        log.warning("LitePan 模块暂无可用配置：%s", cfg.error)

    # 动态 /refresh_<slug> 必须先于 /refresh 的命令 handler 匹配（正则 MessageHandler）
    application.add_handler(MessageHandler(filters.Regex(REFRESH_SLUG_RE), _bind(cmd_refresh_slug)))
    application.add_handler(CommandHandler("refresh", _bind(cmd_refresh)))
    application.add_handler(CommandHandler("strm", _bind(cmd_strm)))
    application.add_handler(CommandHandler("run", _bind(cmd_run)))
    application.add_handler(CommandHandler(["info", "ping"], _bind(cmd_info)))
    application.add_handler(CommandHandler("p_menu", _bind(cmd_p_menu)))
    # 注意：/p_status、/p_list 由 router 的永久别名统一注册（ALIASES -> show_status/show_list），
    # 模块**不能**再注册一次，否则命令会重复（装配级集成测试会红）。
    application.add_handler(CallbackQueryHandler(_bind(on_callback), pattern=r"^p\|"))


async def startup(core: Core, application: Any) -> None:
    """post_init 钩子：启动刷新菜单 + 30 分钟定时刷新 + 停止时清理后台任务。"""
    state = get_state(core)
    bot = getattr(application, "bot", None)
    if bot is not None:
        state.spawn(_safe_refresh_menu(core, bot))

    interval = max(1, int(state.config.menu_refresh_minutes)) * 60
    job_queue = None
    try:
        with warnings.catch_warnings():
            # 没装 PTB[job-queue] 时访问 job_queue 会 warn；这里本来就有 asyncio 兜底
            warnings.simplefilter("ignore")
            job_queue = application.job_queue
    except Exception as exc:
        log.debug("没有可用的 JobQueue（%s），改用 asyncio 定时任务", exc)
        job_queue = None

    if job_queue is not None and bot is not None:
        async def _periodic(_context):
            await _safe_refresh_menu(core, bot)

        try:
            job_queue.run_repeating(_periodic, interval=interval, first=interval, name="litepan_menu")
            state.job_queue = job_queue
            log.info("LitePan 菜单定时刷新已注册：每 %d 分钟", interval // 60)
        except Exception as exc:
            log.warning("注册菜单定时任务失败，改用 asyncio：%s", exc)
            job_queue = None

    if job_queue is None and bot is not None:
        async def _loop():
            while True:
                await asyncio.sleep(interval)
                await _safe_refresh_menu(core, bot)

        state.spawn(_loop())
        log.info("LitePan 菜单定时刷新使用 asyncio 任务：每 %d 分钟", interval // 60)

    _install_shutdown_hook(application, state)


#: 兜底救援表（全角斜杠 / 零宽字符由 router 归一化后按名字分发）
RESCUE: dict[str, Any] = {
    "refresh": cmd_refresh,
    "refresh_": cmd_refresh_slug,
    "strm": cmd_strm,
    "run": cmd_run,
    "info": cmd_info,
    "ping": cmd_info,
    "p_menu": cmd_p_menu,
    "p_status": cmd_info,
    "p_list": show_list,
}


__all__ = [
    "BASE_COMMANDS",
    "REFRESH_SLUG_RE",
    "RESCUE",
    "LitePanState",
    "build_info_text",
    "build_menu_entries",
    "check",
    "classify_refresh_arg",
    "commands",
    "get_state",
    "help_text",
    "id_lines",
    "menu_entries",
    "on_callback",
    "open_panel",
    "paginate",
    "refresh_menu",
    "register",
    "render_result",
    "scope_chats",
    "show_list",
    "show_status",
    "startup",
    "summary",
    "watch_run",
]
