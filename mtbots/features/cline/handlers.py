"""Cline 模块的 Telegram 交互层（从 ``ClinePass-TG-Bot/bot.py`` 移植）。

移植保留的交互细节（这些是原 Bot 好用的地方，不能丢）：

* ``/addkey``：**先撤回含明文 Key 的消息**，清洗不可见字符，校验别名/Key 形态，
  回执只给掩码 + sha256 指纹 + 形态提示，日志永远不写 Key（``_safe_args``）；
* ``/keys``：只显示掩码与指纹，并给出本地对账办法；
* ``/clear confirm``：两步确认文案保留；
* ``/quota`` / ``/c_status``：冷却、``正在查询 N 个账号…`` 占位（无论成败都收掉）、
  查询期间注册 ``core.jobs`` 任务、结果走统一面板。

相对原始实现改了四处（都是合并契约要求）：

1. 权限走 :meth:`mtbots.core.Core` 的 ``acl``（**默认拒绝**），不再有「白名单留空 = 所有人可用」；
2. 额度面板走 ``core.panels.render("cline", …)``（单会话单面板 + 原地编辑 + 自动分片），
   并新增 ``[🔄 刷新面板]``（回调 ``c|refresh``）；
3. 不注册 ``start/help/status/list/menu/home/cancel/jobs/id`` —— 这些归 MTBots 路由，
   本模块只通过 ``rescue`` 表参与全角斜杠兜底；
4. 不安装 excepthook、不设置命令菜单（由 MTBots 全局负责）。
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from mtbots.core import Core, core_of
from mtbots.features.cline.core import (
    ConfigError,
    ConfigStore,
    Cooldown,
    ClinePassClient,
    KeyLimitError,
    Settings,
    Snapshot,
    esc,
    key_fingerprint,
    key_shape_note,
    mask_key,
    normalize_api_key,
    render_panel,
    split_alias_and_key,
    split_message,
)
from mtbots.jobs import DONE, FAILED
from mtbots.panels import cb, cb_args

log = logging.getLogger("mtbots.cline")

MODULE_ID = "cline"


# ==================== 模块状态 ====================
@dataclass
class ClineState:
    """把设置、存储、客户端、节流器打包，避免到处用全局变量。"""

    settings: Settings
    store: ConfigStore
    client: ClinePassClient
    cooldown: Cooldown
    #: Key 的增删改锁（`/addkey` `/delkey` `/clear`）：保护本地存储，**不覆盖网络查询**
    locks: dict[int, asyncio.Lock] = field(default_factory=dict)
    #: 额度查询锁（面板刷新 / 首页自动刷新）：一次查询 = 每个 Key 3 个接口，必须串起来
    fetch_locks: dict[int, asyncio.Lock] = field(default_factory=dict)
    #: 每个用户最近一次成功查询的快照（首页 summary 只读它，绝不发网络请求）
    snapshots: dict[int, list[Snapshot]] = field(default_factory=dict)

    def lock_for(self, user_id: int) -> asyncio.Lock:
        """Key 存储的写锁（与网络查询无关，别混用）。"""
        return self.locks.setdefault(int(user_id), asyncio.Lock())

    def fetch_lock_for(self, user_id: int) -> asyncio.Lock:
        """额度查询锁：同一用户同一时间只允许一轮 `fetch_all`。"""
        return self.fetch_locks.setdefault(int(user_id), asyncio.Lock())


def state_of(core: Core) -> ClineState:
    """取（或惰性创建）本模块的运行期状态，挂在 ``core.data["cline"]["state"]``。"""
    bucket = core.data.setdefault(MODULE_ID, {})
    state = bucket.get("state")
    if state is None:
        settings = Settings.from_env(global_settings=core.settings)
        state = ClineState(
            settings=settings,
            store=ConfigStore(settings.config_file, settings.max_keys_per_user),
            client=ClinePassClient(settings),
            cooldown=Cooldown(settings.status_cooldown),
        )
        bucket["state"] = state
        log.info(
            "Cline 模块就绪：存储=%s API=%s 额度路径=%s 并行=%s 演示模式=%s",
            settings.config_file,
            settings.api_base,
            settings.usage_path,
            settings.max_parallel,
            settings.demo_mode,
        )
    return state


# ==================== 通用守卫 ====================
async def _guard(core: Core, update: Update, *, private: bool = True) -> bool:
    """统一鉴权：``core.acl``（默认拒绝）+ 私聊限制。返回 True 表示可以继续处理。"""
    user = update.effective_user
    message = update.effective_message
    query = update.callback_query
    if user is None:
        return False
    if message is None and query is None:  # 既没有消息也没有按钮，无从回复
        return False

    if not core.acl.can(user.id, MODULE_ID):
        role = core.acl.role(user.id)
        if role is None:
            text = (
                "⛔️ 你不在白名单中，无法使用本 Bot。\n"
                f"你的用户 ID：<code>{esc(user.id)}</code>\n"
                "（把 ID 填进 <code>ALLOWED_USER_IDS</code> 即可启用）"
            )
        else:
            text = "⛔️ 你没有 <b>Cline</b> 模块的权限（角色：<code>%s</code>）。" % esc(role)
        if query is not None:
            await query.answer("⛔️ 无权限", show_alert=True)
        elif message is not None:
            await message.reply_text(text, parse_mode=ParseMode.HTML)
        return False

    chat = update.effective_chat
    if private and (chat is None or chat.type != ChatType.PRIVATE):
        if query is not None:
            await query.answer("🔒 请在私聊中使用", show_alert=True)
        elif message is not None:
            await message.reply_text("🔒 该指令涉及你的 API Key，请在私聊中使用。")
        return False
    return True


def _safe_args(args: object) -> str:
    """日志里描述命令参数，但绝不把 API Key 写进去。"""
    tokens = [str(a) for a in (args or [])]  # type: ignore[union-attr]
    shown = [
        tok if len(tok) <= 16 and not tok.lower().startswith(("sk_", "sk-")) else f"〈{len(tok)}字符〉"
        for tok in tokens
    ]
    return f"共 {len(tokens)} 个 {shown}"


async def _load_keys(state: ClineState, user_id: int, message: Any) -> Optional[dict[str, str]]:
    """读取用户 Key；存储不可用时明确报错（而不是假装成功）。"""
    try:
        return await asyncio.to_thread(state.store.keys, user_id)
    except ConfigError as exc:
        log.error("读取配置失败：%s", exc)
        if message is not None:
            await message.reply_text(
                "❌ 配置存储不可用，操作已取消。\n"
                f"原因：<code>{esc(str(exc)[:300])}</code>\n"
                "请检查 CONFIG_FILE 路径与挂载权限。",
                parse_mode=ParseMode.HTML,
            )
        return None


# ==================== 面板 ====================
def _panel_keyboard() -> InlineKeyboardMarkup:
    """面板按钮；``🏠 返回`` 由 PanelManager 自动补齐。"""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 刷新面板", callback_data=cb("c", "refresh"))]]
    )


def _keys_text(user_keys: dict[str, str]) -> str:
    """掩码 + 指纹的 Key 列表（任何路径都不出现明文）。"""
    lines = ["📋 <b>已绑定的 Key</b>", ""]
    lines.extend(
        f"• <b>{esc(alias)}</b>：<code>{esc(mask_key(key, show_length=True))}</code>\n"
        f"  指纹 <code>{esc(key_fingerprint(key))}</code>"
        for alias, key in user_keys.items()
    )
    lines.append("")
    lines.append(f"共 {len(user_keys)} 个")
    lines.append("🔍 指纹对账：<code>printf '%s' '你的Key' | sha256sum</code> 的前 12 位应当一致")
    return "\n".join(lines)


async def _query_and_render(
    core: Core,
    context: ContextTypes.DEFAULT_TYPE,
    update: Update,
    state: ClineState,
    user_id: int,
    user_keys: dict[str, str],
) -> None:
    """查询全部账号并把结果渲染成唯一面板。

    耗时查询注册 ``core.jobs``（module=cline），这样 /jobs 能看到；
    交互式面板路径**不**额外推送完成通知（原文：do not announce）。
    """
    chat = update.effective_chat
    chat_id = chat.id if chat is not None else None
    if chat_id is None:
        return

    notice = None
    try:
        notice = await context.bot.send_message(chat_id, f"⏳ 正在查询 {len(user_keys)} 个账号…")
    except TelegramError as exc:  # 占位消息发不出去不影响结果
        log.warning("占位消息发送失败：%s", exc)

    job = core.jobs.add(MODULE_ID, f"查询 {len(user_keys)} 个账号额度", chat_id=chat_id)
    failed = False
    try:
        try:
            # 与首页自动刷新共用一把「查询锁」：一轮就是 3×N 个接口，不能两轮并发打
            # （注意不是 Key 存储那把锁——那个会挡住 /addkey）
            async with state.fetch_lock_for(user_id):
                snapshots = await state.client.fetch_all(list(user_keys.items()))
        except Exception as exc:  # ApiError 已在客户端内部转成 warnings，这里兜底
            failed = True
            core.jobs.finish(job, FAILED, str(exc)[:200])
            log.error("查询额度失败：user=%s，%s", user_id, exc)
            text = "❌ 查询失败，请稍后再试。"
            if notice is not None:
                try:
                    await notice.edit_text(text)
                except TelegramError:
                    pass
            else:
                await context.bot.send_message(chat_id, text)
            return

        core.jobs.finish(job, DONE, f"共 {len(snapshots)} 个账号")
        state.snapshots[int(user_id)] = snapshots
        text = render_panel(snapshots, show_identity=state.settings.show_identity)
        keyboard = _panel_keyboard()

        # 面板统一交给 PanelManager（单会话单面板 + 原地编辑 + disable_web_page_preview）。
        # 面板本身就超长时，前面的分片先发出去（保留原文行为），最后一片作为可刷新面板。
        chunks = split_message(text, state.settings.message_limit)
        if len(chunks) > 1:
            try:
                bot = update.get_bot()
            except RuntimeError:  # 测试/离线环境拿不到 bot：交给 PanelManager 自己分片
                bot = None
            if bot is not None:
                for chunk in chunks[:-1]:
                    await core.panels.send(chat_id, chunk, bot=bot)
                text = chunks[-1]
        await core.panels.render(MODULE_ID, update, text, keyboard, chat_id=chat_id)
    finally:
        if notice is not None and not failed:
            try:
                await notice.delete()  # "正在查询" 永远不残留
            except TelegramError:
                pass


async def show_status(core: Core, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """额度面板（``/c_status``、``/quota``；路由的 ``/status`` 在 cline 上下文时也走这里）。"""
    if not await _guard(core, update):
        return
    state = state_of(core)
    message = update.effective_message
    user = update.effective_user
    assert user is not None  # _guard 已保证

    wait = state.cooldown.hit(user.id)
    if wait > 0:
        await message.reply_text(f"⏳ 操作太快了，请 {wait:.0f} 秒后再试。")  # type: ignore[union-attr]
        return

    user_keys = await _load_keys(state, user.id, message)
    if user_keys is None:
        return
    if not user_keys:
        await message.reply_text(  # type: ignore[union-attr]
            "⚠️ 你还没有绑定任何 Key。\n用法：<code>/addkey 主账号 sk_xxxx</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await _query_and_render(core, context, update, state, user.id, user_keys)


async def open_panel(
    core: Core, update: Update, context: ContextTypes.DEFAULT_TYPE, *, page: int = 1
) -> None:
    """首页按钮打开本模块：等价于额度面板（``page`` 仅为兼容契约）。"""
    await show_status(core, update, context)


async def show_list(core: Core, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/c_list``：列出已绑定别名（掩码 + 指纹，不查 API、不出现明文）。"""
    if not await _guard(core, update):
        return
    state = state_of(core)
    message = update.effective_message
    user = update.effective_user
    assert user is not None
    user_keys = await _load_keys(state, user.id, message)
    if user_keys is None:
        return
    if not user_keys:
        await message.reply_text(  # type: ignore[union-attr]
            "⚠️ 你还没有绑定任何 Key。\n用法：<code>/addkey 主账号 sk_xxxx</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await core.panels.render(MODULE_ID, update, _keys_text(user_keys))


# ==================== PTB 回调适配 ====================
def _ptb(panel: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """把「core 优先」的面板函数适配成 PTB 的 ``(update, context)`` 回调。

    ModuleSpec 约定面板函数签名是 ``(core, update, context)``（路由直接调用），
    而 PTB 的 CommandHandler 只会传 ``(update, context)``，所以要这一层。
    """

    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await panel(core_of(context), update, context)

    return _handler


# ==================== 指令处理（PTB 签名） ====================
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/c_status``、``/quota``。"""
    await show_status(core_of(context), update, context)


async def cmd_keys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/keys``：列出已绑定的别名（掩码 + 指纹）。"""
    await show_list(core_of(context), update, context)


async def cmd_addkey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/addkey <别名> <API_KEY>``：只允许私聊，先撤回含明文 Key 的消息。"""
    core = core_of(context)
    if not await _guard(core, update):
        return
    state = state_of(core)
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    assert user is not None and chat is not None
    user_id, chat_id = user.id, chat.id

    async def say(text: str) -> None:
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    # 先撤回这条含明文 Key 的消息：无论参数是否合法，都不把 Key 留在聊天记录里。
    # 原消息删除后仍可正常 send_message，所以后续统一用它回复。
    deleted = False
    if message is not None:
        try:
            await message.delete()
            deleted = True
        except TelegramError as exc:
            log.warning("撤回含 Key 的消息失败：%s", exc)

    args = context.args or []
    log.info("收到 /addkey：user=%s，%s", user_id, _safe_args(args))
    alias, api_key = split_alias_and_key(args)
    api_key, key_cleaned = normalize_api_key(api_key)
    if not api_key:
        log.warning("addkey 参数不足：user=%s，%s", user_id, _safe_args(args))
        await say(
            "⚠️ 参数不完整。\n"
            "格式：<code>/addkey &lt;别名&gt; &lt;API_KEY&gt;</code>\n"
            "示例：<code>/addkey 主账号 sk_1234567890</code>\n"
            "　　　<code>/addkey Cline-01 sk_1234567890</code>"
        )
        return

    if alias is None:
        log.warning("addkey 别名不合法：user=%s，%s", user_id, _safe_args(args))
        await say(
            "⚠️ 别名不合法：1–24 个字符，以中英文、数字或下划线开头，"
            "之后可含空格、点、连字符"
            "（<code>#</code>、<code>/</code>、<code>:</code>、emoji 不行）。\n"
            "示例：<code>/addkey 主账号 sk_1234567890</code>、<code>/addkey Cline-01 sk_1234567890</code>"
        )
        return
    if len(api_key) < 8 or any(ch.isspace() for ch in api_key):
        log.warning(
            "addkey 的 Key 看起来不合法：user=%s，长度=%d，清理过=%s", user_id, len(api_key), key_cleaned
        )
        await say(
            "⚠️ API Key 看起来不合法：长度需 ≥ 8，且不含空格或换行。\n"
            "（从网页复制时容易带上不可见字符，Bot 已经自动清理过一次）"
        )
        return

    try:
        async with state.lock_for(user_id):
            total = await asyncio.to_thread(state.store.add, user_id, alias, api_key)
        # Key 变了，旧快照立刻作废：否则首页会拿上一把 Key 的额度顶在新 Key 头上
        state.snapshots.pop(int(user_id), None)
    except KeyLimitError as exc:
        log.warning("addkey 超出上限：user=%s，%s", user_id, exc)
        await say(f"⚠️ {esc(str(exc))}")
        return
    except ConfigError as exc:
        log.error("写入配置失败：user=%s，%s", user_id, exc)
        await say("❌ 保存失败，Key <b>没有</b>被记录。\n" f"原因：<code>{esc(str(exc)[:300])}</code>")
        return

    log.info("已保存 Key：user=%s，别名=%r，长度=%d，该用户现有 %s 个", user_id, alias, len(api_key), total)
    note = "（含 Key 的消息已撤回）" if deleted else "（⚠️ 未能撤回原消息，建议自行删除）"
    if key_cleaned:
        note += "\n🧹 已自动去掉 Key 里夹带的不可见字符"
    # 复制不全是最常见的 401 原因，绑定当场就提醒，别等 /status
    shape = key_shape_note(api_key)
    if shape:
        note += "\n" + esc(shape)
    await say(
        f"✅ 已保存 Key\n📌 别名：<code>{esc(alias)}</code>\n"
        f"🔐 Key：<code>{esc(mask_key(api_key, show_length=True))}</code>\n"
        f"🔍 指纹 <code>{esc(key_fingerprint(api_key))}</code>\n{note}"
    )


async def cmd_delkey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/delkey <别名>``。"""
    core = core_of(context)
    if not await _guard(core, update):
        return
    state = state_of(core)
    message = update.effective_message
    user = update.effective_user
    assert user is not None
    if not context.args:
        await message.reply_text(  # type: ignore[union-attr]
            "⚠️ 请指定别名。\n格式：<code>/delkey &lt;别名&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    alias = context.args[0].strip()
    try:
        async with state.lock_for(user.id):
            removed = await asyncio.to_thread(state.store.delete, user.id, alias)
        state.snapshots.pop(int(user.id), None)
    except ConfigError as exc:
        log.error("删除失败：%s", exc)
        await message.reply_text(  # type: ignore[union-attr]
            f"❌ 删除失败：<code>{esc(str(exc)[:200])}</code>", parse_mode=ParseMode.HTML
        )
        return
    log.info("删除别名：user=%s，别名=%r，结果=%s", user.id, alias, removed)
    text = f"🗑️ 已删除别名 <b>{esc(alias)}</b>。" if removed else f"❌ 未找到别名 <b>{esc(alias)}</b>。"
    await message.reply_text(text, parse_mode=ParseMode.HTML)  # type: ignore[union-attr]


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/clear confirm``：清空当前用户绑定的全部 Key。"""
    core = core_of(context)
    if not await _guard(core, update):
        return
    state = state_of(core)
    message = update.effective_message
    user = update.effective_user
    assert user is not None
    if [a.lower() for a in (context.args or [])] != ["confirm"]:
        await message.reply_text(  # type: ignore[union-attr]
            "⚠️ 这会删除你绑定的<b>全部</b> Key，确认请输入：<code>/clear confirm</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        async with state.lock_for(user.id):
            removed = await asyncio.to_thread(state.store.clear, user.id)
        state.snapshots.pop(int(user.id), None)
    except ConfigError as exc:
        log.error("清空失败：%s", exc)
        await message.reply_text(  # type: ignore[union-attr]
            f"❌ 操作失败：<code>{esc(str(exc)[:200])}</code>", parse_mode=ParseMode.HTML
        )
        return
    log.info("清空 Key：user=%s，删除 %s 个", user.id, removed)
    await message.reply_text(f"🧹 已清空 {removed} 个 Key。", parse_mode=ParseMode.HTML)  # type: ignore[union-attr]


# ==================== 回调 ====================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``c|…`` 命名空间的回调（目前只有 ``c|refresh``）。"""
    core = core_of(context)
    query = update.callback_query
    if query is None:
        return
    prefix, action = cb_args(query.data or "")
    if prefix != "c":
        return

    if action == "refresh":
        if not await _guard(core, update):
            return
        state = state_of(core)
        user = update.effective_user
        assert user is not None
        wait = state.cooldown.hit(user.id)
        if wait > 0:
            await query.answer(f"⏳ 请 {wait:.0f} 秒后再试", show_alert=True)
            return
        await query.answer("🔄 正在刷新…")
        user_keys = await _load_keys(state, user.id, query.message or update.effective_message)
        if user_keys is None:
            return
        if not user_keys:
            await query.edit_message_text(
                "⚠️ 你还没有绑定任何 Key。\n用法：<code>/addkey 主账号 sk_xxxx</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        await _query_and_render(core, context, update, state, user.id, user_keys)
        return

    # 其它动作：过期/未知回调也不能让按钮一直转圈
    await query.answer("⚠️ 按钮已过期，请重新打开面板", show_alert=False)


# ==================== 兜底救援（全角斜杠 / 零宽字符） ====================
#: 说明：路由 ``unknown_command`` 已经用 ``_ArgsOverride`` 包装了 context，
#: 所以这里的 handler 都是 PTB 的 ``(update, context)`` 签名。
#: ``start/help/id/status`` 这几格只是「救援表占位」，正常路径由 router 先接管。
async def rescue_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from mtbots.router import home_panel

    await home_panel(core_of(context), update, context)


async def rescue_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from mtbots.router import help_panel

    await help_panel(core_of(context), update, context)


async def rescue_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from mtbots.router import id_panel

    await id_panel(core_of(context), update, context)


async def rescue_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """全角 ``／status``：交给路由按当前模块消歧（在 cline 上下文里就会回到本模块面板）。"""
    from mtbots.router import status_command

    await status_command(core_of(context), update, context)


#: 兜底救援表：命令名 -> PTB handler（交给 ModuleSpec.rescue，自己不注册 MessageHandler）
RESCUE: dict[str, Callable[..., Awaitable[None]]] = {
    "start": rescue_start,
    "help": rescue_help,
    "id": rescue_id,
    "status": rescue_status,
    "quota": cmd_status,
    "addkey": cmd_addkey,
    "delkey": cmd_delkey,
    "keys": cmd_keys,
    "clear": cmd_clear,
}


# ==================== ModuleSpec 贡献 ====================
def commands(core: Core, uid: int) -> list[tuple[str, str]]:
    """贡献给全局命令菜单的片段（不含斜杠）。"""
    return [
        ("c_status", "Cline 额度面板"),
        ("quota", "同 /c_status"),
        ("addkey", "添加或更新 Key（私聊）"),
        ("delkey", "删除 Key（私聊）"),
        ("keys", "列出已绑定的别名（私聊）"),
        ("clear", "清空全部 Key（私聊）"),
    ]


def help_text(core: Core, uid: int) -> str:
    """帮助章节（HTML，由 router 拼到 /help 里）。"""
    return (
        "🤖 <b>Cline 额度</b>\n"
        "· <code>/c_status</code> 或 <code>/quota</code> — 额度面板（所有已绑定 Key）\n"
        "· <code>/addkey &lt;别名&gt; &lt;API_KEY&gt;</code> — 添加或更新指定别名的 Key\n"
        "· <code>/delkey &lt;别名&gt;</code> — 删除指定的 Key\n"
        "· <code>/keys</code> 或 <code>/c_list</code> — 列出已绑定别名（只显示掩码与指纹）\n"
        "· <code>/clear confirm</code> — 清空你绑定的全部 Key\n\n"
        "🔒 涉及 Key 的指令仅在私聊生效；包含 Key 的消息会被自动撤回。\n"
        "🧪 <code>DEMO_MODE=1</code> 时额度为示例数据（面板里会标注）。"
    )


#: 首页摘要里的窗口短名（面板正文里仍是「5 小时额度 / 本周额度 / 本月额度」全称）
SHORT_WINDOWS: tuple[tuple[str, str], ...] = (("小时", "5时"), ("周", "周"), ("月", "月"))


def _short_window(label: str) -> str:
    """把窗口名压成两三个字：首页一行要塞下 12 个 Key。"""
    text = str(label or "").strip()
    for needle, short in SHORT_WINDOWS:
        if needle in text:
            return short
    return (text.replace("额度", "") or text)[:4]


def _quota_brief(snapshot: Snapshot) -> str:
    """一个 Key 的额度摘要：`5时 15% / 周 30% / 月 20%`（已用百分比，与面板一致）。"""
    parts = [
        "%s %d%%" % (_short_window(window.label), round(window.percent))
        for window in snapshot.windows
        if window.percent is not None
    ]
    return " / ".join(parts)


async def summary(core: Core, uid: int) -> str:
    """首页总览：Key 数 + 每个 Key 的额度一行。

    只读本地存储与上次查询的快照缓存，**绝不发网络请求**（首页必须秒开）；
    快照由 ``refresh()``（点 /start 或 🔄 刷新）在后台拉取。

    **计数与明细必须同源**：`/delkey` 之后 store 少了 Key 而快照还在，若直接并列就会
    出现「2 个 Key」下面列 3 行。所以这里按**当前还在的别名**过滤快照，凑不齐整份
    就退回一行「点击进入」（下一轮刷新会补齐）。
    """
    state = state_of(core)
    try:
        keys = await asyncio.to_thread(state.store.keys, uid)
    except ConfigError:
        return "🤖 Cline · ❌ 存储不可用 · 点击进入"
    count = len(keys)
    if not count:
        return "🤖 Cline · 未绑定 Key · 点击进入"

    snapshots = [s for s in (state.snapshots.get(int(uid)) or []) if s.alias in keys]
    if len(snapshots) != count:
        return f"🤖 Cline · {count} 个 Key · 点击进入"

    briefs = [(snap.alias, _quota_brief(snap)) for snap in snapshots]
    failed = sum(1 for _, brief in briefs if not brief)
    # 只有一个 Key：压成一行（用户不用在两行之间来回看）
    if count == 1:
        alias, brief = briefs[0]
        return f"🤖 Cline · 1 个 Key · {esc(alias)} {brief or '⚠️ 无额度数据'}"

    head = f"🤖 Cline · {count} 个 Key"
    if failed:
        head += f"（正常 {count - failed} · 失败 {failed}）"
    lines = [head]
    for alias, brief in briefs:
        lines.append(f"• {esc(alias[:12])} · {brief or '⚠️ 无额度数据'}")
    return "\n".join(lines)


async def refresh(core: Core, uid: int, force: bool = False) -> None:
    """首页自动刷新：重新查一遍所有 Key 的额度并缓存。

    一个 Key 要打 3 个接口，所以这里**必须**收着点：

    * 用 `state.fetch_lock_for()`（额度查询锁，**不是** Key 存储那把锁——拿错锁会挡住
      `/addkey`）串起同一用户的查询；发现已经在查就直接放弃，不排队、不叠加；
    * 频率由 router 的 `ModuleSpec.refresh_ttl` 控制（`force` 也由它判断，这里不再重复）。
    """
    state = state_of(core)
    try:
        keys = await asyncio.to_thread(state.store.keys, uid)
    except ConfigError as exc:
        log.error("首页刷新读取 Cline Key 失败：%s", exc)
        return
    if not keys:
        state.snapshots.pop(int(uid), None)
        return

    lock = state.fetch_lock_for(uid)
    if lock.locked():  # 同一用户已有额度查询在跑（多半是面板上的手动刷新）
        return
    async with lock:
        state.snapshots[int(uid)] = await state.client.fetch_all(list(keys.items()))


async def id_lines(core: Core, uid: int) -> list[str]:
    """贡献给全局 ``/id``：容器、存储路径、自检结果、已绑定数量（永不列 Key）。"""
    state = state_of(core)
    lines = [
        "🤖 Cline 模块",
        f"🏠 容器：<code>{esc(socket.gethostname())}</code>",
        f"🗂 Key 存储：<code>{esc(state.settings.config_file)}</code>",
    ]
    ok, detail = await asyncio.to_thread(state.store.self_check)
    lines.append(f"💾 存储：{'✅ 可读写' if ok else '❌ 不可写'}（{esc(detail[:200])}）")
    try:
        keys = await asyncio.to_thread(state.store.keys, uid)
    except ConfigError as exc:
        lines.append(f"🔑 已绑定：读取失败 <code>{esc(str(exc)[:160])}</code>")
    else:
        lines.append(f"🔑 已绑定：{len(keys)} 个")
    if state.settings.demo_mode:
        lines.append("🧪 DEMO_MODE 已开启（额度为示例数据）")
    return lines


# ==================== 注册 ====================
def register(app: Application, core: Core) -> None:
    """只注册本模块自己的命令与回调。

    **绝不注册** ``start/help/status/list/menu/home/cancel/jobs/id``：
    它们归 MTBots 路由（``/status``、``/list`` 会在当前模块是 cline 时代理到这里）。

    ``/c_status`` 与 ``/c_list`` 由 router 的永久别名表（``router.ALIASES``）统一注册，
    并通过 ``spec.show_status`` / ``spec.show_list`` 调用本模块——模块**不能**再注册一次，
    否则命令重复（装配级集成测试会红），与 litepan 的处理保持一致。
    """
    # ModuleSpec 的面板函数是 (core, update, context)，PTB 只会传 (update, context)，
    # 所以经 _ptb() 适配——这一层只做参数搬运，不含业务逻辑。
    app.add_handler(CommandHandler("quota", _ptb(show_status)))
    app.add_handler(CommandHandler("addkey", cmd_addkey))
    app.add_handler(CommandHandler("delkey", cmd_delkey))
    app.add_handler(CommandHandler("keys", cmd_keys))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^c\|"))


__all__ = [
    "MODULE_ID",
    "ClineState",
    "state_of",
    "show_status",
    "show_list",
    "open_panel",
    "on_callback",
    "cmd_status",
    "cmd_addkey",
    "cmd_delkey",
    "cmd_keys",
    "cmd_clear",
    "commands",
    "help_text",
    "summary",
    "id_lines",
    "register",
    "RESCUE",
]
