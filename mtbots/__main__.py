"""入口：`python -m mtbots`。

    python -m mtbots              # 启动 Bot（长轮询）
    python -m mtbots --check      # 只做配置自检，不连 Telegram（适合部署前验证）
    python -m mtbots --health     # 额外探测 docker compose / LitePan / Cline 接口连通性
    python -m mtbots --version
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

from telegram import Update

from . import __version__
from .app import build_application, build_core, load_modules, modules_summary
from .config import TOKEN_VARS, Settings
from .logging_setup import install_excepthook, redact, setup_logging
from .text import strip_tags

log = logging.getLogger("mtbots.main")

USAGE = __doc__


def _parse_args(argv: list[str]) -> dict[str, bool]:
    flags = {"check": False, "health": False, "version": False, "list": False}
    for arg in argv:
        key = arg.lstrip("-").strip()
        if key in flags:
            flags[key] = True
        elif key in ("h", "help"):
            print(USAGE)
            raise SystemExit(0)
        else:
            print("未知参数：%s\n%s" % (arg, USAGE), file=sys.stderr)
            raise SystemExit(2)
    return flags


async def _health_probes(core) -> list[str]:
    """真实探测（会连网络的只有这一条路径，且只在 --health 下执行）。

    这里刻意用 getattr 链 + 逐步 try：模块内部 API 命名变化只会让某一行变成提示，
    不会让 `--health` 整个崩掉。
    """
    lines: list[str] = []
    settings = core.settings

    if core.has("docker"):
        try:
            from .features.docker import compose as docker_compose  # type: ignore
            from .features.docker.config import DockerSettings  # type: ignore

            builder = getattr(docker_compose, "make_state", None)
            state = (
                builder(DockerSettings.from_env(settings))
                if builder
                else docker_compose.DockerState(DockerSettings.from_env(settings))
            )
            hosts = list(getattr(state, "hosts", []) or [])
            remote = [h for h in hosts if getattr(h, "is_remote", False)]
            probe = getattr(state, "get_compose_bin", None)
            binary = await asyncio.to_thread(probe) if probe else []
            lines.append("🐳 docker compose（本机）：%s" % (" ".join(binary) if binary else "❌ 未找到"))
            lines.append(
                "🐳 主机：%d 台%s"
                % (len(hosts), "（含远端 %s）" % "、".join(h.id for h in remote) if remote else "（单机）")
            )
            projects = await state.get_projects() if hasattr(state, "get_projects") else None
            if projects is not None:
                lines.append("🐳 可扫描到的 compose 项目：%d 个" % len(projects))
                for host in hosts:
                    count = sum(1 for p in projects if p.get("host") == host.id)
                    lines.append("   · %s：%d 个" % (host.id, count))
                if not projects:
                    # 空列表最需要原因：权限不足 / 目录没挂载 / 命令缺失 / 远端连不上
                    hint_fn = getattr(docker_compose, "scan_hint", None)
                    for hint in hint_fn(state) if hint_fn else []:
                        lines.append("   " + redact(strip_tags(hint)))
        except Exception as exc:
            lines.append("🐳 docker compose：❌ %s" % redact(str(exc)))

    if core.has("litepan"):
        try:
            from .features.litepan import client as litepan_client  # type: ignore
            from .features.litepan import config as litepan_config  # type: ignore

            config_cls = getattr(litepan_config, "LitePanConfig", None) or getattr(litepan_config, "Config", None)
            if config_cls is None:
                lines.append("🎬 LitePan：⚠️ 未找到配置类，跳过探测")
            else:
                config = config_cls(settings)
                profiles = list(config.all_profiles()) if hasattr(config, "all_profiles") else []
                if not profiles:
                    lines.append("🎬 LitePan：⚠️ 未配置任何实例（users.json / LITEPAN_URL）")
                client_cls = getattr(litepan_client, "LitePanClient", None)
                for profile in profiles[:5]:
                    if client_cls is None:
                        lines.append("🎬 LitePan：⚠️ 未找到客户端类")
                        break
                    label = getattr(profile, "lite_url", "实例") if getattr(profile, "show_url", False) else "已配置实例"
                    try:
                        await asyncio.to_thread(client_cls(profile).health)
                        lines.append("🎬 LitePan %s：✅ 连接正常" % label)
                    except Exception as exc:
                        lines.append("🎬 LitePan %s：❌ %s" % (label, redact(str(exc))))
        except Exception as exc:
            lines.append("🎬 LitePan：❌ 探测失败 %s" % redact(str(exc)))

    if core.has("cline"):
        try:
            from .features.cline import core as cline_core  # type: ignore

            cline_settings = cline_core.Settings.from_env(global_settings=settings)
            store = cline_core.ConfigStore(cline_settings.config_file, cline_settings.max_keys_per_user)
            store.load()
            ok, detail = store.self_check()
            lines.append("🤖 Cline 存储：%s（%s）" % ("✅ 可读写" if ok else "❌ 不可写", redact(detail)))
            lines.append(
                "🤖 Cline API：%s（探测额度接口需要用户 Key，请用 /c_status 验证）" % cline_settings.api_base
            )
        except Exception as exc:
            lines.append("🤖 Cline：❌ %s" % redact(str(exc)))

    return lines


def main(argv: Optional[list[str]] = None) -> int:
    flags = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if flags["version"]:
        print("MTBots %s" % __version__)
        return 0

    settings = Settings.from_env()
    settings.ensure_dirs()
    setup_logging(settings.log_level, settings.log_dir, settings.bot_token, quiet_libs=not flags["check"])
    install_excepthook()

    core = load_modules(build_core(settings))

    if flags["list"]:
        for spec in core.modules.values():
            print("%s %s  id=%s  回调前缀=%s" % (spec.icon, spec.title, spec.id, spec.callback_prefix))
        return 0

    if flags["check"] or flags["health"]:
        print(settings.check_summary())
        print()
        print("📦 模块")
        print("\n".join(modules_summary(core)))
        if flags["health"]:
            print()
            print("🩺 连通性探测")
            print("\n".join(asyncio.run(_health_probes(core)) or ["（没有可探测的模块）"]))
        return 0 if not settings.problems() else 1

    problems = settings.problems()
    if problems:
        for issue in problems:
            log.critical("配置问题：%s", issue)
        log.critical(
            "启动中止。先运行 `python -m mtbots --check` 自检；Token 取 %s 任一环境变量。",
            " / ".join(TOKEN_VARS),
        )
        return 1

    log.info("MTBots v%s 启动中：模块=%s 白名单=%d Token=***%s", __version__, "、".join(settings.modules_enabled), len(settings.allowed_user_ids), settings.token_tail())
    application = build_application(settings, core)
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:  # pragma: no cover
        log.info("收到 Ctrl-C，退出。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
