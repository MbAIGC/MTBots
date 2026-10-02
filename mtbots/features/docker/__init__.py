"""Docker 模块（← LDMG）：宿主 Compose 项目升级与镜像清理。

对外的唯一接口是 `MODULE: ModuleSpec`；核心逻辑在 `compose.py`（探测/扫描/执行/任务锁），
handler 在 `handlers.py`（面板 + 长任务）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ...core import Core, ModuleSpec
from pathlib import Path

from ...panels import cb_simple
from ...text import esc
from . import handlers
from .compose import DockerState, make_state
from .config import DockerSettings

log = logging.getLogger("mtbots.docker")

_ID = "docker"
_ICON = "🐳"
_TITLE = "Docker 管理"
_DESCRIPTION = "宿主 Compose 项目升级与镜像清理"

#: 命令片段：(命令名, 描述)——由 MenuManager 合并后统一下发，模块自己不碰 setMyCommands
_COMMANDS: list[tuple[str, str]] = [
    ("d_list", "项目列表"),
    ("d_status", "容器状态"),
    ("upgrade", "升级项目/服务"),
    ("prune", "镜像清理"),
]


def _state(core: Core) -> Optional[DockerState]:
    state = core.data.get(_ID)
    return state if isinstance(state, DockerState) else None


def commands(core: Core, uid: int) -> list[tuple[str, str]]:
    """本模块贡献给统一命令菜单的片段（与 uid 无关）。"""
    return list(_COMMANDS)


def home_entries(core: Core, user_id: int) -> list[tuple[str, str]]:
    """多主机时把每台主机直接摆到首页：`🐳 docker（本机 NAS）`。

    单主机时返回空列表 —— 首页按钮与老版本一字不差。
    """
    state = _state(core)
    if state is None or not state.multi_host:
        return []
    entries: list[tuple[str, str]] = []
    for host in state.hosts:
        label = (host.label or host.id)[:20]
        mark = "⚠️" if (host.error or (state.host_errors or {}).get(host.id)) else ""
        entries.append(
            ("🐳 docker（%s）%s" % (label, mark), cb_simple("d", "host_list", host.id))
        )
    return entries


async def summary(core: Core, user_id: int) -> str:
    """首页总览一行：只读缓存，绝不跑 docker / 不阻塞（首页必须秒开）。"""
    state = _state(core)
    if state is None or not state.has_scan():
        return "🐳 Docker · 点击进入"
    projects = state.cached_projects()
    if not projects:
        if state.multi_host:
            return "🐳 Docker · %d 台主机，暂无项目" % len(state.hosts)
        return "🐳 Docker · 暂无项目"
    running = sum(1 for p in projects if "running" in str(p.get("status", "")).lower())
    if state.multi_host:
        return "🐳 Docker · %d 台主机 %d 个项目（%d 个运行中）" % (
            len(state.hosts),
            len(projects),
            running,
        )
    return "🐳 Docker · %d 个项目（%d 个运行中）" % (len(projects), running)


def help_text(core: Core, user_id: int) -> str:
    """帮助章节（HTML）：LDMG 的帮助文案按合并后的命令名改写。"""
    state = _state(core)
    settings = state.settings if state is not None else DockerSettings.from_env(core.settings)
    return (
        "ℹ️ <b>Docker 管理（原 LDMG）</b>\n\n"
        "• <code>/d_list</code> / <code>/list</code> — 打开项目管理面板\n"
        "• <code>/d_status</code> / <code>/status</code> — 查看所有容器实时状态\n"
        "• <code>/prune</code> — 打开镜像清理菜单（两步确认）\n"
        "• <code>/upgrade</code> — 查看升级命令用法\n"
        "• <code>/upgrade 01</code> — 升级列表中第 01 个项目\n"
        "• <code>/upgrade 01 emby</code> — 升级第 01 个项目中的 emby 服务\n"
        "• <code>/upgrade all</code> — 升级全部项目\n\n"
        "⚙️ 单条命令超时 <code>COMMAND_TIMEOUT</code>=%d 秒；主面板每页 "
        "<code>PAGE_SIZE</code>=%d 个项目；同时只允许一个 compose 任务。"
        % (settings.command_timeout, settings.page_size)
    )


async def id_lines(core: Core, user_id: int) -> list[str]:
    """贡献给全局 /id 的模块信息（全部来自内存状态，不探测）。"""
    state = _state(core)
    if state is None:
        return ["🐳 docker：未初始化"]
    settings = state.settings
    compose = " ".join(state.compose_bin) if state.compose_bin else "未探测"
    lines = [
        "🐳 docker：compose=<code>%s</code>" % esc(compose),
        "🐳 docker：已缓存项目 %d 个｜超时 %ds｜每页 %d 个"
        % (len(state.cached_projects()), settings.command_timeout, settings.page_size),
    ]
    hosts_file = getattr(settings, "hosts_file", None)
    if hosts_file:
        exists = "存在" if Path(str(hosts_file)).is_file() else "不存在（= 只管理本机）"
        lines.append("🐳 docker：主机清单 <code>%s</code>（%s）" % (esc(str(hosts_file)), exists))
    if state.multi_host:
        per_host = "、".join(
            "%s %d" % (host.id, sum(1 for p in state.cached_projects() if p.get("host") == host.id))
            for host in state.hosts
        )
        lines.append("🐳 docker：主机 %d 台（%s）" % (len(state.hosts), esc(per_host)))
        for host_id, error in (state.host_errors or {}).items():
            lines.append("🐳 docker：主机 %s 异常 <code>%s</code>" % (esc(host_id), esc(error)))
    lines.append("🐳 docker：日志目录 <code>%s</code>" % esc(settings.log_dir))
    return lines


def register(app: Any, core: Core) -> None:
    """创建本模块的 DockerState 并注册 handler。"""
    state = make_state(DockerSettings.from_env(core.settings))
    core.data[_ID] = state

    # 命令菜单只贡献片段：MenuManager 合成后统一下发（模块自己不调 set_my_commands）
    menu = getattr(core, "menu", None)
    if menu is not None and hasattr(menu, "set_module_commands"):
        try:
            menu.set_module_commands(_ID, commands(core, 0))
        except Exception as exc:  # 菜单登记失败不影响功能
            log.warning("docker 命令片段登记失败：%s", exc)

    handlers.register(app, core)
    log.info(
        "docker 模块已注册：page_size=%d timeout=%ds cache_ttl=%ss 主机=%s",
        state.settings.page_size,
        state.settings.command_timeout,
        state.settings.projects_cache_ttl,
        ",".join(host.id for host in state.hosts),
    )


MODULE = ModuleSpec(
    id=_ID,
    icon=_ICON,
    title=_TITLE,
    description=_DESCRIPTION,
    callback_prefix="d",
    register=register,
    commands=commands,
    summary=summary,
    help_text=help_text,
    id_lines=id_lines,
    open_panel=handlers.open_panel,
    show_status=handlers.show_status,
    show_list=handlers.show_list,
    home_entries=home_entries,
    rescue=dict(handlers.RESCUE),
    startup=None,
)

__all__ = [
    "MODULE",
    "commands",
    "help_text",
    "id_lines",
    "register",
    "summary",
]
