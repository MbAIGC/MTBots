"""Docker 模块的 handler 层：命令 → 面板 → 长任务（core.jobs）。

对照 LDMG 的 `bot.py`：逻辑照搬，只换壳——
  · 鉴权从 `check_permission` 换成 `core.acl.can(user_id, "docker")`（默认拒绝）；
  · 回调整理成 `d|` 命名空间（`panels.cb` / `cb_simple`），主面板/详情/确认全部走
    `core.panels`（一条会话一个面板 + 面包屑 + 🏠 返回 + 两步确认）；
  · 升级 / 清理注册进 `core.jobs`，跑完 `announce()` 推一张带跨模块下一步的卡片。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from ...core import Core, core_of
from ...jobs import CANCELLED, DONE, FAILED, Job, card_text
from ...panels import (
    cb,
    cb_args,
    cb_parse,
    cb_parts,
    cb_simple,
    nav_jobs,
    next_actions_keyboard,
)
from ...text import esc
from .compose import (
    DockerState,
    dump_container_status,
    format_prune_snapshot,
    paginate_projects,
    run_command_with_feedback,
    scan_hint,
    scan_prune_candidates,
    sort_projects_for_display,
)
from .config import DockerSettings

log = logging.getLogger("mtbots.docker")

#: 需要 `panels.cb()` 内存载荷才能还原的回调（载荷被清理 = 菜单已过期）
_PAYLOAD_ACTIONS = {"p_sel", "up_s_ask", "up_svc_ask", "up_p_do", "up_svc_do"}

_UPGRADE_GUIDE = (
    "💡 <b>/upgrade 命令行升级指南：</b>\n\n"
    "• <code>/upgrade 01</code> : 升级列表中第 01 个项目\n"
    "• <code>/upgrade 01 emby</code> : 仅升级第 01 个项目中的 emby 容器\n"
    "• <code>/upgrade all</code> : 升级所有检测到的项目"
)

#: 同时只允许一个 compose 任务的提示（LDMG 的「已有任务在运行」）
_BUSY_TEXT = "⚠️ 已有任务在跑（同时只允许一个 compose 任务），请在 🧰 任务中心等它结束或取消。"


# ==================== 基础工具 ====================
def _state(core: Core) -> DockerState:
    """取本模块的 DockerState（register() 已放进 core.data["docker"]）。"""
    state = core.data.get("docker")
    if not isinstance(state, DockerState):
        # 兜底：register() 之前的极端调用路径也能工作（正常不会走到）
        state = DockerState(DockerSettings.from_env(core.settings))
        core.data["docker"] = state
    return state


def _to_int(value: Any, default: int = 1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _chat_id(update: Update) -> Optional[int]:
    chat = update.effective_chat
    return int(chat.id) if chat is not None else None


async def _answer(update: Update, text: str = "", alert: bool = False) -> None:
    """回调就 answer，命令消息就 reply——保证拒绝/过期永远有可见回执。"""
    query = update.callback_query
    if query is not None:
        try:
            if text:
                await query.answer(text, show_alert=alert)
            else:
                await query.answer()
        except Exception:
            pass
        return
    message = update.effective_message
    if text and message is not None:
        try:
            await message.reply_text(text)
        except Exception:
            pass


async def _guard(core: Core, update: Update, private: bool = False) -> bool:
    """统一入口鉴权：默认拒绝（`❌ 无权限`）。"""
    user = update.effective_user
    if user is not None and core.acl.can(user.id, "docker"):
        return True
    query = update.callback_query
    if query is not None:
        try:
            await query.answer("❌ 无权限", show_alert=True)
        except Exception:
            pass
        return False
    message = update.effective_message
    if message is not None:
        try:
            await message.reply_text("❌ 无权限")
        except Exception:
            pass
    return False


async def _expired(core: Core, update: Update) -> None:
    """菜单已过期（内存载荷被清理 / Bot 重启）：给明确提示 + 一键重开，不静默失败。"""
    await _answer(update, "⚠️ 菜单已过期，请重新打开项目列表", alert=True)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 重新打开项目列表", callback_data=cb_simple("d", "page_turn", 1)
                )
            ]
        ]
    )
    await core.panels.render(
        "docker",
        update,
        "⚠️ <b>菜单已过期</b>（Bot 重启或回调缓存被清理）。\n"
        "请点下面的按钮或发送 <code>/d_list</code> 重新打开。",
        keyboard,
    )


def _jobs_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🧰 任务中心", callback_data=nav_jobs())]]
    )


def _back_keyboard(page: int = 1) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 返回列表", callback_data=cb_simple("d", "page_turn", page))]]
    )


def _progress(core: Core, job: Job):
    """把 compose 的流式进度写回 JobCenter。"""

    def _on_progress(pct: int, detail: str = "") -> None:
        core.jobs.update(job, progress=pct, detail=detail)

    return _on_progress


def _user_id(update: Update) -> Optional[int]:
    user = update.effective_user
    return user.id if user is not None else None


def _done_text(job: Job, extra: str = "") -> str:
    """收尾面板的正文：统一的完成卡片行 +（可选）本流程自己的明细。

    合并后统一成「一次操作一条消息」：交互式任务不再另发「✅ 🐳 升级项目 mt」卡片，
    结果直接画在这条面板上，`card_text()` 保证三个模块的收尾长得一样。
    """
    text = card_text(job)
    if extra:
        text += "\n" + extra.rstrip()
    return text


def _finish_keyboard(core: Core, user_id: Optional[int], page: int = 1) -> InlineKeyboardMarkup:
    """收尾键盘：「🔙 返回列表」+ 一行跨模块入口；排不下就只剩面板补的 🏠 返回。"""
    rows = [[InlineKeyboardButton("🔙 返回列表", callback_data=cb_simple("d", "page_turn", page))]]
    extra = next_actions_keyboard(core, user_id, "docker")
    if extra is not None:
        rows.extend(extra.inline_keyboard)
    return InlineKeyboardMarkup(rows)


async def _busy(core: Core, update: Update) -> None:
    await _answer(update, _BUSY_TEXT, alert=True)


# ==================== 项目列表 ====================
async def _render_list(
    core: Core, update: Update, *, page: int = 1, force_refresh: bool = False
) -> None:
    """主面板列表（支持分页、统计与运行状态分组）。"""
    await _answer(update)
    state = _state(core)
    settings = state.settings
    projects = await state.get_projects(force_refresh=force_refresh)
    total_projects = len(projects)
    running_cnt = sum(1 for p in projects if "running" in str(p.get("status", "")).lower())
    ordered = sort_projects_for_display(projects)
    page_projects, page, total_pages = paginate_projects(ordered, page, settings.page_size)

    text = "📊 <b>统计：</b>共 %d 个项目 | 🟢 %d 运行中 | 🟡 %d 停止\n" % (
        total_projects,
        running_cnt,
        total_projects - running_cnt,
    )
    text += "📖 <b>页码：</b>%d / %d\n" % (page, total_pages)
    if state.compose_bin is not None and not state.compose_bin:
        text += "⚠️ 未检测到 <code>docker compose</code> / <code>docker-compose</code> 命令\n"
    text += "\n"

    keyboard: list[list[InlineKeyboardButton]] = []
    start_idx = (page - 1) * settings.page_size

    if not ordered:
        text += "⚠️ 暂未检测到任何 Docker Compose 项目\n"
        # 空列表必须说清是「权限不够」「目录没挂进来」还是「compose 命令缺失」，
        # 否则用户只能猜（缺命令那行上面已经单独打印过了）。
        for hint in scan_hint(state, include_compose=False):
            text += hint + "\n"
    else:
        last_group: Optional[str] = None
        for i, p in enumerate(page_projects, start=start_idx + 1):
            is_running = "running" in str(p.get("status", "")).lower()
            group = "running" if is_running else "stopped"
            if group != last_group:
                text += "🟢 <b>运行中</b>\n" if is_running else "🟡 <b>已停止</b>\n"
                last_group = group

            num = "%02d" % i
            name = str(p.get("name", ""))
            status = str(p.get("status", ""))
            status_icon = "🟢" if is_running else "🟡"
            disp_name = name[:26] + ".." if len(name) > 28 else name
            services = list(p.get("services") or [])
            services_str = ", ".join(services) if services else "-"

            text += "<b>%s.</b> %s %s <code>[%s]</code>\n" % (
                num,
                esc(name),
                status_icon,
                esc(status),
            )
            text += "     路径：<code>%s</code>\n" % esc(p.get("dir", ""))
            text += "     容器：%s\n\n" % esc(services_str)

            if len(services) > 1:
                data = cb("d", "p_sel", {"name": name, "page": page})
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            "⚙️ %s. %s (多服务)" % (num, disp_name), callback_data=data
                        )
                    ]
                )
            else:
                data = cb("d", "up_s_ask", {"name": name, "page": page})
                keyboard.append(
                    [InlineKeyboardButton("🚀 %s. %s" % (num, disp_name), callback_data=data)]
                )

    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(
            InlineKeyboardButton("◀ 上一页", callback_data=cb_simple("d", "page_turn", page - 1))
        )
    nav.append(
        InlineKeyboardButton("📄 %d/%d" % (page, total_pages), callback_data=cb_simple("d", "noop"))
    )
    if page < total_pages:
        nav.append(
            InlineKeyboardButton("下一页 ▶", callback_data=cb_simple("d", "page_turn", page + 1))
        )
    keyboard.append(nav)
    keyboard.append(
        [
            InlineKeyboardButton("🧹 镜像清理菜单", callback_data=cb_simple("d", "prune_menu")),
            InlineKeyboardButton("⬆️ 升级全部项目", callback_data=cb_simple("d", "upgrade_all")),
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton(
                "🔄 刷新状态", callback_data=cb_simple("d", "refresh", page)
            )
        ]
    )

    await core.panels.render("docker", update, text, InlineKeyboardMarkup(keyboard))


async def _show_detail(
    core: Core, update: Update, project_name: str, back_page: int = 1
) -> None:
    """项目卡片：选整项目升级还是单服务升级。"""
    await _answer(update)
    state = _state(core)
    projects = await state.get_projects()
    target = next((p for p in projects if p.get("name") == project_name), None)

    if target is None:
        await core.panels.render(
            "docker",
            update,
            "❌ <b>未找到该项目</b>：<code>%s</code>（可能已被移除，请刷新）" % esc(project_name),
            _back_keyboard(back_page),
        )
        return

    is_running = "running" in str(target.get("status", "")).lower()
    text = "📦 <b>项目卡片：%s</b>\n\n" % esc(target["name"])
    text += "📂 <b>路径：</b><code>%s</code>\n" % esc(target["dir"])
    text += "%s <b>状态：</b>%s\n\n" % (
        "🟢" if is_running else "🟡",
        esc(target.get("status", "")),
    )
    text += "⚙️ <b>请选择操作控制范围：</b>\n"

    keyboard = [
        [
            InlineKeyboardButton(
                "⚡ 升级全部服务容器",
                callback_data=cb("d", "up_s_ask", {"name": project_name, "page": back_page}),
            )
        ]
    ]
    for svc in target.get("services") or []:
        disp_svc = svc[:24] + ".." if len(svc) > 26 else svc
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🔹 仅升级服务: %s" % disp_svc,
                    callback_data=cb(
                        "d", "up_svc_ask", {"name": project_name, "svc": svc, "page": back_page}
                    ),
                )
            ]
        )
    keyboard.append(
        [
            InlineKeyboardButton(
                "🔙 返回列表", callback_data=cb_simple("d", "page_turn", back_page)
            )
        ]
    )

    await core.panels.render("docker", update, text, InlineKeyboardMarkup(keyboard))


# ==================== 两步确认（panels.ask_confirm） ====================
async def _ask_project_upgrade(
    core: Core, update: Update, project_name: str, back_page: int = 1
) -> None:
    """整项目升级确认（LDMG ask_single_upgrade）。"""
    await _answer(update)
    state = _state(core)
    projects = await state.get_projects()
    target = next((p for p in projects if p.get("name") == project_name), None)

    safe_name = esc(project_name)
    safe_dir = esc(target["dir"]) if target else "未知路径"
    # 不在这里探测 compose（同步 subprocess 会卡事件循环），用已缓存结果或默认展示
    compose = " ".join(state.compose_bin or ["docker", "compose"])
    confirm_data = cb("d", "up_p_do", {"name": project_name, "page": back_page})

    text = (
        "🚀 <b>升级确认 - [%s]</b>\n\n"
        "📂 <b>工作目录：</b><code>%s</code>\n"
        "🛠 <b>执行步骤：</b>\n"
        "  1. <code>%s pull</code>\n"
        "  2. <code>%s up -d</code>\n\n"
        "⏱ 确认后立即执行，可在进度消息里中断。"
        % (safe_name, safe_dir, esc(compose), esc(compose))
    )
    await core.panels.ask_confirm(
        "docker",
        update,
        text,
        confirm_data,
        cancel_data=cb_simple("d", "page_turn", back_page),
        confirm_label="✅ 确认升级",
        cancel_label="🔙 取消返回",
    )


async def _ask_service_upgrade(
    core: Core, update: Update, project_name: str, service_name: str, back_page: int = 1
) -> None:
    """单服务升级确认（LDMG ask_svc_upgrade）。"""
    await _answer(update)
    state = _state(core)
    projects = await state.get_projects()
    target = next((p for p in projects if p.get("name") == project_name), None)

    safe_project = esc(project_name)
    safe_service = esc(service_name)
    safe_dir = esc(target["dir"]) if target else "未知路径"
    confirm_data = cb(
        "d", "up_svc_do", {"name": project_name, "svc": service_name, "page": back_page}
    )
    cancel_data = cb("d", "p_sel", {"name": project_name, "page": back_page})

    text = (
        "🚀 <b>服务升级确认 - [%s]</b>\n\n"
        "📦 <b>所属项目：</b>%s\n"
        "📂 <b>工作路径：</b><code>%s</code>\n\n"
        "💡 仅重建并升级 <code>%s</code>，项目内其他容器不受影响。"
        % (safe_service, safe_project, safe_dir, safe_service)
    )
    await core.panels.ask_confirm(
        "docker",
        update,
        text,
        confirm_data,
        cancel_data=cancel_data,
        confirm_label="✅ 确认升级单一服务",
        cancel_label="🔙 返回",
    )


async def _ask_upgrade_all(core: Core, update: Update) -> None:
    """批量升级全部项目的确认（LDMG upgrade_all）。"""
    await _answer(update)
    state = _state(core)
    projects = await state.get_projects()
    text = (
        "⚠️ <b>确认批量升级全部项目？</b>\n"
        "共有 %d 个 Compose 项目，将依次执行 <code>pull</code> + <code>up -d</code>。\n"
        "过程可在 🧰 任务中心或进度消息里中断。" % len(projects)
    )
    await core.panels.ask_confirm(
        "docker",
        update,
        text,
        cb_simple("d", "upgrade_all_confirm"),
        cancel_data=cb_simple("d", "page_turn", 1),
        confirm_label="🚀 确认升级全部",
        cancel_label="❌ 取消",
    )


# ==================== 长任务：升级 ====================
async def _do_upgrade_project(
    core: Core, update: Update, context: Any, project_name: str
) -> None:
    """升级整个项目：pull → up -d（LDMG do_upgrade_project）。"""
    state = _state(core)
    if update.effective_message is None:
        await _answer(update, "⚠️ 当前会话不可用，请重新用 /d_list 打开面板")
        return
    task_id = uuid.uuid4().hex
    if not await state.begin_task(task_id):
        await _busy(core, update)
        return

    job = core.jobs.add(
        "docker",
        "升级项目 %s" % project_name,
        cancel=state.request_cancel,
        chat_id=_chat_id(update),
    )
    status, detail = FAILED, "未执行"

    try:
        await _answer(update)
        projects = await state.get_projects()
        target = next((p for p in projects if p.get("name") == project_name), None)

        if target is None:
            status, detail = FAILED, "找不到项目 %s（可能已删除或改名，/d_list 可刷新）" % project_name
        elif not await asyncio.to_thread(state.get_compose_bin):
            status, detail = FAILED, "未检测到 docker compose / docker-compose 命令"
        else:
            safe_name = esc(target["name"])
            await core.panels.render(
                "docker",
                update,
                "⏳ <b>开始升级项目 [%s]</b>\n阶段 1/2：拉取新镜像" % safe_name,
                _jobs_keyboard(),
            )
            log.info("升级项目 [%s]", project_name)

            pull_ok = await run_command_with_feedback(
                state,
                update.effective_message,
                state.build_compose_cmd(target, "pull"),
                cwd=target["dir"],
                title="拉取新镜像 - %s" % target["name"],
                progress_pct=30,
                task_id=task_id,
                on_progress=_progress(core, job),
                delete_on_success=True,
            )
            up_ok = False
            if pull_ok and not state.cancel_requested:
                await core.panels.render(
                    "docker",
                    update,
                    "⏳ <b>升级项目 [%s]</b>\n阶段 2/2：重建与启动" % safe_name,
                    _jobs_keyboard(),
                )
                up_ok = await run_command_with_feedback(
                    state,
                    update.effective_message,
                    state.build_compose_cmd(target, "up", "-d"),
                    cwd=target["dir"],
                    title="重建与启动 - %s" % target["name"],
                    progress_pct=80,
                    task_id=task_id,
                    on_progress=_progress(core, job),
                    delete_on_success=True,
                )

            if state.cancel_requested:
                status, detail = CANCELLED, "已按用户请求取消"
            elif pull_ok and up_ok:
                status, detail = DONE, "项目整体升级完成"
            else:
                status, detail = FAILED, "拉取镜像或重建启动失败（详情见上面的执行消息）"
    except Exception as exc:
        log.exception("升级项目 %s 异常", project_name)
        status, detail = FAILED, "执行异常：%s" % exc
    finally:
        state.invalidate_cache()
        state.end_task()
        core.jobs.finish(job, status, detail)

    await core.panels.render(
        "docker", update, _done_text(job), _finish_keyboard(core, _user_id(update))
    )


async def _do_upgrade_service(
    core: Core, update: Update, context: Any, project_name: str, service_name: str
) -> None:
    """升级单个服务：pull <svc> → up -d <svc>（LDMG do_upgrade_service）。"""
    state = _state(core)
    if update.effective_message is None:
        await _answer(update, "⚠️ 当前会话不可用，请重新用 /d_list 打开面板")
        return
    task_id = uuid.uuid4().hex
    if not await state.begin_task(task_id):
        await _busy(core, update)
        return

    job = core.jobs.add(
        "docker",
        "升级服务 %s / %s" % (project_name, service_name),
        cancel=state.request_cancel,
        chat_id=_chat_id(update),
    )
    status, detail = FAILED, "未执行"

    try:
        await _answer(update)
        projects = await state.get_projects()
        target = next((p for p in projects if p.get("name") == project_name), None)

        if target is None:
            status, detail = FAILED, "未找到项目 %s（/d_list 可刷新）" % project_name
        elif service_name not in list(target.get("services") or []):
            status, detail = FAILED, "项目 %s 中没有服务 %s" % (project_name, service_name)
        elif not await asyncio.to_thread(state.get_compose_bin):
            status, detail = FAILED, "未检测到 docker compose / docker-compose 命令"
        else:
            safe_project = esc(target["name"])
            safe_service = esc(service_name)
            await core.panels.render(
                "docker",
                update,
                "⏳ <b>升级服务 [%s]（%s）</b>\n阶段 1/2：拉取服务镜像"
                % (safe_service, safe_project),
                _jobs_keyboard(),
            )
            log.info("升级服务 [%s -> %s]", project_name, service_name)

            pull_ok = await run_command_with_feedback(
                state,
                update.effective_message,
                state.build_compose_cmd(target, "pull", service_name),
                cwd=target["dir"],
                title="拉取服务镜像 - %s" % service_name,
                progress_pct=30,
                task_id=task_id,
                on_progress=_progress(core, job),
                delete_on_success=True,
            )
            up_ok = False
            if pull_ok and not state.cancel_requested:
                await core.panels.render(
                    "docker",
                    update,
                    "⏳ <b>升级服务 [%s]（%s）</b>\n阶段 2/2：重建启动服务"
                    % (safe_service, safe_project),
                    _jobs_keyboard(),
                )
                up_ok = await run_command_with_feedback(
                    state,
                    update.effective_message,
                    state.build_compose_cmd(target, "up", "-d", service_name),
                    cwd=target["dir"],
                    title="重建启动服务 - %s" % service_name,
                    progress_pct=80,
                    task_id=task_id,
                    on_progress=_progress(core, job),
                    delete_on_success=True,
                )

            if state.cancel_requested:
                status, detail = CANCELLED, "已按用户请求取消"
            elif pull_ok and up_ok:
                status, detail = DONE, "服务升级完成"
            else:
                status, detail = FAILED, "拉取镜像或重建启动失败（详情见上面的执行消息）"
    except Exception as exc:
        log.exception("升级服务 %s/%s 异常", project_name, service_name)
        status, detail = FAILED, "执行异常：%s" % exc
    finally:
        state.invalidate_cache()
        state.end_task()
        core.jobs.finish(job, status, detail)

    await core.panels.render(
        "docker", update, _done_text(job), _finish_keyboard(core, _user_id(update))
    )


async def _do_upgrade_all(core: Core, update: Update, context: Any) -> None:
    """批量升级全部项目（LDMG do_upgrade_all）。"""
    state = _state(core)
    if update.effective_message is None:
        await _answer(update, "⚠️ 当前会话不可用，请重新用 /d_list 打开面板")
        return
    task_id = uuid.uuid4().hex
    if not await state.begin_task(task_id):
        await _busy(core, update)
        return

    job = core.jobs.add(
        "docker",
        "批量升级全部项目",
        cancel=state.request_cancel,
        chat_id=_chat_id(update),
    )
    status, detail = FAILED, "未执行"
    extra = ""

    try:
        await _answer(update)
        projects = sort_projects_for_display(await state.get_projects())

        if not projects:
            status, detail = FAILED, "未检测到可升级的项目"
        elif not await asyncio.to_thread(state.get_compose_bin):
            status, detail = FAILED, "未检测到 docker compose / docker-compose 命令"
        else:
            await core.panels.render(
                "docker",
                update,
                "⏳ <b>开始批量升级全部 %d 个项目…</b>" % len(projects),
                _jobs_keyboard(),
            )
            log.info("批量升级共 %d 个项目", len(projects))

            success_list: list[str] = []
            fail_list: list[str] = []
            aborted = False
            processed = 0

            for i, project in enumerate(projects, 1):
                if state.cancel_requested:
                    aborted = True
                    break
                processed = i
                pct = int((i / len(projects)) * 100)
                pull_ok = await run_command_with_feedback(
                    state,
                    update.effective_message,
                    state.build_compose_cmd(project, "pull"),
                    cwd=project["dir"],
                    title="批量拉取 - %s" % project["name"],
                    progress_pct=pct,
                    task_id=task_id,
                    on_progress=_progress(core, job),
                    delete_on_success=True,
                )
                up_ok = False
                if pull_ok and not state.cancel_requested:
                    up_ok = await run_command_with_feedback(
                        state,
                        update.effective_message,
                        state.build_compose_cmd(project, "up", "-d"),
                        cwd=project["dir"],
                        title="批量启动 - %s" % project["name"],
                        progress_pct=pct,
                        task_id=task_id,
                        on_progress=_progress(core, job),
                        delete_on_success=True,
                    )
                if pull_ok and up_ok:
                    success_list.append(project["name"])
                else:
                    fail_list.append(project["name"])

            # 批量升级的结论写在卡片行里，这里只补「谁成谁败」的明细
            extra = "✅ <b>成功 (%d)：</b> %s\n❌ <b>失败 (%d)：</b> %s" % (
                len(success_list),
                esc(", ".join(success_list)) if success_list else "无",
                len(fail_list),
                esc(", ".join(fail_list)) if fail_list else "无",
            )
            if aborted:
                status = CANCELLED
                detail = "已中止（剩余 %d 个项目）" % (len(projects) - processed)
                extra = "剩余 %d 个项目未处理\n%s" % (len(projects) - processed, extra)
            elif not success_list:
                status, detail = FAILED, "全部项目升级失败"
            elif fail_list:
                status = DONE
                detail = "成功 %d / 失败 %d" % (len(success_list), len(fail_list))
            else:
                status, detail = DONE, "全部 %d 个项目升级完成" % len(success_list)
    except Exception as exc:
        log.exception("批量升级异常")
        status, detail = FAILED, "执行异常：%s" % exc
    finally:
        state.invalidate_cache()
        state.end_task()
        core.jobs.finish(job, status, detail)

    await core.panels.render(
        "docker", update, _done_text(job, extra), _finish_keyboard(core, _user_id(update))
    )


# ==================== 镜像清理 ====================
async def _show_prune_menu(core: Core, update: Update) -> None:
    """镜像清理菜单（LDMG show_prune_menu）。"""
    await _answer(update)
    text = (
        "🧹 <b>Docker 镜像清理中心</b>\n\n"
        "请选择清理类型：\n"
        "• <b>悬空镜像 (Dangling)</b>：无标签且未被使用的临时镜像层（安全推荐）\n"
        "• <b>所有未使用镜像 (All Unused)</b>：没有任何容器正在使用的全部旧镜像（深度清理）"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "🍂 仅清理悬空镜像 (Dangling)",
                callback_data=cb_simple("d", "prune_req", "dangling"),
            )
        ],
        [
            InlineKeyboardButton(
                "🗑 清理所有未使用镜像 (All Unused)",
                callback_data=cb_simple("d", "prune_req", "all"),
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 返回主菜单", callback_data=cb_simple("d", "page_turn", 1)
            )
        ],
    ]
    await core.panels.render("docker", update, text, InlineKeyboardMarkup(keyboard))


async def _ask_prune_confirm(core: Core, update: Update, prune_all: bool) -> None:
    """扫描候选镜像 → 两步确认（LDMG ask_prune_confirm）。"""
    state = _state(core)
    await _answer(update)
    label = "所有未使用" if prune_all else "悬空 (dangling)"

    await core.panels.render(
        "docker", update, "🔍 正在扫描系统中的 <b>%s</b> 镜像..." % label
    )

    try:
        ok, dry_output, error_output = await scan_prune_candidates(state, prune_all)
        if not ok:
            await core.panels.render(
                "docker",
                update,
                "❌ 扫描镜像异常：<code>%s</code>" % format_prune_snapshot(error_output),
                _back_keyboard(1),
            )
            return
        if not dry_output:
            await core.panels.render(
                "docker",
                update,
                "✨ 系统内未检测到可清理的 <b>%s</b> 镜像！" % label,
                _back_keyboard(1),
            )
            return
    except Exception as exc:
        log.exception("扫描 prune 候选异常")
        await core.panels.render(
            "docker",
            update,
            "❌ 执行扫描出错: <code>%s</code>" % esc(str(exc)),
            _back_keyboard(1),
        )
        return

    confirm_data = cb_simple("d", "prune_do", "all" if prune_all else "dangling")
    text = (
        "🧹 <b>当前可清理镜像快照 (范围: %s)：</b>\n"
        "<code>%s</code>\n\n"
        "确认时 Docker 会重新判断实际可清理范围。\n确认执行清理吗？"
        % (label, format_prune_snapshot(dry_output))
    )
    await core.panels.ask_confirm(
        "docker",
        update,
        text,
        confirm_data,
        cancel_data=cb_simple("d", "prune_menu"),
        confirm_label="✅ 确认清理",
        cancel_label="❌ 取消",
    )


async def _do_prune(core: Core, update: Update, context: Any, prune_all: bool) -> None:
    """执行镜像清理（LDMG do_prune）。"""
    state = _state(core)
    if update.effective_message is None:
        await _answer(update, "⚠️ 当前会话不可用，请重新用 /d_list 打开面板")
        return
    task_id = uuid.uuid4().hex
    if not await state.begin_task(task_id):
        await _busy(core, update)
        return

    label = "所有未使用" if prune_all else "悬空"
    job = core.jobs.add(
        "docker",
        "清理%s镜像" % label,
        cancel=state.request_cancel,
        chat_id=_chat_id(update),
    )
    status, detail = FAILED, "未执行"
    extra = ""

    try:
        await _answer(update)
        await core.panels.render(
            "docker", update, "🗑 <b>正在执行%s镜像清理，请稍候...</b>" % label, _jobs_keyboard()
        )
        cmd = ["docker", "image", "prune", "-f"]
        if prune_all:
            cmd.append("-a")

        captured: list[str] = []
        success = await run_command_with_feedback(
            state,
            update.effective_message,
            cmd,
            title="清理系统镜像",
            progress_pct=90,
            task_id=task_id,
            on_progress=_progress(core, job),
            delete_on_success=True,
            out=captured,
        )
        if success:
            state.invalidate_cache()
            status, detail = DONE, "%s镜像清理完成" % label
            # 执行消息会被删掉，把回收空间这条结论抄进收尾面板
            reclaimed = next(
                (
                    line.strip()
                    for line in "".join(captured).splitlines()
                    if "reclaimed space" in line.lower()
                ),
                "",
            )
            if reclaimed:
                extra = esc(reclaimed)
        elif state.cancel_requested:
            status, detail = CANCELLED, "已按用户请求取消"
        else:
            status, detail = FAILED, "%s镜像清理失败（详情见上面的执行消息）" % label
    except Exception as exc:
        log.exception("镜像清理异常")
        status, detail = FAILED, "执行异常：%s" % exc
    finally:
        state.end_task()
        core.jobs.finish(job, status, detail)

    await core.panels.render(
        "docker", update, _done_text(job, extra), _finish_keyboard(core, _user_id(update))
    )


# ==================== 容器状态速览 ====================
async def _show_status(core: Core, update: Update) -> None:
    """`docker ps -a` 状态速览（LDMG cmd_status）：截断 + HTML 转义。"""
    await _answer(update)
    state = _state(core)
    await core.panels.render("docker", update, "🔍 正在拉取 Docker 容器状态速览...")

    try:
        ok, output = await dump_container_status(state)
    except Exception as exc:
        log.exception("获取容器状态异常")
        await core.panels.render(
            "docker", update, "❌ 获取状态失败: <code>%s</code>" % esc(str(exc)), _back_keyboard()
        )
        return

    if not ok:
        await core.panels.render(
            "docker",
            update,
            "❌ 获取 Docker 状态失败: <code>%s</code>" % esc(output[-1500:]),
            _back_keyboard(),
        )
        return
    if not output:
        await core.panels.render(
            "docker", update, "⚠️ 未找到正在运行或已停止的 Docker 容器。", _back_keyboard()
        )
        return

    await core.panels.render(
        "docker",
        update,
        "📊 <b>Docker 容器实时状态速览</b>\n\n<code>%s</code>" % esc(output[-3800:]),
        _back_keyboard(),
    )


# ==================== 回调分发 ====================
def _parse_callback(data: str) -> tuple[str, str, list[str], Optional[dict]]:
    """拆回调：cb_args 管命名空间/动作，cb_parts 管短参数，cb_parse 管内存载荷。"""
    prefix, action = cb_args(data)
    return prefix, action, cb_parts(data), cb_parse(data)[2]


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`d|` 命名空间的全部按钮（LDMG button_handler）。"""
    core = core_of(context)
    query = update.callback_query
    if query is None:
        return
    if not await _guard(core, update):
        return

    data = query.data or ""
    prefix, action, parts, payload = _parse_callback(data)
    if prefix != "d":
        await _answer(update, "⚠️ 未知回调（菜单可能已过期），请重新 /d_list 打开", alert=True)
        return
    state = _state(core)

    if action == "task_cancel":
        task_id = parts[2] if len(parts) > 2 and parts[2] != "-" else None
        if state.request_cancel(task_id):
            await _answer(update, "🛑 已发送中断信号，正在停止任务...", alert=True)
        else:
            await _answer(update, "⏳ 该任务已结束或不存在", alert=True)
        return

    if action == "noop":
        await _answer(update)
        return

    if action in _PAYLOAD_ACTIONS and payload is None:
        await _expired(core, update)
        return

    if action == "page_turn":
        await _render_list(core, update, page=_to_int(parts[2] if len(parts) > 2 else 1))
    elif action == "refresh":
        await _render_list(
            core,
            update,
            page=_to_int(parts[2] if len(parts) > 2 else 1),
            force_refresh=True,
        )
    elif action == "p_sel":
        await _show_detail(
            core, update, payload["name"], _to_int(payload.get("page"), 1)
        )
    elif action == "up_s_ask":
        await _ask_project_upgrade(
            core, update, payload["name"], _to_int(payload.get("page"), 1)
        )
    elif action == "up_svc_ask":
        await _ask_service_upgrade(
            core,
            update,
            payload["name"],
            payload["svc"],
            _to_int(payload.get("page"), 1),
        )
    elif action == "up_p_do":
        ok, why = core.panels.validate_confirm(query, data)
        if not ok:
            await _answer(update, why, alert=True)
            return
        await _do_upgrade_project(core, update, context, payload["name"])
    elif action == "up_svc_do":
        ok, why = core.panels.validate_confirm(query, data)
        if not ok:
            await _answer(update, why, alert=True)
            return
        await _do_upgrade_service(core, update, context, payload["name"], payload["svc"])
    elif action == "upgrade_all":
        await _ask_upgrade_all(core, update)
    elif action == "upgrade_all_confirm":
        ok, why = core.panels.validate_confirm(query, data)
        if not ok:
            await _answer(update, why, alert=True)
            return
        await _do_upgrade_all(core, update, context)
    elif action == "prune_menu":
        await _show_prune_menu(core, update)
    elif action == "prune_req":
        await _ask_prune_confirm(
            core, update, prune_all=(parts[2] if len(parts) > 2 else "dangling") == "all"
        )
    elif action == "prune_do":
        ok, why = core.panels.validate_confirm(query, data)
        if not ok:
            await _answer(update, why, alert=True)
            return
        await _do_prune(core, update, context, prune_all=(parts[2] if len(parts) > 2 else "dangling") == "all")
    else:
        await _answer(update, "⚠️ 未知操作（菜单可能已过期），请重新 /d_list 打开", alert=True)


