"""feature 包装载器：按 `MTBOTS_MODULES` 顺序返回启用的 ModuleSpec。

懒导入是故意的：某个模块的依赖缺失（例如没有 pypinyin）或代码出错时，
只跳过它自己，另外两个模块照常启动。

`python -m mtbots --check` 的自检也挂在这里（`_attach_check`），这样三个 feature 包
不必各自知道 CLI 的存在，集成层也不去改它们的 `__init__.py`。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover
    from ..core import Core, ModuleSpec

log = logging.getLogger("mtbots.features")

#: 模块 id -> (包名, 属性名)
_LOADERS = {
    "docker": (".docker", "MODULE"),
    "litepan": (".litepan", "MODULE"),
    "cline": (".cline", "MODULE"),
}


# ==================== 静态自检（不联网） ====================
def _check_docker(core: "Core") -> list[str]:
    lines: list[str] = []
    try:
        from .docker.config import DockerSettings

        settings = DockerSettings.from_env(core.settings)
        lines.append("每页 %d 个项目｜单命令超时 %ds｜扫描缓存 %gs"
                     % (settings.page_size, settings.command_timeout, settings.projects_cache_ttl))
        lines.append("日志目录：%s" % settings.log_dir)
    except Exception as exc:
        lines.append("❌ 读取配置失败：%s" % exc)
    binary = shutil.which("docker")
    lines.append("docker CLI：%s" % (binary or "❌ 未找到（容器内请用自带 CLI 的镜像）"))
    if binary:
        lines.append("compose 插件：%s" % (
            "已安装" if shutil.which("docker-compose") or Path("/usr/local/lib/docker/cli-plugins/docker-compose").exists()
            else "⚠️ 未检测到（docker compose 子命令仍可用）"
        ))
    return lines


def _check_litepan(core: "Core") -> list[str]:
    lines: list[str] = []
    try:
        from .litepan.config import LitePanConfig

        config = LitePanConfig(core.settings)
    except Exception as exc:
        return ["❌ 读取配置失败：%s" % exc]

    profiles = list(config.all_profiles())
    users_file = Path(config.users_file)
    lines.append("用户配置文件：%s（%s）" % (
        users_file,
        "存在" if users_file.exists() else "不存在（将使用 .env 单用户模式）",
    ))
    try:
        if users_file.exists():
            lines.append("文件权限：%s（应为 600）" % oct(users_file.stat().st_mode & 0o777))
    except OSError:
        pass
    if not config.enabled:
        lines.append("⚠️ %s" % (config.error or "没有任何 LitePan 实例配置"))
        return lines
    receipt = sum(1 for p in profiles if getattr(p, "receipt_enabled", False))
    lines.append(
        "实例：%d 个｜回执/自动发现：%d 个开启｜规则菜单预算：%d 条"
        % (len(profiles), receipt, getattr(config, "menu_budget", 30))
    )
    for profile in profiles[:5]:
        chats = getattr(profile, "chat_ids", []) or []
        lines.append(
            "· %s → %s（%s）"
            % (
                "、".join(str(c) for c in chats) or "env 单用户",
                "已绑定实例" if not getattr(profile, "show_url", False) else getattr(profile, "lite_url", "-"),
                "回执开启" if getattr(profile, "receipt_enabled", False) else "回执关闭",
            )
        )
    return lines


def _check_cline(core: "Core") -> list[str]:
    lines: list[str] = []
    try:
        from .cline.core import ConfigStore, Settings as ClineSettings

        settings = ClineSettings.from_env(global_settings=core.settings)
        lines.append("API：%s%s" % (settings.api_base, settings.usage_path))
        lines.append("每用户最多 %d 个 Key｜/status 冷却 %gs" % (settings.max_keys_per_user, settings.status_cooldown))
        store = ConfigStore(settings.config_file, settings.max_keys_per_user)
        try:
            store.load()
        except Exception as exc:
            lines.append("⚠️ 读取存储失败（首次运行属正常）：%s" % exc)
        else:
            ok, detail = store.self_check()
            lines.append("存储：%s（%s）" % ("✅ 可读写" if ok else "❌ 不可写", detail))
        users = sum(1 for _ in getattr(store, "all_users", lambda: [])()) if hasattr(store, "all_users") else None
        if users is not None:
            lines.append("已绑定用户数：%d" % users)
    except Exception as exc:
        lines.append("❌ 读取配置失败：%s" % exc)
    if core.settings.demo_mode:
        lines.append("⚠️ DEMO_MODE=1：额度面板使用示例数字，不会请求接口")
    return lines


_CHECKS: dict[str, Callable[["Core"], list[str]]] = {
    "docker": _check_docker,
    "litepan": _check_litepan,
    "cline": _check_cline,
}


def _attach_check(module_id: str, spec: "ModuleSpec") -> "ModuleSpec":
    check = _CHECKS.get(module_id)
    if check is None or getattr(spec, "check", None) is not None:
        return spec
    try:
        return replace(spec, check=check)
    except Exception:  # ModuleSpec 不是 dataclass 时的兜底
        return spec


# ==================== 装载 ====================
def load_modules(core: "Core") -> list["ModuleSpec"]:
    import importlib

    specs: list["ModuleSpec"] = []
    for module_id in core.settings.modules_enabled:
        target = _LOADERS.get(module_id)
        if target is None:
            log.warning("未知模块 %r，已跳过", module_id)
            continue
        package, attr = target
        try:
            mod = importlib.import_module(package, __package__)
            spec = getattr(mod, attr)
        except Exception:
            log.exception("模块 %s 装载失败，已跳过（其它模块继续）", module_id)
            continue
        specs.append(_attach_check(module_id, spec))
    return specs


__all__ = ["load_modules"]
