"""组装层：把 Core、router、三个 feature 模块拼成一个 python-telegram-bot 应用。"""

from __future__ import annotations

import logging
from typing import Optional

from telegram import Update
from telegram.ext import Application, TypeHandler

from . import __version__, features
from .bot import SafeBot
from .config import Settings
from .core import Core
from .jobs import JobCenter
from .menu import MenuManager
from .panels import PanelManager
from .router import BASE_COMMANDS, global_error_handler, log_incoming, register as register_router
from .router import register_fallback as register_router_fallback

log = logging.getLogger("mtbots.app")


def build_core(settings: Settings) -> Core:
    """两阶段构造：先拼 Core，再把 panels / menu 反向 attach 回 core。"""
    core = Core(
        settings=settings,
        acl=settings.build_acl(),
        panels=PanelManager(),
        jobs=JobCenter(),
        menu=MenuManager(None, BASE_COMMANDS),
    )
    core.panels.attach(core)
    core.menu.attach(core)
    return core


def load_modules(core: Core) -> Core:
    """按 `MTBOTS_MODULES` 装载启用的模块（禁用或导入失败的模块不影响其它模块）。"""
    for spec in features.load_modules(core):
        core.register(spec)
    log.info(
        "已启用模块：%s",
        "、".join("%s %s" % (s.icon, s.title) for s in core.modules.values()) or "（无）",
    )
    return core


def build_application(settings: Settings, core: Optional[Core] = None) -> Application:
    core = core or load_modules(build_core(settings))

    # 用自己的 Bot 子类：所有出站 HTML 都在这一层兜底（转义非法 `<` + 解析失败降级纯文本）。
    # 注意 builder 的 token/base_url 与 .bot() 互斥，所以这两项都交给 SafeBot 构造函数。
    api_base = settings.custom_api_base()
    bot = SafeBot(settings.bot_token, base_url=api_base) if api_base else SafeBot(settings.bot_token)
    builder = Application.builder().bot(bot)

    async def _post_init(application: Application) -> None:
        await post_init(application, core)

    application = builder.post_init(_post_init).build()
    application.bot_data["core"] = core

    # group=-1：只旁听记录「收到了什么」，block=False 保证不拦截后续处理
    application.add_handler(TypeHandler(Update, log_incoming, block=False), group=-1)

    # 路由层必须早于模块注册：四个冲突命令（/start /help /status /list）归路由解释
    register_router(application, core)

    for spec in core.modules.values():
        try:
            spec.register(application, core)
        except Exception:
            log.exception("模块 %s 注册失败，已跳过（其它模块继续）", spec.id)

    # 兜底必须最后注册：同组内先匹配者执行后就 break，模块自己的消息级 handler
    # （例如 LitePan 的 /refresh_<slug>）才能优先于「未知命令」兜底。
    register_router_fallback(application, core)

    application.add_error_handler(global_error_handler)
    return application


async def post_init(application: Application, core: Core) -> None:
    """启动钩子：下发合并菜单 → 各模块 startup（后台预热）。"""
    chats = sorted(set(core.settings.allowed_user_ids))
    for spec in core.modules.values():
        try:
            chats = sorted(set(chats) | set(spec.scope_chats(core) or []))
        except Exception as exc:
            log.warning("模块 %s 的 scope_chats 失败：%s", spec.id, exc)

    try:
        await core.menu.apply(application.bot, force=True, chats=chats)
    except Exception as exc:
        log.warning("下发命令菜单失败（不影响其它功能）：%s", exc)

    for spec in core.modules.values():
        if spec.startup is None:
            continue
        try:
            await spec.startup(core, application)
        except Exception:
            log.exception("模块 %s 启动钩子失败，已跳过", spec.id)

    log.info("MTBots v%s 就绪：%d 个模块，白名单 %d 人", __version__, len(core.modules), len(core.settings.allowed_user_ids))


def modules_summary(core: Core) -> list[str]:
    """给 `--check` 用：每个模块一行自检。"""
    lines: list[str] = []
    for spec in core.modules.values():
        detail: list[str] = []
        if spec.check is not None:
            try:
                detail = spec.check(core) or []
            except Exception as exc:
                detail = ["❌ 自检失败：%s" % exc]
        head = "%s %s（%s）" % (spec.icon, spec.title, spec.id)
        lines.append(head)
        lines.extend("   %s" % item for item in detail)
    return lines


__all__ = ["build_core", "load_modules", "build_application", "post_init", "modules_summary"]