# ==================== 命令入口 ====================
async def _upgrade_command(core: Core, update: Update, context: Any) -> None:
    """`/upgrade` 的三种用法：`01` / `01 emby` / `all`。"""
    args = list(getattr(context, "args", None) or [])
    if not args:
        await core.panels.render("docker", update, _UPGRADE_GUIDE, _back_keyboard())
        return

    arg = str(args[0]).lower()
    service_name = args[1] if len(args) > 1 else None

    if arg in ("all", "a"):
        await _ask_upgrade_all(core, update)
        return

    try:
        num = int(arg)
        if num < 1:
            raise ValueError
    except ValueError:
        await core.panels.render(
            "docker",
            update,
            "❌ 格式不正确。示例: <code>/upgrade 01</code> 或 <code>/upgrade 01 emby</code>",
            _back_keyboard(),
        )
        return

    projects = sort_projects_for_display(await _state(core).get_projects())
    if not 1 <= num <= len(projects):
        await core.panels.render(
            "docker", update, "❌ 无效的项目序号（当前共 %d 个）" % len(projects), _back_keyboard()
        )
        return

    target = projects[num - 1]
    if service_name:
        if service_name not in list(target.get("services") or []):
            await core.panels.render(
                "docker",
                update,
                "❌ 项目 <b>%s</b> 中不存在服务 <code>%s</code>"
                % (esc(target["name"]), esc(service_name)),
                _back_keyboard(),
            )
            return
        await _ask_service_upgrade(core, update, target["name"], service_name, 1)
    else:
        await _ask_project_upgrade(core, update, target["name"], 1)


