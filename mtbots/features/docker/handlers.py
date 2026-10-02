"""Docker 模块的 handler 层：命令 → 面板 → 长任务（core.jobs）。

对照 LDMG 的 `bot.py`：逻辑照搬，只换壳——
  · 鉴权从 `check_permission` 换成 `core.acl.can(user_id, "docker")`（默认拒绝）；
  · 回调整理成 `d|` 命名空间（`panels.cb` / `cb_simple`），主面板/详情/确认全部走
    `core.panels`（一条会话一个面板 + 面包屑 + 🏠 返回 + 两步确认）；
  · 升级 / 清理注册进 `core.jobs`；跑的过程把进度画在面板上（键盘带 🛑 中断执行），
    跑完把 `jobs.card_text()` 也画在同一面板上——交互式任务不再另发完成卡片；
  · 多主机（v1.1.0）：项目按 `(host, name)` 定位，远端主机的命令在 `compose.DockerState`
    里就被包成 ssh 调用；本文件只负责「面板按主机分组 + 回调只认配置内的 host id」。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional, Sequence

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
    nav_home,
    nav_jobs,
    next_actions_keyboard,
)
from ...text import esc
from .compose import (
    DockerState,
    make_state,
    dump_container_status,
    format_prune_snapshot,
    paginate_projects,
    run_command_with_feedback,
    scan_hint,
    scan_prune_candidates,
)
from .config import DockerSettings
from .hosts import explain_exit

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
    """取本模块的 DockerState；没有就用 make_state() 建一个（会读主机清单）。"""
    state = core.data.get("docker")
    if not isinstance(state, DockerState):
        # 兜底也要走 make_state：直接 DockerState(...) 会丢掉主机清单，静默退回单机
        state = make_state(DockerSettings.from_env(core.settings))
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


def _progress_keyboard(task_id: Optional[str]) -> InlineKeyboardMarkup:
    """执行中的面板键盘：中断 + 任务中心。

    进度面板是用户唯一会一直盯着的那条消息，取消按钮必须在这儿——每一步的执行消息
    会随阶段/项目不断新建删除，想取消还得先把当前那条翻出来。
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🛑 中断执行", callback_data=cb_simple("d", "task_cancel", task_id or "-")
                ),
                InlineKeyboardButton("🧰 任务中心", callback_data=nav_jobs()),
            ]
        ]
    )


def _list_back(core: Core, page: int, host: Optional[str] = None) -> InlineKeyboardMarkup:
    """项目/确认页面的「返回列表」：多主机时回到那台主机的列表，单主机保持原回调。"""
    state = _state(core)
    return _back_keyboard(page, (host or "all") if state.multi_host else None)


def _back_keyboard(page: int = 1, list_host: Optional[str] = None) -> InlineKeyboardMarkup:
    """返回列表；多主机时带上「当前看的是哪台」，免得又跳回选主机页。"""
    data = (
        cb_simple("d", "page_turn", page, list_host)
        if list_host
        else cb_simple("d", "page_turn", page)
    )
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回列表", callback_data=data)]])


def _progress(core: Core, job: Job):
    """把 compose 的流式进度写回 JobCenter。"""

    def _on_progress(pct: int, detail: str = "") -> None:
        core.jobs.update(job, progress=pct, detail=detail)

    return _on_progress


def _user_id(update: Update) -> Optional[int]:
    user = update.effective_user
    return user.id if user is not None else None


def _find_project(projects: Sequence[dict], name: str, host_id: Optional[str]) -> Optional[dict]:
    """按 (主机, 项目名) 定位项目；`host_id` 为空时只比名字（单机部署的唯一形态）。"""
    for project in projects:
        if str(project.get("name")) != name:
            continue
        if host_id and str(project.get("host") or "") != host_id:
            continue
        return project
    return None


async def _reject_unknown_host(core: Core, update: Update, host_id: Optional[str]) -> bool:
    """回调里的 host 必须在配置里（只认配置内的 id，绝不拿它拼命令）。

    返回 True 表示「不认识的 host，已经回复用户，调用方直接 return」。
    """
    if not host_id:
        return False
    state = _state(core)
    if state.host_by_id(host_id) is not None:
        return False
    log.warning("回调里的主机 id 不在配置中：%s", host_id)
    await core.panels.render(
        "docker",
        update,
        "⚠️ <b>未知主机</b>：<code>%s</code> 不在 <code>data/docker-hosts.json</code> 里，"
        "请重新 /d_list 打开面板。" % esc(str(host_id)),
        _back_keyboard(1),
    )
    return True


async def _project_of(
    core: Core, update: Update, name: str, host_id: Optional[str]
) -> Optional[dict]:
    """取项目；找不到就渲染提示并返回 None。"""
    state = _state(core)
    projects = await state.get_projects()
    target = _find_project(projects, name, host_id)
    if target is None:
        await core.panels.render(
            "docker",
            update,
            "❌ <b>未找到该项目</b>：<code>%s</code>（可能已被移除，请刷新）"
            % esc((host_id + "/" + name) if host_id else name),
            _back_keyboard(1),
        )
    return target


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
    """收尾键盘：「🔙 返回列表」（回到刚才那页）+ 一行跨模块入口。

    排不下（或只有一个模块）时只剩「🔙 返回列表」，面板自己会补 🏠 返回。
    """
    rows = [[InlineKeyboardButton("🔙 返回列表", callback_data=cb_simple("d", "page_turn", page))]]
    extra = next_actions_keyboard(core, user_id, "docker")
    if extra is not None:
        rows.extend(extra.inline_keyboard)
    return InlineKeyboardMarkup(rows)


#: 失败时抄进收尾面板的输出行数与单行长度上限
FAIL_TAIL_LINES = 3
FAIL_TAIL_CHARS = 120
#: 批量升级里最多抄几行失败输出（再多面板就成日志了）
BATCH_FAIL_LINES = 6


def _tail_lines(captured: Sequence[str], limit: int = FAIL_TAIL_LINES) -> list[str]:
    """取命令输出最后几行非空内容（每行截断），给失败面板用。"""
    lines: list[str] = []
    for chunk in captured:
        for raw in str(chunk).splitlines():
            text = raw.strip()
            if text:
                lines.append(text[:FAIL_TAIL_CHARS])
    return lines[-limit:]


def _failure_block(captured: Sequence[str], limit: int = FAIL_TAIL_LINES) -> str:
    """失败时把命令尾部输出抄进面板——批量跑起来失败的那条消息可能在上面好几条之外。"""
    lines = _tail_lines(captured, limit)
    if not lines:
        return ""
    return "🔻 <b>最后输出：</b>\n<code>%s</code>" % esc("\n".join(lines))


async def _busy(core: Core, update: Update) -> None:
    await _answer(update, _BUSY_TEXT, alert=True)


# ==================== 项目列表 ====================
async def _render_list(
    core: Core,
    update: Update,
    *,
    page: int = 1,
    force_refresh: bool = False,
    host: Optional[str] = None,
) -> None:
    """主面板：多主机时先选主机，再列那台的项目（单主机时不分层，文案与以前完全一致）。

    `host=None` = 多主机时的「选主机」页；`host="all"` = 全部主机的混合列表；
    其它值 = 指定主机（回调里只认配置内的 id）。
    """
    await _answer(update)
    state = _state(core)
    settings = state.settings
    projects = await state.get_projects(force_refresh=force_refresh)
    if await _reject_unknown_host(core, update, host if host != "all" else None):
        return
    target_host = state.host_by_id(host) if host and host != "all" else None

    # 编号永远是「全库连续」（与 /upgrade NN 同序），换主机查看不会改变编号
    all_ordered = state.order(projects)
    numbers = {(p.get("host"), p.get("name")): i + 1 for i, p in enumerate(all_ordered)}

    # ---------- 多主机：第一屏先选主机 ----------
    if state.multi_host and target_host is None and host is None:
        total = len(all_ordered)
        running = sum(1 for p in all_ordered if "running" in str(p.get("status", "")).lower())
        text = "📊 <b>共 %d 个项目</b>（🟢 %d 运行中），分布在 %d 台主机\n" % (
            total,
            running,
            len(state.hosts),
        )
        text += "🖥 <b>主机：</b>%s\n\n" % esc(
            " / ".join(
                "%s %d" % (h.label or h.id, sum(1 for p in all_ordered if p.get("host") == h.id))
                for h in state.hosts
            )
        )
        text += "请选择要管理的主机：\n"
        for hint in scan_hint(state, include_compose=False):
            text += hint + "\n"
        rows: list[list[InlineKeyboardButton]] = []
        for h in state.hosts:
            count = sum(1 for p in all_ordered if p.get("host") == h.id)
            mark = " ⚠️" if (h.error or (state.host_errors or {}).get(h.id)) else ""
            rows.append(
                [
                    InlineKeyboardButton(
                        "🖥 %s（%d 个项目）%s" % ((h.label or h.id)[:22], count, mark),
                        callback_data=cb_simple("d", "host_list", h.id),
                    )
                ]
            )
        rows.append(
            [InlineKeyboardButton("📚 全部主机（%d 个项目）" % total, callback_data=cb_simple("d", "page_turn", 1, "all"))]
        )
        rows.append(
            [
                InlineKeyboardButton("🧹 镜像清理菜单", callback_data=cb_simple("d", "prune_menu")),
                InlineKeyboardButton("🏠 返回", callback_data=nav_home()),
            ]
        )
        await core.panels.render("docker", update, text, InlineKeyboardMarkup(rows))
        return

    visible = all_ordered if target_host is None else [p for p in all_ordered if p.get("host") == target_host.id]
    total_projects = len(projects)
    running_cnt = sum(1 for p in projects if "running" in str(p.get("status", "")).lower())
    page_projects, page, total_pages = paginate_projects(visible, page, settings.page_size)

    text = "📊 <b>统计：</b>共 %d 个项目 | 🟢 %d 运行中 | 🟡 %d 停止\n" % (
        total_projects,
        running_cnt,
        total_projects - running_cnt,
    )
    if state.multi_host:
        if target_host is not None:
            text += "🖥 <b>主机：</b>%s（%d 个项目）\n" % (
                esc(target_host.display),
                len(visible),
            )
        else:
            text += "🖥 <b>主机：</b>%s\n" % esc(
                " / ".join(
                    "%s %d" % (h.id, sum(1 for p in all_ordered if p.get("host") == h.id))
                    for h in state.hosts
                )
            )
    text += "📖 <b>页码：</b>%d / %d\n" % (page, total_pages)
    if state.compose_bin is not None and not state.compose_bin:
        text += "⚠️ 未检测到 <code>docker compose</code> / <code>docker-compose</code> 命令\n"
    text += "\n"

    keyboard: list[list[InlineKeyboardButton]] = []

    def row_text(p: dict) -> str:
        """画一条项目（编号全局连续，与 `/upgrade NN` 同序），并把按钮挂进 keyboard。"""
        num = "%02d" % numbers.get((p.get("host"), p.get("name")), 0)
        name = str(p.get("name", ""))
        label = state.project_label(p)
        status = str(p.get("status", ""))
        status_icon = "🟢" if "running" in status.lower() else "🟡"
        disp_name = label[:26] + ".." if len(label) > 28 else label
        services = list(p.get("services") or [])
        services_str = ", ".join(services) if services else "-"

        out = "<b>%s.</b> %s %s <code>[%s]</code>\n" % (num, esc(label), status_icon, esc(status))
        if state.multi_host and target_host is None:
            out += "     主机：%s\n" % esc(str(p.get("host_label") or p.get("host") or ""))
        out += "     路径：<code>%s</code>\n" % esc(p.get("dir", ""))
        out += "     容器：%s\n\n" % esc(services_str)

        payload = {"name": name, "page": page, "host": p.get("host"), "list_host": host or ""}
        if len(services) > 1:
            data = cb("d", "p_sel", payload)
            keyboard.append(
                [InlineKeyboardButton("⚙️ %s. %s (多服务)" % (num, disp_name), callback_data=data)]
            )
        else:
            data = cb("d", "up_s_ask", payload)
            keyboard.append(
                [InlineKeyboardButton("🚀 %s. %s" % (num, disp_name), callback_data=data)]
            )
        return out

    hints = scan_hint(state, include_compose=False)

    if not visible:
        if target_host is not None:
            text += "⚠️ 这台主机上未检测到 Docker Compose 项目\n"
        else:
            text += "⚠️ 暂未检测到任何 Docker Compose 项目\n"
        for hint in hints:
            text += hint + "\n"
    elif target_host is not None or not state.multi_host:
        last_group: Optional[str] = None
        for p in page_projects:
            if not state.multi_host:
                is_running = "running" in str(p.get("status", "")).lower()
                group = "running" if is_running else "stopped"
                if group != last_group:
                    text += "🟢 <b>运行中</b>\n" if is_running else "🟡 <b>已停止</b>\n"
                    last_group = group
            text += row_text(p)
        if hints:
            text += "\n" + "\n".join(hints) + "\n"
    else:
        # 全部主机混合视图：每台主机都画一段（0 项目/故障也要看得见）
        page_by_host: dict[str, list[dict]] = {}
        for p in page_projects:
            page_by_host.setdefault(str(p.get("host") or ""), []).append(p)
        total_by_host: dict[str, int] = {}
        for p in all_ordered:
            key = str(p.get("host") or "")
            total_by_host[key] = total_by_host.get(key, 0) + 1

        drawn: set[str] = set()
        # 段顺序必须跟 order() 的排序键（host id）一致，否则编号会在同一页里来回跳
        for h in sorted(state.hosts, key=lambda item: str(item.id)):
            text += "🖥 <b>%s</b>\n" % esc(h.display)
            drawn.add(h.id)
            if h.error:
                text += "     ⚠️ %s\n\n" % esc(h.error)
                continue
            mine = page_by_host.get(h.id, [])
            if mine:
                for p in mine:
                    text += row_text(p)
                continue
            err = (state.host_errors or {}).get(h.id)
            if total_by_host.get(h.id):
                text += "     （这一页没有它的项目，翻页看看）\n\n"
            elif err:
                text += "     ⚠️ %s\n\n" % esc(err)
            else:
                text += "     （未检测到 Compose 项目）\n\n"
        for p in page_projects:
            if str(p.get("host") or "") not in drawn:
                text += row_text(p)
        if hints:
            text += "\n" + "\n".join(hints) + "\n"

    host_arg = host or ("all" if state.multi_host else None)

    def turn(n: int) -> str:
        return cb_simple("d", "page_turn", n, host_arg) if host_arg else cb_simple("d", "page_turn", n)

    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀ 上一页", callback_data=turn(page - 1)))
    nav.append(
        InlineKeyboardButton("📄 %d/%d" % (page, total_pages), callback_data=cb_simple("d", "noop"))
    )
    if page < total_pages:
        nav.append(InlineKeyboardButton("下一页 ▶", callback_data=turn(page + 1)))
    keyboard.append(nav)
    prune_data = (
        cb_simple("d", "prune_menu", target_host.id) if target_host is not None else cb_simple("d", "prune_menu")
    )
    upgrade_data = (
        cb_simple("d", "upgrade_all", target_host.id)
        if target_host is not None
        else cb_simple("d", "upgrade_all", "all")
    )
    keyboard.append(
        [
            InlineKeyboardButton("🧹 镜像清理菜单", callback_data=prune_data),
            InlineKeyboardButton(
                "⬆️ 升级全部项目" if target_host is None else "⬆️ 升级这台全部项目",
                callback_data=upgrade_data,
            ),
        ]
    )
    refresh_row = [
        InlineKeyboardButton(
            "🔄 刷新状态",
            callback_data=cb_simple("d", "refresh", page, host_arg) if host_arg else cb_simple("d", "refresh", page),
        )
    ]
    if state.multi_host:
        refresh_row.append(InlineKeyboardButton("🖥 换主机", callback_data=cb_simple("d", "page_turn", 1)))
    keyboard.append(refresh_row)

    await core.panels.render("docker", update, text, InlineKeyboardMarkup(keyboard))