async def cmd_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    await _upgrade_command(core, update, context)


async def cmd_prune(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    await _show_prune_menu(core, update)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    await _render_list(core, update)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    await _show_status(core, update)


# ==================== ModuleSpec 入口 ====================
async def open_panel(
    core: Core, update: Update, context: Any = None, *, page: int = 1
) -> None:
    """router 的 `nav|open|docker` / 首页按钮入口。"""
    if not await _guard(core, update):
        return
    await _render_list(core, update, page=page)


async def show_list(
    core: Core, update: Update, context: Any = None, *, page: int = 1
) -> None:
    """`/d_list` 与 `/list` 在 docker 语境下的入口。"""
    if not await _guard(core, update):
        return
    await _render_list(core, update, page=page)


async def show_status(core: Core, update: Update, context: Any = None) -> None:
    """`/d_status` 与 `/status` 在 docker 语境下的入口。"""
    if not await _guard(core, update):
        return
    await _show_status(core, update)


# ==================== 兜底救援（全角斜杠/零宽字符由 router 归一后回调） ====================
async def _rescue_upgrade(core: Core, update: Update, context: Any) -> None:
    if not await _guard(core, update):
        return
    await _upgrade_command(core, update, context)


async def _rescue_prune(core: Core, update: Update, context: Any) -> None:
    if not await _guard(core, update):
        return
    await _show_prune_menu(core, update)


async def _rescue_list(core: Core, update: Update, context: Any) -> None:
    if not await _guard(core, update):
        return
    await _render_list(core, update)


async def _rescue_status(core: Core, update: Update, context: Any) -> None:
    if not await _guard(core, update):
        return
    await _show_status(core, update)


#: router 兜底命令名 -> handler(core, update, context)
RESCUE = {
    "upgrade": _rescue_upgrade,
    "prune": _rescue_prune,
    "d_list": _rescue_list,
    "d_status": _rescue_status,
}


def _registered_commands(app: Application) -> set[str]:
    """已经注册过的命令名（router 会先注册 /d_list /d_status 这类永久别名）。"""
    names: set[str] = set()
    for handlers in getattr(app, "handlers", {}).values():
        for handler in handlers:
            names |= set(getattr(handler, "commands", None) or ())
    return names


def register(app: Application, core: Core) -> None:
    """注册 docker 模块的 PTB handler（绝不碰 start/help/status/list/menu/home/cancel/jobs/id）。

    `/d_list` `/d_status` 是契约里的 docker 永久别名，但合并后的 `router` 会**先**注册它们
    并转发到 `spec.show_list` / `spec.show_status`。同一条命令注册两次虽然运行时无害，
    却会让装配级「命令不打架」检查失败，所以这里只在命令还没被注册时补上。
    """
    core.data["docker"] = DockerState(DockerSettings.from_env(core.settings))

    owned = _registered_commands(app)
    plan = (
        ("upgrade", cmd_upgrade),
        ("prune", cmd_prune),
        ("d_list", cmd_list),
        ("d_status", cmd_status),
    )
    skipped: list[str] = []
    for name, handler in plan:
        if name in owned:
            skipped.append(name)
            continue
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^d\|"))

    log.info(
        "docker 模块 handler 已注册：%s / ^d\\|%s",
        " / ".join(name for name, _ in plan if name not in skipped),
        ("（%s 已由 router 注册，跳过）" % "、".join(skipped)) if skipped else "",
    )


__all__ = [
    "RESCUE",
    "button_handler",
    "cmd_list",
    "cmd_prune",
    "cmd_status",
    "cmd_upgrade",
    "open_panel",
    "register",
    "show_list",
    "show_status",
]