async def _show_detail(
    core: Core, update: Update, project_name: str, back_page: int = 1, host: Optional[str] = None
) -> None:
    """项目卡片：选整项目升级还是单服务升级。"""
    await _answer(update)
    state = _state(core)
    if await _reject_unknown_host(core, update, host):
        return
    target = await _project_of(core, update, project_name, host)
    if target is None:
        return

    is_running = "running" in str(target.get("status", "")).lower()
    text = "📦 <b>项目卡片：%s</b>\n\n" % esc(state.project_label(target))
    if state.multi_host:
        text += "🖥 <b>主机：</b>%s\n" % esc(str(target.get("host_label") or target.get("host") or ""))
    text += "📂 <b>路径：</b><code>%s</code>\n" % esc(target["dir"])
    text += "%s <b>状态：</b>%s\n\n" % (
        "🟢" if is_running else "🟡",
        esc(target.get("status", "")),
    )
    text += "⚙️ <b>请选择操作控制范围：</b>\n"

    host_id = target.get("host")
    keyboard = [
        [
            InlineKeyboardButton(
                "⚡ 升级全部服务容器",
                callback_data=cb(
                    "d", "up_s_ask", {"name": project_name, "page": back_page, "host": host_id}
                ),
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
                        "d",
                        "up_svc_ask",
                        {"name": project_name, "svc": svc, "page": back_page, "host": host_id},
                    ),
                )
            ]
        )
    keyboard.append(
        [
            InlineKeyboardButton(
                "🔙 返回列表",
                callback_data=cb_simple("d", "page_turn", back_page, host_id)
                if state.multi_host
                else cb_simple("d", "page_turn", back_page),
            )
        ]
    )

    await core.panels.render("docker", update, text, InlineKeyboardMarkup(keyboard))


# ==================== 两步确认（panels.ask_confirm） ====================
async def _ask_project_upgrade(
    core: Core, update: Update, project_name: str, back_page: int = 1, host: Optional[str] = None
) -> None:
    """整项目升级确认（LDMG ask_single_upgrade）。"""
    await _answer(update)
    state = _state(core)
    if await _reject_unknown_host(core, update, host):
        return
    projects = await state.get_projects()
    target = _find_project(projects, project_name, host)

    host_id = target.get("host") if target else host
    safe_name = esc(state.project_label(target) if target else project_name)
    safe_dir = esc(target["dir"]) if target else "未知路径"
    # 不在这里探测 compose（同步 subprocess 会卡事件循环），用已缓存结果或默认展示
    compose = " ".join(state.get_remote_compose_bin(state.host_by_id(host_id)) or ["docker", "compose"])
    confirm_data = cb("d", "up_p_do", {"name": project_name, "page": back_page, "host": host_id})

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
    core: Core,
    update: Update,
    project_name: str,
    service_name: str,
    back_page: int = 1,
    host: Optional[str] = None,
) -> None:
    """单服务升级确认（LDMG ask_svc_upgrade）。"""
    await _answer(update)
    state = _state(core)
    if await _reject_unknown_host(core, update, host):
        return
    projects = await state.get_projects()
    target = _find_project(projects, project_name, host)

    host_id = target.get("host") if target else host
    safe_project = esc(state.project_label(target) if target else project_name)
    safe_service = esc(service_name)
    safe_dir = esc(target["dir"]) if target else "未知路径"
    confirm_data = cb(
        "d", "up_svc_do", {"name": project_name, "svc": service_name, "page": back_page, "host": host_id}
    )
    cancel_data = cb("d", "p_sel", {"name": project_name, "page": back_page, "host": host_id})

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


async def _ask_upgrade_all(core: Core, update: Update, host: Optional[str] = None) -> None:
    """批量升级的确认（LDMG upgrade_all）。给了 host 就只升那台主机。"""
    await _answer(update)
    state = _state(core)
    if await _reject_unknown_host(core, update, host if host != "all" else None):
        return
    target_host = state.host_by_id(host) if host and host != "all" else None
    projects = await state.get_projects()
    if target_host is not None:
        projects = [p for p in projects if p.get("host") == target_host.id]
        scope = "主机 <b>%s</b> 上的 %d 个" % (esc(target_host.display), len(projects))
    else:
        scope = "全部 %d 个" % len(projects)
    text = (
        "⚠️ <b>确认批量升级？</b>\n"
        "%s Compose 项目将依次执行 <code>pull</code> + <code>up -d</code>。\n"
        "过程可在 🧰 任务中心或进度消息里中断。" % scope
    )
    confirm_extra = [target_host.id] if target_host is not None else []
    cancel_extra = [target_host.id] if target_host is not None else []
    await core.panels.ask_confirm(
        "docker",
        update,
        text,
        cb_simple("d", "upgrade_all_confirm", *confirm_extra),
        cancel_data=cb_simple("d", "page_turn", 1, *cancel_extra)
        if cancel_extra
        else cb_simple("d", "page_turn", 1),
        confirm_label="🚀 确认升级全部" if target_host is None else "🚀 确认升级这台",
        cancel_label="❌ 取消",
    )


# ==================== 长任务：升级 ====================
async def _do_upgrade_project(
    core: Core,
    update: Update,
    context: Any,
    project_name: str,
    page: int = 1,
    host: Optional[str] = None,
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
    extra = ""

    try:
        await _answer(update)
        projects = await state.get_projects()
        target = _find_project(projects, project_name, host)

        if target is None:
            status, detail = FAILED, "找不到项目 %s（可能已删除或改名，/d_list 可刷新）" % project_name
        elif not state.get_remote_compose_bin(state.host_by_id(host)):
            status, detail = FAILED, "未检测到 docker compose / docker-compose 命令"
        else:
            upgrade_host = state.host_of(target)
            job.title = "升级项目 %s" % state.project_label(target)
            safe_name = esc(state.project_label(target))
            await core.panels.render(
                "docker",
                update,
                "⏳ <b>开始升级项目 [%s]</b>\n阶段 1/2：拉取新镜像" % safe_name,
                _progress_keyboard(task_id),
            )
            log.info("升级项目 [%s]", project_name)

            pull_out: list[str] = []
            up_out: list[str] = []
            pull_ok = await run_command_with_feedback(
                state,
                update.effective_message,
                state.build_compose_cmd(target, "pull"),
                cwd=state.cwd_for(target),
                title="拉取新镜像 - %s" % state.project_label(target),
                progress_pct=30,
                task_id=task_id,
                on_progress=_progress(core, job),
                delete_on_success=True,
                out=pull_out,
                host=upgrade_host,
            )
            up_ok = False
            if pull_ok and not state.cancel_requested:
                await core.panels.render(
                    "docker",
                    update,
                    "⏳ <b>升级项目 [%s]</b>\n阶段 2/2：重建与启动" % safe_name,
                    _progress_keyboard(task_id),
                )
                up_ok = await run_command_with_feedback(
                    state,
                    update.effective_message,
                    state.build_compose_cmd(target, "up", "-d"),
                    cwd=state.cwd_for(target),
                    title="重建与启动 - %s" % state.project_label(target),
                    progress_pct=80,
                    task_id=task_id,
                    on_progress=_progress(core, job),
                    delete_on_success=True,
                    out=up_out,
                    host=upgrade_host,
                )

            if state.cancel_requested:
                status, detail = CANCELLED, "已按用户请求取消"
            elif pull_ok and up_ok:
                status, detail = DONE, "项目整体升级完成"
            else:
                status, detail = FAILED, "拉取镜像或重建启动失败"
                extra = _failure_block(up_out if pull_ok else pull_out)
    except Exception as exc:
        log.exception("升级项目 %s 异常", project_name)
        status, detail = FAILED, "执行异常：%s" % exc
    finally:
        state.invalidate_cache()
        state.end_task()
        core.jobs.finish(job, status, detail)

    await core.panels.render(
        "docker", update, _done_text(job, extra), _finish_keyboard(core, _user_id(update), page)
    )


async def _do_upgrade_service(
    core: Core,
    update: Update,
    context: Any,
    project_name: str,
    service_name: str,
    page: int = 1,
    host: Optional[str] = None,
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
    extra = ""

    try:
        await _answer(update)
        projects = await state.get_projects()
        target = _find_project(projects, project_name, host)

        if target is None:
            status, detail = FAILED, "未找到项目 %s（/d_list 可刷新）" % project_name
        elif service_name not in list(target.get("services") or []):
            status, detail = FAILED, "项目 %s 中没有服务 %s" % (project_name, service_name)
        elif not state.get_remote_compose_bin(state.host_by_id(host)):
            status, detail = FAILED, "未检测到 docker compose / docker-compose 命令"
        else:
            upgrade_host = state.host_of(target)
            job.title = "升级服务 %s / %s" % (state.project_label(target), service_name)
            safe_project = esc(state.project_label(target))
            safe_service = esc(service_name)
            await core.panels.render(
                "docker",
                update,
                "⏳ <b>升级服务 [%s]（%s）</b>\n阶段 1/2：拉取服务镜像"
                % (safe_service, safe_project),
                _progress_keyboard(task_id),
            )
            log.info("升级服务 [%s -> %s]", project_name, service_name)

            pull_out: list[str] = []
            up_out: list[str] = []
            pull_ok = await run_command_with_feedback(
                state,
                update.effective_message,
                state.build_compose_cmd(target, "pull", service_name),
                cwd=state.cwd_for(target),
                title="拉取服务镜像 - %s" % service_name,
                progress_pct=30,
                task_id=task_id,
                on_progress=_progress(core, job),
                delete_on_success=True,
                out=pull_out,
                host=upgrade_host,
            )
            up_ok = False
            if pull_ok and not state.cancel_requested:
                await core.panels.render(
                    "docker",
                    update,
                    "⏳ <b>升级服务 [%s]（%s）</b>\n阶段 2/2：重建启动服务"
                    % (safe_service, safe_project),
                    _progress_keyboard(task_id),
                )
                up_ok = await run_command_with_feedback(
                    state,
                    update.effective_message,
                    state.build_compose_cmd(target, "up", "-d", service_name),
                    cwd=state.cwd_for(target),
                    title="重建启动服务 - %s" % service_name,
                    progress_pct=80,
                    task_id=task_id,
                    on_progress=_progress(core, job),
                    delete_on_success=True,
                    out=up_out,
                    host=upgrade_host,
                )

            if state.cancel_requested:
                status, detail = CANCELLED, "已按用户请求取消"
            elif pull_ok and up_ok:
                status, detail = DONE, "服务升级完成"
            else:
                status, detail = FAILED, "拉取镜像或重建启动失败"
                extra = _failure_block(up_out if pull_ok else pull_out)
    except Exception as exc:
        log.exception("升级服务 %s/%s 异常", project_name, service_name)
        status, detail = FAILED, "执行异常：%s" % exc
    finally:
        state.invalidate_cache()
        state.end_task()
        core.jobs.finish(job, status, detail)

    await core.panels.render(
        "docker", update, _done_text(job, extra), _finish_keyboard(core, _user_id(update), page)
    )


async def _do_upgrade_all(
    core: Core, update: Update, context: Any, host: Optional[str] = None
) -> None:
    """批量升级（LDMG do_upgrade_all）。给了 host 就只升那台主机。"""
    state = _state(core)
    if update.effective_message is None:
        await _answer(update, "⚠️ 当前会话不可用，请重新用 /d_list 打开面板")
        return
    if await _reject_unknown_host(core, update, host if host != "all" else None):
        return
    target_host = state.host_by_id(host) if host and host != "all" else None
    task_id = uuid.uuid4().hex
    if not await state.begin_task(task_id):
        await _busy(core, update)
        return

    job = core.jobs.add(
        "docker",
        (
            "批量升级 %s" % target_host.display
            if target_host is not None
            else ("批量升级全部项目" if not state.multi_host else "批量升级全部项目（%d 台主机）" % len(state.hosts))
        ),
        cancel=state.request_cancel,
        chat_id=_chat_id(update),
    )
    status, detail = FAILED, "未执行"
    extra = ""

    try:
        await _answer(update)
        all_projects = await state.get_projects()
        if target_host is not None:
            all_projects = [p for p in all_projects if p.get("host") == target_host.id]
        projects = state.order(all_projects)

        if not projects:
            status, detail = FAILED, "未检测到可升级的项目"
        elif not any(state.get_remote_compose_bin(h) for h in state.hosts):
            status, detail = FAILED, "未检测到 docker compose / docker-compose 命令"
        else:
            await core.panels.render(
                "docker",
                update,
                "⏳ <b>开始批量升级全部 %d 个项目…</b>" % len(projects),
                _progress_keyboard(task_id),
            )
            log.info("批量升级共 %d 个项目（%d 台主机）", len(projects), len(state.hosts))

            success_list: list[str] = []
            fail_list: list[str] = []
            fail_lines: list[str] = []
            interrupted = ""
            processed = 0

            for i, project in enumerate(projects, 1):
                if state.cancel_requested:
                    break
                processed = i
                pct = int((i / len(projects)) * 100)
                label = state.project_label(project)
                loop_host = state.host_of(project)
                pull_out: list[str] = []
                up_out: list[str] = []
                pull_ok = await run_command_with_feedback(
                    state,
                    update.effective_message,
                    state.build_compose_cmd(project, "pull"),
                    cwd=state.cwd_for(project),
                    title="批量拉取 - %s" % label,
                    progress_pct=pct,
                    task_id=task_id,
                    on_progress=_progress(core, job),
                    delete_on_success=True,
                    out=pull_out,
                    host=loop_host,
                )
                up_ok = False
                if pull_ok and not state.cancel_requested:
                    up_ok = await run_command_with_feedback(
                        state,
                        update.effective_message,
                        state.build_compose_cmd(project, "up", "-d"),
                        cwd=state.cwd_for(project),
                        title="批量启动 - %s" % label,
                        progress_pct=pct,
                        task_id=task_id,
                        on_progress=_progress(core, job),
                        delete_on_success=True,
                        out=up_out,
                        host=loop_host,
                    )
                if pull_ok and up_ok:
                    success_list.append(label)
                elif state.cancel_requested:
                    # 被中断的项目不算「失败」——它不是坏，是用户按了停
                    interrupted = label
                else:
                    fail_list.append(label)
                    if len(fail_lines) < BATCH_FAIL_LINES:
                        fail_lines.extend(_tail_lines(up_out if pull_ok else pull_out, 2))

            # 批量升级的结论写在卡片行里，这里只补「谁成谁败」的明细
            lines = [
                "✅ <b>成功 (%d)：</b> %s"
                % (len(success_list), esc(", ".join(success_list)) if success_list else "无"),
                "❌ <b>失败 (%d)：</b> %s"
                % (len(fail_list), esc(", ".join(fail_list)) if fail_list else "无"),
            ]
            if interrupted:
                lines.append("⚠️ <b>已中断：</b> %s" % esc(interrupted))
            extra = "\n".join(lines)
            if fail_lines:
                extra += "\n🔻 <b>最后输出：</b>\n<code>%s</code>" % esc(
                    "\n".join(fail_lines[:BATCH_FAIL_LINES])
                )
            if state.cancel_requested:
                # 取消可能发生在最后一个项目的命令里（循环顶部那次检查看不到），
                # 所以这里用实时的 cancel_requested 判定，而不是循环里那个 aborted 标记
                status = CANCELLED
                remaining = max(0, len(projects) - processed)
                if remaining:
                    detail = "已中止（剩余 %d 个项目）" % remaining
                else:
                    detail = "已按用户请求取消（最后一步被中断）"
            elif not success_list:
                status, detail = FAILED, "全部项目升级失败"
            elif fail_list:
                status, detail = DONE, "部分项目升级失败"
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
async def _show_prune_menu(core: Core, update: Update, host: Optional[str] = None) -> None:
    """镜像清理菜单（LDMG show_prune_menu）。

    **镜像清理是按主机执行的**，所以多主机时先选主机，再选清理范围。
    """
    await _answer(update)
    state = _state(core)
    if await _reject_unknown_host(core, update, host):
        return

    if state.multi_host and not host:
        text = (
            "🧹 <b>Docker 镜像清理中心</b>\n\n"
            "镜像清理**按主机执行**——请先选择要清理哪台主机：\n"
        )
        keyboard = [
            [
                InlineKeyboardButton(
                    "🧹 %s（%s）" % (item.id, item.display),
                    callback_data=cb_simple("d", "prune_menu", item.id),
                )
            ]
            for item in state.hosts
        ]
        keyboard.append(
            [InlineKeyboardButton("🔙 返回主菜单", callback_data=cb_simple("d", "page_turn", 1))]
        )
        await core.panels.render("docker", update, text, InlineKeyboardMarkup(keyboard))
        return

    target = state.host_by_id(host)
    where = ""
    if state.multi_host and target is not None:
        where = "\n🖥 <b>目标主机：</b>%s" % esc(target.display)
    text = (
        "🧹 <b>Docker 镜像清理中心</b>%s\n\n"
        "请选择清理类型：\n"
        "• <b>悬空镜像 (Dangling)</b>：无标签且未被使用的临时镜像层（安全推荐）\n"
        "• <b>所有未使用镜像 (All Unused)</b>：没有任何容器正在使用的全部旧镜像（深度清理）"
        % where
    )
    host_arg = target.id if (state.multi_host and target is not None) else None
    args = [host_arg] if host_arg else []
    keyboard = [
        [
            InlineKeyboardButton(
                "🍂 仅清理悬空镜像 (Dangling)",
                callback_data=cb_simple("d", "prune_req", "dangling", *args),
            )
        ],
        [
            InlineKeyboardButton(
                "🗑 清理所有未使用镜像 (All Unused)",
                callback_data=cb_simple("d", "prune_req", "all", *args),
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 返回主菜单",
                callback_data=cb_simple("d", "prune_menu", *args)
                if host_arg
                else cb_simple("d", "page_turn", 1),
            )
        ],
    ]
    await core.panels.render("docker", update, text, InlineKeyboardMarkup(keyboard))


async def _ask_prune_confirm(
    core: Core, update: Update, prune_all: bool, host: Optional[str] = None
) -> None:
    """扫描候选镜像 → 两步确认（LDMG ask_prune_confirm）。"""
    state = _state(core)
    await _answer(update)
    if await _reject_unknown_host(core, update, host):
        return
    target = state.host_by_id(host)
    label = "所有未使用" if prune_all else "悬空 (dangling)"
    where = "（%s）" % target.display if (state.multi_host and target is not None) else ""

    await core.panels.render(
        "docker", update, "🔍 正在扫描 %s 上的 <b>%s</b> 镜像..." % (esc(where or "系统"), label)
    )

    try:
        ok, dry_output, error_output = await scan_prune_candidates(state, prune_all, target)
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

    host_arg = target.id if (state.multi_host and target is not None) else None
    extra = [host_arg] if host_arg else []
    confirm_data = cb_simple("d", "prune_do", "all" if prune_all else "dangling", *extra)
    text = (
        "🧹 <b>当前可清理镜像快照 (范围: %s%s)：</b>\n"
        "<code>%s</code>\n\n"
        "确认时 Docker 会重新判断实际可清理范围。\n确认执行清理吗？"
        % (label, where, format_prune_snapshot(dry_output))
    )
    await core.panels.ask_confirm(
        "docker",
        update,
        text,
        confirm_data,
        cancel_data=cb_simple("d", "prune_menu", *extra)
        if extra
        else cb_simple("d", "prune_menu"),
        confirm_label="✅ 确认清理",
        cancel_label="❌ 取消",
    )


async def _do_prune(
    core: Core, update: Update, context: Any, prune_all: bool, host: Optional[str] = None
) -> None:
    """执行镜像清理（LDMG do_prune）。镜像清理按主机执行。"""
    state = _state(core)
    if update.effective_message is None:
        await _answer(update, "⚠️ 当前会话不可用，请重新用 /d_list 打开面板")
        return
    if await _reject_unknown_host(core, update, host):
        return
    target = state.host_by_id(host)
    task_id = uuid.uuid4().hex
    if not await state.begin_task(task_id):
        await _busy(core, update)
        return

    label = "所有未使用" if prune_all else "悬空"
    job = core.jobs.add(
        "docker",
        "清理%s镜像%s" % (label, "（%s）" % target.display if state.multi_host else ""),
        cancel=state.request_cancel,
        chat_id=_chat_id(update),
    )
    status, detail = FAILED, "未执行"
    extra = ""

    try:
        await _answer(update)
        await core.panels.render(
            "docker",
            update,
            "🗑 <b>正在执行%s镜像清理，请稍候...</b>" % label,
            _progress_keyboard(task_id),
        )
        cmd = ["docker", "image", "prune", "-f"]
        if prune_all:
            cmd.append("-a")
        cmd = target.command(cmd)

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
            host=target,
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
            status, detail = FAILED, "%s镜像清理失败" % label
            extra = _failure_block(captured)
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
    """`docker ps -a` 状态速览（LDMG cmd_status）：截断 + HTML 转义。

    多主机时**每台主机一段**；单主机时文案与以前完全一致。
    """
    await _answer(update)
    state = _state(core)
    await core.panels.render("docker", update, "🔍 正在拉取 Docker 容器状态速览...")

    sections: list[str] = []
    for host in state.hosts:
        if host.error:
            sections.append("🖥 <b>%s</b>\n⚠️ %s" % (esc(host.display), esc(host.error)))
            continue
        try:
            ok, output = await dump_container_status(state, host)
        except Exception as exc:
            log.exception("获取容器状态异常：host=%s", host.id)
            sections.append("🖥 <b>%s</b>\n❌ 获取状态失败: <code>%s</code>" % (esc(host.display), esc(str(exc))))
            continue
        if not ok:
            sections.append(
                "🖥 <b>%s</b>\n❌ %s"
                % (esc(host.display), esc(explain_exit(1, output[-600:], host)))
            )
            continue
        if not output:
            sections.append("🖥 <b>%s</b>\n⚠️ 未找到正在运行或已停止的容器。" % esc(host.display))
            continue
        sections.append("🖥 <b>%s</b>\n<code>%s</code>" % (esc(host.display), esc(output[-3000:])))

    if not state.multi_host:
        # 单主机：保持老文案（每台主机的标题/失败细节都省略）
        section = sections[0] if sections else ""
        body = section.split("\n", 1)[1] if "\n" in section else ""
        if body.startswith("❌"):
            await core.panels.render(
                "docker",
                update,
                "❌ 获取 Docker 状态失败: <code>%s</code>" % esc(body[1:].strip()[:1500]),
                _back_keyboard(),
            )
            return
        if body.startswith("⚠️"):
            await core.panels.render("docker", update, body, _back_keyboard())
            return
        await core.panels.render(
            "docker",
            update,
            "📊 <b>Docker 容器实时状态速览</b>\n\n%s" % body,
            _back_keyboard(),
        )
        return

    await core.panels.render(
        "docker",
        update,
        "📊 <b>Docker 容器实时状态速览</b>\n\n" + "\n\n".join(sections),
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
        await _render_list(
            core,
            update,
            page=_to_int(parts[2] if len(parts) > 2 else 1),
            host=parts[3] if len(parts) > 3 else None,
        )
    elif action == "host_list":
        await _render_list(
            core, update, page=1, host=parts[2] if len(parts) > 2 else None
        )
    elif action == "refresh":
        await _render_list(
            core,
            update,
            page=_to_int(parts[2] if len(parts) > 2 else 1),
            force_refresh=True,
            host=parts[3] if len(parts) > 3 else None,
        )
    elif action == "p_sel":
        await _show_detail(
            core, update, payload["name"], _to_int(payload.get("page"), 1), payload.get("host")
        )
    elif action == "up_s_ask":
        await _ask_project_upgrade(
            core, update, payload["name"], _to_int(payload.get("page"), 1), payload.get("host")
        )
    elif action == "up_svc_ask":
        await _ask_service_upgrade(
            core,
            update,
            payload["name"],
            payload["svc"],
            _to_int(payload.get("page"), 1),
            payload.get("host"),
        )
    elif action == "up_p_do":
        ok, why = core.panels.validate_confirm(query, data)
        if not ok:
            await _answer(update, why, alert=True)
            return
        await _do_upgrade_project(
            core,
            update,
            context,
            payload["name"],
            _to_int(payload.get("page"), 1),
            payload.get("host"),
        )
    elif action == "up_svc_do":
        ok, why = core.panels.validate_confirm(query, data)
        if not ok:
            await _answer(update, why, alert=True)
            return
        await _do_upgrade_service(
            core,
            update,
            context,
            payload["name"],
            payload["svc"],
            _to_int(payload.get("page"), 1),
            payload.get("host"),
        )
    elif action == "upgrade_all":
        await _ask_upgrade_all(core, update, parts[2] if len(parts) > 2 else None)
    elif action == "upgrade_all_confirm":
        ok, why = core.panels.validate_confirm(query, data)
        if not ok:
            await _answer(update, why, alert=True)
            return
        await _do_upgrade_all(core, update, context, parts[2] if len(parts) > 2 else None)
    elif action == "prune_menu":
        await _show_prune_menu(core, update, parts[2] if len(parts) > 2 else None)
    elif action == "prune_req":
        await _ask_prune_confirm(
            core,
            update,
            prune_all=(parts[2] if len(parts) > 2 else "dangling") == "all",
            host=parts[3] if len(parts) > 3 else None,
        )
    elif action == "prune_do":
        ok, why = core.panels.validate_confirm(query, data)
        if not ok:
            await _answer(update, why, alert=True)
            return
        await _do_prune(
            core,
            update,
            context,
            prune_all=(parts[2] if len(parts) > 2 else "dangling") == "all",
            host=parts[3] if len(parts) > 3 else None,
        )
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

    state = _state(core)
    projects = state.order(await state.get_projects())
    if not 1 <= num <= len(projects):
        await core.panels.render(
            "docker", update, "❌ 无效的项目序号（当前共 %d 个）" % len(projects), _back_keyboard()
        )
        return

    target = projects[num - 1]
    target_host = target.get("host")
    if service_name:
        if service_name not in list(target.get("services") or []):
            await core.panels.render(
                "docker",
                update,
                "❌ 项目 <b>%s</b> 中不存在服务 <code>%s</code>"
                % (esc(state.project_label(target)), esc(service_name)),
                _back_keyboard(),
            )
            return
        await _ask_service_upgrade(
            core, update, target["name"], service_name, 1, target_host
        )
    else:
        await _ask_project_upgrade(core, update, target["name"], 1, target_host)


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
    # 注意：这里必须复用已有的 state。以前直接 `DockerState(DockerSettings.from_env(...))`
    # 重建，会把 __init__.register() 里 make_state() 读进来的主机清单冲成「只有本机」——
    # 表现为「配了 docker-hosts.json 却处处只有本机」，而且启动日志还傻乎乎地打印了两台主机。
    _state(core)

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
