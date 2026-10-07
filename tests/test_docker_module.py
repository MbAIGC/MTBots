"""docker 模块测试：stdlib unittest，无网络、无真实 docker、无 Telegram 请求。

只覆盖纯逻辑与接线：排序/分页、回调数据往返、pull 噪音过滤与 prune 候选挑选、
DockerState 隔离/缓存/任务锁、DockerSettings.from_env、ModuleSpec 与 register 的 handler。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from mtbots.config import Settings
from mtbots.features.docker import MODULE, commands, help_text, id_lines, refresh, register, summary
from mtbots.features.docker import handlers as docker_handlers
from mtbots.features.docker.compose import (
    DockerState,
    dump_container_status,
    common_mount_root,
    delete_message_quietly,
    filter_pull_noise,
    format_prune_snapshot,
    is_pull_noise,
    normalize_image_id,
    paginate_projects,
    run_command_with_feedback,
    scan_hint,
    select_unused_images,
    socket_group_hint,
    sort_projects_for_display,
)
from mtbots.features.docker.config import DockerSettings
from mtbots.features.docker.hosts import (
    DockerHost,
    explain_exit,
    load_hosts,
)
from mtbots.panels import _CB_PAYLOAD, cb, cb_args, cb_parse, cb_parts, cb_simple

# 测试里不需要 docker 模块的 warning（例如「未检测到 compose」）
logging.getLogger("mtbots.docker").addHandler(logging.NullHandler())
logging.getLogger("mtbots.docker").propagate = False


# ==================== fixture ====================
def _project(name: str, status: str = "running (1)", services=None, work_dir=None) -> dict:
    d = work_dir if work_dir is not None else "/srv/%s" % name
    return {
        "name": name,
        "dir": d,
        "status": status,
        "services": list(services) if services else ["web"],
        "config_files": ["%s/docker-compose.yml" % d],
    }


class _FakeMenu:
    def __init__(self) -> None:
        self.entries: dict[str, list[tuple[str, str]]] = {}

    def set_module_commands(self, module_id, entries, *, scope_chats=None) -> None:
        self.entries[module_id] = list(entries)


class _FakeCore:
    """只实现 docker 模块会用到的 Core 表面。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings(page_size=4)
        self.data: dict = {}
        self.menu = _FakeMenu()
        self.modules: dict = {}

    def has(self, module_id: str) -> bool:
        return module_id in self.modules


# ==================== 排序 / 分页 ====================
class SortPaginateTest(unittest.TestCase):
    def test_running_first_then_name(self) -> None:
        items = [
            _project("zeta", "exited (0)"),
            _project("alpha", "exited (0)"),
            _project("beta", "running (2)"),
        ]
        ordered = sort_projects_for_display(items)
        self.assertEqual([p["name"] for p in ordered], ["beta", "alpha", "zeta"])
        # /upgrade 01 必须命中面板里的 01
        self.assertEqual(ordered[0]["name"], "beta")
        self.assertEqual(ordered[1]["name"], "alpha")

    def test_paginate_clamps_and_slices(self) -> None:
        items = [{"name": str(i)} for i in range(7)]

        page_items, page, total = paginate_projects(items, 1, 3)
        self.assertEqual(([p["name"] for p in page_items], page, total), (["0", "1", "2"], 1, 3))

        page_items, page, total = paginate_projects(items, 99, 3)
        self.assertEqual(([p["name"] for p in page_items], page, total), (["6"], 3, 3))

        page_items, page, total = paginate_projects(items, "bad", 3)
        self.assertEqual((len(page_items), page, total), (3, 1, 3))

        self.assertEqual(paginate_projects([], 1, 3), ([], 1, 1))

    def test_upgrade_number_matches_sorted_index(self) -> None:
        projects = [_project("b", "exited"), _project("a", "running")]
        ordered = sort_projects_for_display(projects)
        self.assertEqual(ordered[0]["name"], "a")  # /upgrade 01
        self.assertEqual(ordered[1]["name"], "b")  # /upgrade 02


# ==================== 回调数据 ====================
class CallbackDataTest(unittest.TestCase):
    def test_payload_roundtrip(self) -> None:
        data = cb("d", "p_sel", {"name": "emby", "page": 2})
        self.assertTrue(data.startswith("d|p_sel|"))
        self.assertLess(len(data.encode("utf-8")), 64)
        self.assertEqual(cb_args(data), ("d", "p_sel"))

        prefix, action, payload = cb_parse(data)
        self.assertEqual((prefix, action), ("d", "p_sel"))
        self.assertEqual(payload, {"name": "emby", "page": 2})

    def test_simple_three_part_parses(self) -> None:
        data = cb_simple("d", "page_turn", 2)
        self.assertEqual(data, "d|page_turn|2")
        self.assertEqual(cb_args(data), ("d", "page_turn"))
        self.assertEqual(cb_parts(data), ["d", "page_turn", "2"])
        self.assertIsNone(cb_parse(data)[2])

    def test_confirm_and_cancel_data_shapes(self) -> None:
        self.assertEqual(
            cb_parse(cb_simple("d", "upgrade_all_confirm"))[:2], ("d", "upgrade_all_confirm")
        )
        self.assertEqual(cb_simple("d", "prune_req", "all"), "d|prune_req|all")
        self.assertTrue(cb("d", "task_cancel", {"task_id": "abc"}).startswith("d|task_cancel|"))
        self.assertTrue(cb("d", "up_svc_do", {"name": "x", "svc": "y"}).startswith("d|up_svc_do|"))

    def test_expired_payload_is_visible_as_none(self) -> None:
        data = cb("d", "p_sel", {"name": "gone"})
        _CB_PAYLOAD.pop(data, None)  # 模拟内存表被清理 / Bot 重启
        self.assertIsNone(cb_parse(data)[2])


# ==================== pull 噪音过滤 ====================
class PullNoiseTest(unittest.TestCase):
    NOISE = [
        "1a2b3c4d5e6f Downloading [====>    ]  1.2MB/3.4MB",
        "aabbccdd Download complete",
        "deadbeef1234 Extracting [==>   ]  12s",
        "00112233445566 Verifying Checksum",
        "abcdef12 Waiting",
    ]
    KEEP = [
        "Pulling emby ...",
        "emby Pulled",
        "Container emby  Started",
        "Successfully built 0f0f0f",
        "WARNING: some docker warning",
    ]

    def test_noise_detected(self) -> None:
        for line in self.NOISE:
            self.assertTrue(is_pull_noise(line), line)

    def test_real_lines_kept(self) -> None:
        for line in self.KEEP:
            self.assertFalse(is_pull_noise(line), line)

    def test_filter_keeps_order_and_drops_noise(self) -> None:
        lines = ["Pulling x", self.NOISE[0], "x Pulled", self.NOISE[1], "done"]
        self.assertEqual(filter_pull_noise(lines), ["Pulling x", "x Pulled", "done"])


# ==================== prune 候选 ====================
class PruneCandidateTest(unittest.TestCase):
    def test_select_unused_images_skips_referenced_and_duplicates(self) -> None:
        lines = [
            "sha256:aaa111\tnginx:latest\t120MB",
            "bbb222\t<none>:<none>\t12MB",
            "bbb222\t<none>:<none>\t12MB",  # 同 ID 去重
            "no-tabs-here",
        ]
        out = select_unused_images(lines, {"sha256:aaa111"})
        self.assertEqual(out, ["bbb222\t<none>:<none>\t12MB"])

    def test_referenced_short_id_is_normalized(self) -> None:
        self.assertEqual(normalize_image_id("abc123"), "sha256:abc123")
        self.assertEqual(normalize_image_id("sha256:abc123"), "sha256:abc123")
        self.assertEqual(normalize_image_id(""), "")

    def test_format_prune_snapshot_escapes_tail(self) -> None:
        out = format_prune_snapshot("a" * 2999 + "<b>", limit=3000)
        self.assertGreaterEqual(len(out), 3000)  # 先截断后转义，实体化可能略长
        self.assertTrue(out.endswith("&lt;b&gt;"))
        self.assertEqual(format_prune_snapshot("<script>x</script>"), "&lt;script&gt;x&lt;/script&gt;")

    def test_format_prune_snapshot_truncates_from_tail(self) -> None:
        out = format_prune_snapshot("HEAD" + "x" * 4000, limit=3000)
        self.assertEqual(len(out), 3000)
        self.assertNotIn("HEAD", out)


# ==================== DockerState ====================
class DockerStateTest(unittest.TestCase):
    def test_two_states_are_independent(self) -> None:
        a = DockerState(DockerSettings())
        b = DockerState(DockerSettings())
        a.compose_bin = ["docker", "compose"]
        a.projects_cache = [{"name": "x"}]
        a.projects_cache_time = time.monotonic()
        a.current_task = "t1"
        a.cancel_requested = True

        self.assertIsNone(b.compose_bin)
        self.assertEqual(b.cached_projects(), [])
        self.assertIsNone(b.current_task)
        self.assertFalse(b.cancel_requested)
        self.assertFalse(b.has_scan())
        self.assertIsNot(a.get_lock(), b.get_lock())

    def test_build_compose_cmd_keeps_all_config_files(self) -> None:
        state = DockerState(DockerSettings())
        state.compose_bin = ["docker", "compose"]
        project = {"config_files": ["/srv/a/docker-compose.yml", "/srv/a/override.yml"]}
        self.assertEqual(
            state.build_compose_cmd(project, "pull"),
            [
                "docker",
                "compose",
                "-f",
                "/srv/a/docker-compose.yml",
                "-f",
                "/srv/a/override.yml",
                "pull",
            ],
        )

        state.compose_bin = []
        with mock.patch.object(state, "get_compose_bin", return_value=[]):
            with self.assertRaises(RuntimeError):
                state.build_compose_cmd(project, "up", "-d")

    def test_stop_command_shape(self) -> None:
        """停止：本地是 `compose -f … stop [服务]`，远端被包成 ssh —— 守卫脚本就看这一串。"""
        state = DockerState(DockerSettings())
        state.compose_bin = ["docker", "compose"]
        project = {"config_files": ["/srv/a/docker-compose.yml"]}
        self.assertEqual(
            state.build_compose_cmd(project, "stop"),
            ["docker", "compose", "-f", "/srv/a/docker-compose.yml", "stop"],
        )
        self.assertEqual(
            state.build_compose_cmd(project, "stop", "emby"),
            ["docker", "compose", "-f", "/srv/a/docker-compose.yml", "stop", "emby"],
        )

        remote = DockerHost(id="vps", kind="ssh", target="mtbots@10.0.0.5")
        state.hosts = [DockerHost(id="local", kind="local"), remote]
        state.remote_compose["vps"] = ["docker", "compose"]
        wrapped = state.build_compose_cmd({**project, "host": "vps"}, "stop", "emby")
        self.assertEqual(wrapped[0], "ssh")
        self.assertEqual(
            wrapped[-1], "docker compose -f /srv/a/docker-compose.yml stop emby"
        )

    def test_compose_probe_prefers_plugin_then_fallback(self) -> None:
        ok = mock.Mock(returncode=0, stdout="", stderr="")
        bad = mock.Mock(returncode=1, stdout="", stderr="")

        state = DockerState(DockerSettings())
        with mock.patch(
            "mtbots.features.docker.compose.subprocess.run", return_value=ok
        ) as run:
            self.assertEqual(state.get_compose_bin(), ["docker", "compose"])
            self.assertEqual(run.call_args[0][0], ["docker", "compose", "version"])

        fallback = DockerState(DockerSettings())
        with mock.patch(
            "mtbots.features.docker.compose.subprocess.run", side_effect=[bad, ok]
        ):
            self.assertEqual(fallback.get_compose_bin(), ["docker-compose"])

        missing = DockerState(DockerSettings())
        with mock.patch(
            "mtbots.features.docker.compose.subprocess.run", side_effect=OSError("no docker")
        ):
            self.assertEqual(missing.get_compose_bin(), [])
            self.assertEqual(missing.get_compose_bin(), [])  # 失败不缓存，允许稍后重试

    def test_projects_cache_ttl_and_force_refresh(self) -> None:
        state = DockerState(DockerSettings(projects_cache_ttl=15.0))
        calls: list[int] = []

        def scanner() -> list[dict]:
            calls.append(1)
            return [_project("a", "running")]

        state.scan_hook = scanner
        self.assertEqual(asyncio.run(state.get_projects())[0]["name"], "a")
        asyncio.run(state.get_projects())  # TTL 内命中缓存
        self.assertEqual(len(calls), 1)

        # 「🔄 强制刷新」必须真扫：用户点刷新就是要新数据，不许悄悄给旧缓存
        asyncio.run(state.get_projects(force_refresh=True))
        self.assertEqual(len(calls), 2)
        self.assertTrue(state.has_scan())

        state.invalidate_cache()
        self.assertFalse(state.has_scan())
        self.assertEqual(state.cached_projects()[0]["name"], "a")  # 失效只动时间戳
        self.assertEqual(state.services_cache, {})  # 服务列表缓存一并作废

    def test_concurrent_refreshes_share_one_scan(self) -> None:
        """两个会话同时点 🔄：等锁的那个复用先到的结果，只跑一次全量扫描。"""
        import threading

        state = DockerState(DockerSettings(projects_cache_ttl=15.0))
        calls: list[int] = []
        gate = threading.Event()

        def scanner() -> list[dict]:
            calls.append(1)
            gate.wait(3)  # 卡住第一个扫描，让第二个确实排在锁上
            return [_project("a", "running")]

        state.scan_hook = scanner

        async def scenario() -> None:
            first = asyncio.ensure_future(state.get_projects(force_refresh=True))
            await asyncio.sleep(0.05)
            second = asyncio.ensure_future(state.get_projects(force_refresh=True))
            await asyncio.sleep(0.05)
            gate.set()
            await asyncio.gather(first, second)

        asyncio.run(scenario())
        self.assertEqual(len(calls), 1, "在飞的扫描要被合并，不能两个会话各扫一遍")

    def test_zero_ttl_always_rescans(self) -> None:
        state = DockerState(DockerSettings(projects_cache_ttl=0.0))
        calls: list[int] = []

        def scanner() -> list[dict]:
            calls.append(1)
            return [_project("a")]

        state.scan_hook = scanner
        asyncio.run(state.get_projects())
        asyncio.run(state.get_projects())
        self.assertEqual(len(calls), 2)

    def test_begin_task_is_exclusive(self) -> None:
        state = DockerState(DockerSettings())

        async def scenario() -> None:
            self.assertTrue(await state.begin_task("t1"))
            self.assertEqual(state.current_task, "t1")
            self.assertFalse(await state.begin_task("t2"))  # 第二次：已有任务在跑
            state.end_task()
            self.assertIsNone(state.current_task)
            self.assertTrue(await state.begin_task("t3"))
            state.end_task()

        asyncio.run(scenario())

    def test_request_cancel_validates_task_id(self) -> None:
        state = DockerState(DockerSettings())
        state.current_task = "t1"
        self.assertFalse(state.request_cancel("other"))
        self.assertFalse(state.cancel_requested)
        self.assertTrue(state.request_cancel("t1"))
        self.assertTrue(state.cancel_requested)
        state.end_task()
        self.assertFalse(state.cancel_requested)

        fresh = DockerState(DockerSettings())
        self.assertFalse(fresh.request_cancel("t1"))  # 没有任何任务在跑
        self.assertFalse(fresh.cancel_requested)


# ==================== DockerSettings ====================
class DockerSettingsTest(unittest.TestCase):
    def test_defaults(self) -> None:
        s = DockerSettings.from_env(Settings(), env={})
        self.assertEqual(s.page_size, 6)
        self.assertEqual(s.command_timeout, 300)
        self.assertEqual(s.projects_cache_ttl, 15.0)
        self.assertEqual(s.log_dir, Path("data/logs"))

    def test_env_and_global_settings_override(self) -> None:
        settings = Settings(page_size=9, log_dir=Path("/tmp/mtbots-logs"))
        s = DockerSettings.from_env(
            settings, env={"COMMAND_TIMEOUT": "42", "PROJECTS_CACHE_TTL": "7"}
        )
        self.assertEqual(s.page_size, 9)
        self.assertEqual(s.command_timeout, 42)
        self.assertEqual(s.projects_cache_ttl, 7.0)
        self.assertEqual(s.log_dir, Path("/tmp/mtbots-logs"))

    def test_invalid_values_fall_back(self) -> None:
        s = DockerSettings.from_env(
            Settings(page_size=0),
            env={"COMMAND_TIMEOUT": "abc", "PROJECTS_CACHE_TTL": "-3"},
        )
        self.assertEqual(s.page_size, 6)
        self.assertEqual(s.command_timeout, 300)
        self.assertEqual(s.projects_cache_ttl, 0.0)


# ==================== ModuleSpec / register ====================
class ModuleSpecTest(unittest.TestCase):
    def test_spec_fields(self) -> None:
        core = _FakeCore()
        m = MODULE
        self.assertEqual(m.id, "docker")
        self.assertEqual(m.icon, "🐳")
        self.assertEqual(m.title, "Docker 管理")
        self.assertEqual(m.callback_prefix, "d")
        self.assertEqual(m.description, "宿主 Compose 项目升级与镜像清理")
        self.assertIsNone(m.startup)

        self.assertEqual(
            [name for name, _desc in m.commands(core, 1)],
            ["d_list", "d_status", "upgrade", "prune"],
        )
        self.assertEqual(commands(core, 1), m.commands(core, 1))

        help_body = m.help_text(core, 1)
        self.assertIn("/upgrade 01", help_body)
        self.assertIn("/prune", help_body)
        self.assertIn("COMMAND_TIMEOUT", help_body)

        self.assertTrue(callable(m.open_panel))
        self.assertTrue(callable(m.show_status))
        self.assertTrue(callable(m.show_list))
        self.assertIsNotNone(m.id_lines)
        self.assertIsNotNone(m.summary)
        for key in ("upgrade", "prune", "d_list", "d_status"):
            self.assertIn(key, m.rescue)

    def test_register_adds_exactly_own_handlers(self) -> None:
        core = _FakeCore()
        app = Application.builder().token("123:abc").build()
        register(app, core)

        found_commands: set[str] = set()
        patterns = []
        for group in app.handlers.values():
            for handler in group:
                if isinstance(handler, CommandHandler):
                    found_commands |= set(handler.commands)
                elif isinstance(handler, CallbackQueryHandler):
                    patterns.append(handler.pattern)

        self.assertEqual(found_commands, {"upgrade", "prune", "d_list", "d_status"})
        banned = {"start", "help", "status", "list", "menu", "home", "cancel", "jobs", "id"}
        self.assertFalse(found_commands & banned)

        self.assertEqual(len(patterns), 1)
        self.assertTrue(patterns[0].match("d|page_turn|1"))
        self.assertTrue(patterns[0].match("d|p_sel|deadbeef"))
        self.assertFalse(patterns[0].match("nav|home"))

        self.assertIsInstance(core.data["docker"], DockerState)
        self.assertEqual(
            core.menu.entries["docker"],
            [
                ("d_list", "项目列表"),
                ("d_status", "容器状态"),
                ("upgrade", "升级项目/服务"),
                ("prune", "镜像清理"),
            ],
        )

    def test_register_skips_commands_owned_by_router(self) -> None:
        """合并后 router 先注册 /d_list /d_status，模块不得重复注册同一条命令。"""
        async def _noop(update, context):  # pragma: no cover - 只用于占位注册
            return None

        core = _FakeCore()
        app = Application.builder().token("123:abc").build()
        app.add_handler(CommandHandler("d_list", _noop))
        app.add_handler(CommandHandler("d_status", _noop))
        register(app, core)

        counts: dict[str, int] = {}
        for group in app.handlers.values():
            for handler in group:
                for cmd in getattr(handler, "commands", None) or ():
                    counts[cmd] = counts.get(cmd, 0) + 1
        for name in ("upgrade", "prune", "d_list", "d_status"):
            self.assertEqual(counts.get(name), 1, name)

    def test_summary_reads_cache_only(self) -> None:
        core = _FakeCore()
        state = DockerState(DockerSettings())
        core.data["docker"] = state
        # 一旦 summary 触发扫描就炸——保证它是纯缓存读取
        state.scan_hook = lambda: (_ for _ in ()).throw(AssertionError("summary 不该跑 docker"))

        self.assertEqual(asyncio.run(summary(core, 1)), "🐳 Docker · 点击进入")

        state.projects_cache = [
            _project("a", "running"),
            _project("b", "running (2)"),
            _project("c", "exited"),
        ]
        state.projects_cache_time = time.monotonic()
        self.assertEqual(asyncio.run(summary(core, 1)), "🐳 Docker · 3 个项目（2 个运行中）")

        state.projects_cache = []
        self.assertEqual(asyncio.run(summary(core, 1)), "🐳 Docker · 暂无项目")

    def test_summary_shows_per_host_counts(self) -> None:
        """多主机：每台主机摊开计数（NAS（15）、VPS（10）），异常主机挂 ⚠️。"""
        from mtbots.features.docker import refresh as docker_refresh
        from mtbots.features.docker.hosts import DockerHost

        core = _FakeCore()
        state = DockerState(DockerSettings())
        state.hosts = [
            DockerHost(id="nas", label="NAS", kind="local"),
            DockerHost(id="vps", label="VPS", kind="ssh", target="root@10.0.0.5"),
        ]
        core.data["docker"] = state
        state.scan_hook = lambda: (_ for _ in ()).throw(AssertionError("summary 不该跑 docker"))

        self.assertEqual(asyncio.run(summary(core, 1)), "🐳 Docker · 点击进入")

        state.projects_cache = [
            {**_project("a"), "host": "nas"},
            {**_project("b"), "host": "nas"},
            {**_project("c"), "host": "vps"},
        ]
        state.projects_cache_time = time.monotonic()
        self.assertEqual(
            asyncio.run(summary(core, 1)), "🐳 Docker · 2 台主机，NAS（2）、VPS（1）"
        )

        state.host_errors = {"vps": "SSH 连不上或认证失败"}
        self.assertEqual(
            asyncio.run(summary(core, 1)), "🐳 Docker · 2 台主机，NAS（2）、VPS（1 ⚠️）"
        )

        # 两台都空且都没有错误时才说「暂无项目」
        state.projects_cache = []
        state.host_errors = {}
        self.assertEqual(asyncio.run(summary(core, 1)), "🐳 Docker · 2 台主机，暂无项目")

        # refresh 钩子走 state.get_projects（会真扫），用 scan_hook 顶掉
        state.scan_hook = lambda: [{"name": "a", "dir": "/srv/a", "host": "nas"}]
        state.projects_cache_time = 0.0  # 上面的缓存是手工塞的，别落进「刚扫过」的合并窗口
        asyncio.run(docker_refresh(core, 1))
        self.assertEqual(len(state.cached_projects()), 1)

    def test_refresh_forces_scan_when_asked(self) -> None:
        from mtbots.features.docker import refresh as docker_refresh

        core = _FakeCore()
        state = DockerState(DockerSettings(projects_cache_ttl=9999.0))
        core.data["docker"] = state
        calls: list[int] = []

        def scanner() -> list[dict]:
            calls.append(1)
            return [_project("a", "running")]

        state.scan_hook = scanner
        asyncio.run(docker_refresh(core, 1))
        self.assertEqual(len(calls), 1)
        asyncio.run(docker_refresh(core, 1))  # TTL 内
        self.assertEqual(len(calls), 1)
        asyncio.run(docker_refresh(core, 1, True))  # 🔄 强制：无视 TTL
        self.assertEqual(len(calls), 2)

    def test_id_lines_are_cheap(self) -> None:
        core = _FakeCore()
        core.data["docker"] = DockerState(DockerSettings())
        lines = asyncio.run(id_lines(core, 1))
        self.assertTrue(any("compose=" in line for line in lines))
        self.assertTrue(any("日志目录" in line for line in lines))
        self.assertTrue(help_text(core, 1).startswith("ℹ️"))


# ==================== prune 扫描（假 docker 输出） ====================
class PruneScanTest(unittest.TestCase):
    def test_dangling_scan_returns_snapshot(self) -> None:
        from mtbots.features.docker.compose import scan_prune_candidates

        state = DockerState(DockerSettings())
        calls: list[tuple] = []

        async def fake_capture(_state, *args, timeout=None, host=None):
            calls.append(args)
            return 0, "abc123\t<none>:<none>\t10MB\n"

        with mock.patch(
            "mtbots.features.docker.compose.run_docker_capture", side_effect=fake_capture
        ):
            ok, snapshot, error = asyncio.run(scan_prune_candidates(state, prune_all=False))

        self.assertTrue(ok)
        self.assertEqual(snapshot, "abc123\t<none>:<none>\t10MB")
        self.assertEqual(error, "")
        self.assertIn("--filter", calls[0])

    def test_prune_all_excludes_images_used_by_containers(self) -> None:
        from mtbots.features.docker.compose import scan_prune_candidates

        state = DockerState(DockerSettings())

        async def fake_capture(_state, *args, timeout=None, host=None):
            if args[0] == "image":
                return (
                    0,
                    "sha256:aaa\tnginx:latest\t100MB\nbbb222\t<none>:<none>\t10MB\n",
                )
            if args[0] == "ps":
                return 0, "container1\n"
            if args[0] == "inspect":
                return 0, "sha256:aaa\n"
            return 1, "unexpected %s" % (args,)

        with mock.patch(
            "mtbots.features.docker.compose.run_docker_capture", side_effect=fake_capture
        ):
            ok, snapshot, error = asyncio.run(scan_prune_candidates(state, prune_all=True))

        self.assertTrue(ok)
        self.assertEqual(snapshot, "bbb222\t<none>:<none>\t10MB")
        self.assertEqual(error, "")


class ScanDiagnosticsTest(unittest.TestCase):
    """空项目列表必须说清原因（权限不够 / 目录没挂进来 / 缺 compose 命令）。

    以前这三种情况都是静默返回 []，面板只有一句「暂未检测到任何 Docker Compose 项目」，
    用户只能猜（线上就是这么被反馈的）。
    """

    def _state(self) -> DockerState:
        state = DockerState(DockerSettings())
        state.compose_bin = ["docker", "compose"]
        return state

    @staticmethod
    def _result(returncode: int = 0, stdout: str = "", stderr: str = ""):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_permission_denied_is_reported_with_fix(self) -> None:
        state = self._state()
        denied = self._result(
            1,
            "",
            "permission denied while trying to connect to the Docker daemon socket "
            "at unix:///var/run/docker.sock",
        )
        with mock.patch("mtbots.features.docker.compose.subprocess.run", return_value=denied):
            self.assertEqual(state.scan_projects_sync(), [])

        self.assertIn("permission denied", state.last_scan_error.lower())
        hints = " ".join(scan_hint(state))
        self.assertIn("docker.sock", hints)
        self.assertIn("stat -c", hints)

    def test_permission_denied_includes_measured_gid(self) -> None:
        """权限不够时必须给出实测 GID（`getent group docker` 在 NAS 上经常没条目）。"""
        state = self._state()
        denied = self._result(1, "", "permission denied while trying to connect to the Docker daemon")
        with mock.patch("mtbots.features.docker.compose.subprocess.run", return_value=denied):
            state.scan_projects_sync()
        with mock.patch(
            "mtbots.features.docker.compose.socket_group_hint",
            return_value=["   实测：MEASURED-GID 提示"],
        ):
            hints = " ".join(scan_hint(state))
        self.assertIn("MEASURED-GID", hints, "权限错误必须带上实测 GID 提示")

    def test_socket_group_hint_names_the_exact_gid(self) -> None:
        hints = " ".join(socket_group_hint("/var/run/docker.sock", gid=996, groups=[10001, 999]))
        self.assertIn("DOCKER_GID=996", hints)
        self.assertIn("force-recreate", hints)

    def test_socket_group_hint_silent_when_group_present(self) -> None:
        # 组已经在附加组里 → 权限问题另有原因，不许瞎指路
        self.assertEqual(socket_group_hint("/var/run/docker.sock", gid=996, groups=[10001, 996]), [])

    def test_socket_group_hint_explains_root_owned_socket(self) -> None:
        hints = " ".join(socket_group_hint("/var/run/docker.sock", gid=0, groups=[10001]))
        self.assertIn("group_add", hints)
        self.assertIn("0:0", hints)

    def test_socket_group_hint_tolerates_missing_socket(self) -> None:
        self.assertEqual(socket_group_hint("/nonexistent/docker.sock", groups=[10001]), [])

    def test_daemon_unreachable_hint(self) -> None:
        state = self._state()
        unreachable = self._result(
            1,
            "",
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
            "Is the docker daemon running?",
        )
        with mock.patch("mtbots.features.docker.compose.subprocess.run", return_value=unreachable):
            state.scan_projects_sync()
        self.assertIn("docker.sock", " ".join(scan_hint(state)))

    def test_unmounted_project_dir_is_reported(self) -> None:
        state = self._state()
        payload = json.dumps(
            [
                {
                    "Name": "media",
                    "Status": "running(1)",
                    "ConfigFiles": "/opt/stacks/media/docker-compose.yml",
                }
            ]
        )
        with mock.patch(
            "mtbots.features.docker.compose.subprocess.run",
            return_value=self._result(0, payload, ""),
        ):
            self.assertEqual(state.scan_projects_sync(), [])

        self.assertEqual(state.hidden_dirs, ["/opt/stacks/media"])
        self.assertEqual(state.last_scan_error, "")
        hints = " ".join(scan_hint(state))
        self.assertIn("在容器里不存在", hints)
        self.assertIn("-v /opt/stacks/media:/opt/stacks/media", hints)

    def test_common_mount_root_groups_sibling_projects(self) -> None:
        self.assertEqual(
            common_mount_root(["/mnt/data2/docker/a", "/mnt/data2/docker/b"]),
            "/mnt/data2/docker",
        )

    def test_common_mount_root_rejects_too_shallow(self) -> None:
        # 挂 "/" 或 "/mnt" 显然不现实 → 退回逐个提示
        self.assertIsNone(common_mount_root(["/opt/a", "/srv/b"]))
        self.assertIsNone(common_mount_root(["/mnt/a", "/mnt/b"]))
        self.assertIsNone(common_mount_root([]))

    def test_hidden_dirs_hint_suggests_one_mount_line(self) -> None:
        """16 个项目散在同一个根下面时，只给一条能直接抄的 -v。"""
        state = self._state()
        state.hidden_dirs = ["/mnt/data2/docker/clinepass-tg-bot", "/mnt/data2/docker/cpa"]
        hints = " ".join(scan_hint(state, include_compose=False))
        self.assertIn("-v /mnt/data2/docker:/mnt/data2/docker", hints)
        self.assertIn("2 个", hints)

    def test_missing_compose_binary_hint(self) -> None:
        state = DockerState(DockerSettings())
        state.compose_bin = []
        self.assertIn("docker compose", " ".join(scan_hint(state)))
        # d_list 面板已经在上面单独打印过缺命令，这里允许不重复
        self.assertEqual(scan_hint(state, include_compose=False), [])

    def test_successful_scan_has_no_hint(self) -> None:
        state = self._state()
        with tempfile.TemporaryDirectory() as tmp:
            compose_file = os.path.join(tmp, "docker-compose.yml")
            Path(compose_file).write_text("services: {}\n", encoding="utf-8")
            payload = json.dumps(
                [{"Name": "media", "Status": "running(1)", "ConfigFiles": compose_file}]
            )

            def fake_run(cmd, **kwargs):
                if "config" in cmd:
                    return self._result(0, "emby\n", "")
                return self._result(0, payload, "")

            with mock.patch(
                "mtbots.features.docker.compose.subprocess.run", side_effect=fake_run
            ):
                projects = state.scan_projects_sync()
                # 扫描只跑 compose ls；服务列表按需取（这里模拟「当前页要渲染」）
                self.assertEqual(projects[0]["services"], [])
                self.assertFalse(projects[0]["services_loaded"])
                state.load_services(projects)

        self.assertEqual([p["name"] for p in projects], ["media"])
        self.assertEqual(projects[0]["services"], ["emby"])
        self.assertTrue(projects[0]["services_loaded"])
        self.assertEqual(state.last_scan_error, "")
        self.assertEqual(state.hidden_dirs, [])
        self.assertEqual(scan_hint(state), [])


# ==================== 执行状态消息：成功可删、失败必留 ====================
class _FakeStream:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self, _n: int = -1) -> bytes:
        data, self._data = self._data, b""
        return data


class _FakeProcess:
    def __init__(self, data: bytes = b"", returncode: int = 0):
        self.stdout = _FakeStream(data)
        self.returncode = returncode
        self.pid = 4242

    async def wait(self) -> int:
        return self.returncode


class _FakeStatusMessage:
    """只记「编辑过什么、删没删」的执行消息替身（`reply_text` 返回自己）。"""

    def __init__(self):
        self.edits: list[str] = []
        self.deleted = 0

    async def reply_text(self, text: str, **kwargs):
        self.edits.append(text)
        return self

    async def edit_text(self, text: str, **kwargs) -> None:
        self.edits.append(text)

    async def delete(self) -> None:
        self.deleted += 1


class CommandFeedbackTest(unittest.TestCase):
    """`run_command_with_feedback` 的收尾策略：默认留过程消息，`cleanup_message=True` 时一律清掉。"""

    def _run(self, returncode: int, *, cleanup_message: bool):
        state = DockerState(DockerSettings())
        msg = _FakeStatusMessage()
        proc = _FakeProcess(b"Total reclaimed space: 1.2GB\n", returncode)
        out: list[str] = []
        with mock.patch(
            "mtbots.features.docker.compose.asyncio.create_subprocess_exec",
            new=mock.AsyncMock(return_value=proc),
        ):
            ok = asyncio.run(
                run_command_with_feedback(
                    state,
                    msg,
                    ["docker", "image", "prune", "-f"],
                    title="清理系统镜像",
                    cleanup_message=cleanup_message,
                    out=out,
                )
            )
        return ok, msg, out

    def test_success_deletes_the_step_message(self):
        ok, msg, out = self._run(0, cleanup_message=True)
        self.assertTrue(ok)
        self.assertEqual(msg.deleted, 1)
        self.assertTrue(any("完成" in e for e in msg.edits), "先落成完成态，删不掉也不会留假进度")
        self.assertIn("Total reclaimed space", "".join(out))

    def test_success_keeps_the_step_message_by_default(self):
        ok, msg, _out = self._run(0, cleanup_message=False)
        self.assertTrue(ok)
        self.assertEqual(msg.deleted, 0)
        self.assertTrue(any("完成" in e for e in msg.edits))

    def test_failure_also_cleans_up_the_step_message(self):
        """失败也要清掉过程消息：失败输出由调用方的收尾面板「🔻 最后输出」承载。"""
        ok, msg, out = self._run(1, cleanup_message=True)
        self.assertFalse(ok)
        self.assertEqual(msg.deleted, 1, "失败消息不再残留")
        self.assertTrue(any("失败" in e for e in msg.edits), "仍然先落成失败态再删")
        self.assertIn("Total reclaimed space", "".join(out), "输出照旧回传给调用方")

    def test_delete_message_quietly_swallows_errors(self):
        class _Boom:
            async def delete(self):
                raise RuntimeError("没有删消息权限")

        self.assertFalse(asyncio.run(delete_message_quietly(_Boom())))
        self.assertTrue(asyncio.run(delete_message_quietly(_FakeStatusMessage())))


class FailureTailTest(unittest.TestCase):
    """失败时抄进收尾面板的输出尾巴：多了成日志，少了等于没说。"""

    def test_takes_last_non_empty_lines(self):
        captured = ["line1\nline2\n\nline3\nline4\n"]
        self.assertEqual(docker_handlers._tail_lines(captured, 2), ["line3", "line4"])
        self.assertEqual(
            docker_handlers._tail_lines([], 2), [], "没有输出（比如命令没起来）就返回空"
        )

    def test_truncates_long_lines(self):
        lines = docker_handlers._tail_lines(["x" * 400], 1)
        self.assertEqual(len(lines[0]), docker_handlers.FAIL_TAIL_CHARS)

    def test_failure_block_escapes_html(self):
        self.assertEqual(docker_handlers._failure_block([]), "")
        block = docker_handlers._failure_block(["boom <b>tag</b>"])
        self.assertTrue(block.startswith("🔻"))
        self.assertIn("&lt;b&gt;tag&lt;/b&gt;", block)
        self.assertNotIn("<b>tag</b>", block)

    def test_progress_keyboard_offers_interrupt_and_jobs(self):
        markup = docker_handlers._progress_keyboard("abc123")
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("d|task_cancel|abc123", callbacks)
        self.assertIn("nav|jobs", callbacks)


# ==================== 多主机：配置与命令包装 ====================
class HostConfigTest(unittest.TestCase):
    """主机清单必须「非法就明说」，绝不静默退化；没有文件时是单机（与老版本一致）。"""

    def _write(self, payload) -> str:
        path = Path(tempfile.mkdtemp(prefix="mtbots-hosts-")) / "docker-hosts.json"
        path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
        )
        return str(path)

    def _ssh_key(self) -> str:
        key = Path(tempfile.mkdtemp(prefix="mtbots-key-")) / "id_ed25519"
        key.write_text("PRIVATE", encoding="utf-8")
        return str(key)

    def test_missing_file_is_single_local_host(self):
        hosts, notes = load_hosts("/nonexistent/docker-hosts.json")
        self.assertEqual([h.id for h in hosts], ["local"])
        self.assertFalse(hosts[0].is_remote)
        self.assertEqual(notes, [])

    def test_no_path_is_single_local_host(self):
        hosts, notes = load_hosts(None)
        self.assertEqual([h.id for h in hosts], ["local"])
        self.assertEqual(notes, [])

    def test_local_and_ssh_hosts_are_parsed(self):
        key = self._ssh_key()
        path = self._write(
            {
                "hosts": [
                    {"id": "nas", "label": "本机 NAS", "kind": "local"},
                    {
                        "id": "vps",
                        "label": "Oracle",
                        "kind": "ssh",
                        "target": "mtbots@10.0.0.5",
                        "identity": key,
                        "roots": ["/opt"],
                    },
                ]
            }
        )
        hosts, notes = load_hosts(path)
        self.assertEqual([h.id for h in hosts], ["nas", "vps"])
        self.assertEqual(notes, [])
        self.assertFalse(hosts[0].is_remote)
        self.assertTrue(hosts[1].is_remote)
        self.assertEqual(hosts[1].display, "Oracle")
        self.assertEqual(hosts[1].roots, ("/opt",))

    def test_bad_json_falls_back_to_single_host_with_note(self):
        path = self._write("{ this is not json")
        hosts, notes = load_hosts(path)
        self.assertEqual([h.id for h in hosts], ["local"])
        self.assertTrue(notes and "读取失败" in notes[0])

    def test_empty_hosts_list_falls_back(self):
        hosts, notes = load_hosts(self._write({"hosts": []}))
        self.assertEqual([h.id for h in hosts], ["local"])
        self.assertTrue(notes)

    def test_disabled_host_is_skipped(self):
        path = self._write({"hosts": [
            {"id": "nas", "kind": "local"},
            {"id": "vps", "kind": "ssh", "target": "mtbots@10.0.0.5", "identity": self._ssh_key(), "enabled": False},
            {"id": "old", "kind": "local", "enabled": "0"},
        ]})
        hosts, _notes = load_hosts(path)
        self.assertEqual([h.id for h in hosts], ["nas"])

    def test_port_must_be_numeric_and_roots_must_be_a_list(self):
        """静默放行是坑：`port:"abc"` 以前悄悄变 22，`roots:"/opt"` 以前等于不限制。"""
        key = self._ssh_key()
        base = {"id": "vps", "kind": "ssh", "target": "mtbots@10.0.0.5", "identity": key}
        for bad, needle in (({"port": "abc"}, "port"), ({"port": True}, "port"),
                            ({"port": 70000}, "port"), ({"roots": "/opt"}, "roots"),
                            ({"roots": 123}, "roots")):
            entry = dict(base, **bad)
            hosts, _notes = load_hosts(self._write({"hosts": [entry]}))
            self.assertIn(needle, hosts[0].error, "entry=%r" % (bad,))

    def test_target_cannot_start_with_a_dash(self):
        """`-oProxyCommand=…@host` 会被 ssh 当选项吃掉（选项注入），必须拒。"""
        key = self._ssh_key()
        path = self._write({"hosts": [
            {"id": "vps", "kind": "ssh", "target": "-oProxyCommand=touch /tmp/x@10.0.0.5", "identity": key}
        ]})
        hosts, _notes = load_hosts(path)
        self.assertTrue(hosts[0].error, "以 - 开头的 target 必须被拒")

    def test_invalid_targets_are_rejected(self):
        key = self._ssh_key()
        for bad in ("10.0.0.5", "mtbots@10.0.0.5; rm -rf /", "mtbots@host -o ProxyCommand=x", ""):
            path = self._write(
                {"hosts": [{"id": "vps", "kind": "ssh", "target": bad, "identity": key}]}
            )
            hosts, _notes = load_hosts(path)
            self.assertTrue(hosts[0].error, "target=%r 必须被拒" % bad)
            self.assertIn("user@host", hosts[0].error)

    def test_invalid_id_kind_and_strict(self):
        key = self._ssh_key()
        cases = [
            ({"id": "Bad ID", "kind": "local"}, "id"),
            ({"id": "vps", "kind": "telnet"}, "kind"),
            ({"id": "vps", "kind": "ssh", "target": "u@h", "strict": "no", "identity": key}, "strict"),
            (
                {"id": "vps", "kind": "ssh", "target": "u@h", "identity": key, "roots": ["opt"]},
                "roots",
            ),
        ]
        for entry, needle in cases:
            hosts, _notes = load_hosts(self._write({"hosts": [entry]}))
            self.assertIn(needle, hosts[0].error)

    def test_missing_identity_marks_host_error(self):
        path = self._write(
            {
                "hosts": [
                    {
                        "id": "vps",
                        "kind": "ssh",
                        "target": "mtbots@10.0.0.5",
                        "identity": "/nonexistent/id_ed25519",
                    }
                ]
            }
        )
        hosts, _notes = load_hosts(path)
        self.assertIn("私钥不存在", hosts[0].error)

    def test_duplicate_ids_keep_first_and_note(self):
        path = self._write(
            {"hosts": [{"id": "nas", "kind": "local"}, {"id": "nas", "kind": "local"}]}
        )
        hosts, notes = load_hosts(path)
        self.assertEqual(len(hosts), 1)
        self.assertTrue(any("重复" in note for note in notes))


class HostCommandTest(unittest.TestCase):
    """远端命令的拼装：引号、选项、cwd 一个都不能错（错了就是静默挂空目录级别的坑）。"""

    def _remote(self, **kwargs) -> DockerHost:
        base = dict(
            id="vps",
            label="Oracle",
            kind="ssh",
            target="mtbots@10.0.0.5",
            port=2222,
            identity="/app/data/ssh/id_ed25519",
            known_hosts="/app/data/ssh/known_hosts",
            strict="accept-new",
        )
        base.update(kwargs)
        return DockerHost(**base)

    def test_local_command_is_untouched(self):
        host = DockerHost(id="local", kind="local")
        cmd = ["docker", "compose", "-f", "/opt/a/docker-compose.yml", "pull"]
        self.assertEqual(host.command(cmd), cmd)
        self.assertEqual(host.cwd("/opt/a"), "/opt/a")

    def test_remote_command_is_wrapped_with_options(self):
        host = self._remote()
        cmd = ["docker", "compose", "-f", "/opt/blog/docker-compose.yml", "pull"]
        wrapped = host.command(cmd)
        self.assertEqual(wrapped[0], "ssh")
        self.assertIn("-p", wrapped)
        self.assertIn("2222", wrapped)
        self.assertIn("BatchMode=yes", wrapped)
        self.assertIn("StrictHostKeyChecking=accept-new", wrapped)
        self.assertIn("UserKnownHostsFile=/app/data/ssh/known_hosts", wrapped)
        self.assertEqual(wrapped[-3], "--", "-- 必须在目标之前（结束选项解析，防注入）")
        self.assertEqual(wrapped[-2], "mtbots@10.0.0.5")
        self.assertEqual(
            wrapped[-1], "docker compose -f /opt/blog/docker-compose.yml pull"
        )

    def test_remote_command_quotes_paths_with_spaces(self):
        host = self._remote()
        wrapped = host.command(["docker", "compose", "-f", "/opt/my blog/docker-compose.yml", "up", "-d"])
        self.assertIn("'/opt/my blog/docker-compose.yml'", wrapped[-1])

    def test_remote_never_uses_a_local_cwd(self):
        self.assertIsNone(self._remote().cwd("/opt/blog"))
        self.assertIsNone(self._remote().cwd(""))

    def test_roots_filter(self):
        host = self._remote(roots=("/opt",))
        self.assertTrue(host.allows("/opt/blog"))
        self.assertTrue(host.allows("/opt"))
        self.assertFalse(host.allows("/opt2/blog"))
        self.assertFalse(host.allows("/srv/blog"))

    def test_explain_exit_maps_ssh_codes(self):
        host = self._remote()
        self.assertIn("SSH 连不上", explain_exit(255, "", host))
        self.assertIn("守卫脚本", explain_exit(126, "", host))
        self.assertIn("未安装 docker compose", explain_exit(127, "", host))
        self.assertEqual(explain_exit(1, "boom\nsecond", host), "boom")
        # 本机（local）不套 ssh 的退出码语义
        local = DockerHost(id="local", kind="local")
        self.assertEqual(explain_exit(255, "permission denied", local), "permission denied")


class MultiHostStateTest(unittest.TestCase):
    """多主机状态：逐主机扫描、项目带主机、命令包成 ssh、失败按主机提示。"""

    @staticmethod
    def _result(returncode: int = 0, stdout: str = "", stderr: str = ""):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    def _state(self):
        base = Path(tempfile.mkdtemp(prefix="mtbots-mh-"))
        key = base / "id_ed25519"
        key.write_text("PRIVATE", encoding="utf-8")
        local_dir = base / "media"
        local_dir.mkdir()
        (local_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        self.local_dir = str(local_dir)
        hosts = [
            DockerHost(id="nas", label="本机 NAS", kind="local"),
            DockerHost(
                id="vps",
                label="Oracle",
                kind="ssh",
                target="mtbots@10.0.0.5",
                identity=str(key),
            ),
        ]
        state = DockerState(DockerSettings(), hosts=hosts)
        state.compose_bin = ["docker", "compose"]
        # 预置远端探测结果：测试里不能真的跑 ssh（会等 ConnectTimeout）
        state.remote_compose["vps"] = ["docker", "compose"]
        return state

    def _local_payload(self) -> str:
        return json.dumps(
            [
                {
                    "Name": "media",
                    "Status": "running(1)",
                    "ConfigFiles": os.path.join(self.local_dir, "docker-compose.yml"),
                }
            ]
        )

    def _remote_payload(self) -> str:
        return json.dumps(
            [
                {
                    "Name": "blog",
                    "Status": "exited(2)",
                    "ConfigFiles": "/opt/blog/docker-compose.yml",
                }
            ]
        )

    def _fake_run(self, remote_rc: int = 0, remote_err: str = ""):
        """本地/远端两套假 docker：按 argv 区分（远端一律是 ssh 开头）。"""

        def fake(cmd, **kwargs):
            if cmd[0] == "ssh":
                joined = cmd[-1]
                if "version" in joined:
                    return self._result(0, "Docker Compose version v2.35.1")
                if "ls" in joined:
                    if remote_rc:
                        return self._result(remote_rc, "", remote_err)
                    return self._result(0, self._remote_payload())
                return self._result(0, "web\napi\n")

            if "version" in cmd:
                return self._result(0, "Docker Compose version v2.35.1")
            if "ls" in cmd:
                return self._result(0, self._local_payload())
            return self._result(0, "emby\n")

        return fake

    def test_scan_merges_hosts_and_does_not_check_remote_paths(self):
        state = self._state()
        with mock.patch("mtbots.features.docker.compose.subprocess.run", side_effect=self._fake_run()):
            projects = state.scan_projects_sync()
            state.load_services(projects)

        by_host = {p["name"]: p for p in projects}
        self.assertEqual(sorted(by_host), ["blog", "media"])
        self.assertEqual(by_host["media"]["host"], "nas")
        self.assertEqual(by_host["blog"]["host"], "vps")
        self.assertEqual(by_host["blog"]["host_label"], "Oracle")
        # 远端路径在本地当然不存在，但**不能**因此被丢掉或塞进 hidden_dirs
        self.assertEqual(state.hidden_dirs, [])
        self.assertEqual(state.host_errors, {})
        self.assertEqual(by_host["blog"]["services"], ["web", "api"])
        self.assertEqual(by_host["media"]["services"], ["emby"])
        self.assertTrue(state.multi_host)
        self.assertEqual(state.project_label(by_host["blog"]), "vps/blog")
        self.assertEqual(state.project_label(by_host["media"]), "nas/media")
        self.assertEqual([p["host"] for p in state.order(projects)], ["nas", "vps"])

    def test_single_host_labels_stay_plain(self):
        state = DockerState(DockerSettings())
        state.compose_bin = ["docker", "compose"]
        self.assertFalse(state.multi_host)
        self.assertEqual(state.project_label({"name": "media", "host": "local"}), "media")

    def test_remote_scan_failure_is_per_host(self):
        state = self._state()
        with mock.patch(
            "mtbots.features.docker.compose.subprocess.run",
            side_effect=self._fake_run(remote_rc=255, remote_err="ssh: connect to host 10.0.0.5 port 22: refused"),
        ):
            projects = state.scan_projects_sync()

        self.assertEqual([p["name"] for p in projects], ["media"], "本机项目照常列出")
        self.assertIn("SSH 连不上", state.host_errors["vps"])
        self.assertEqual(state.last_scan_error, "", "本机没出错，last_scan_error 保持空")
        hints = " ".join(scan_hint(state))
        self.assertIn("Oracle", hints)
        self.assertIn("自测", hints)

    def test_remote_error_hint_for_guard_and_missing_key(self):
        state = self._state()
        state.host_errors = {"vps": "远端授权只允许 compose 操作（守卫脚本拒绝了这条命令）"}
        self.assertIn("守卫脚本", " ".join(scan_hint(state)))

        state = self._state()
        state.hosts[1] = DockerHost(
            id="vps", kind="ssh", target="mtbots@10.0.0.5", error="私钥不存在：/app/data/ssh/id_ed25519"
        )
        state.host_errors = {"vps": "私钥不存在：/app/data/ssh/id_ed25519"}
        hints = " ".join(scan_hint(state))
        self.assertIn("chmod 600", hints)
        self.assertIn("id_ed25519", hints)

    def test_build_compose_cmd_wraps_remote_projects(self):
        state = self._state()
        remote = {"name": "blog", "dir": "/opt/blog", "host": "vps",
                  "config_files": ["/opt/blog/docker-compose.yml"]}
        cmd = state.build_compose_cmd(remote, "pull")
        self.assertEqual(cmd[0], "ssh")
        self.assertEqual(cmd[-1], "docker compose -f /opt/blog/docker-compose.yml pull")
        self.assertIsNone(state.cwd_for(remote))
        self.assertEqual(state.cwd_for({"name": "m", "dir": "/srv/m", "host": "nas"}), "/srv/m")

    def test_run_command_with_feedback_uses_ssh_and_no_cwd(self):
        state = self._state()
        msg = _FakeStatusMessage()
        proc = _FakeProcess(b"Total reclaimed space: 1.2GB\n", 0)
        remote = {"name": "blog", "dir": "/opt/blog", "host": "vps"}
        seen: dict = {}

        async def fake_exec(*cmd, **kwargs):
            seen["cmd"] = list(cmd)
            seen["kwargs"] = kwargs
            return proc

        with mock.patch(
            "mtbots.features.docker.compose.asyncio.create_subprocess_exec", new=fake_exec
        ):
            ok = asyncio.run(
                run_command_with_feedback(
                    state,
                    msg,
                    state.build_compose_cmd(remote, "pull"),
                    cwd=state.cwd_for(remote),
                    title="拉取新镜像 - vps/blog",
                    host=state.host_of(remote),
                )
            )

        self.assertTrue(ok)
        self.assertEqual(seen["cmd"][0], "ssh")
        self.assertIsNone(seen["kwargs"].get("cwd"), "远端不能带本地 cwd")

    def test_readonly_capture_wraps_host(self):
        state = self._state()
        seen: list = []

        def fake_run(cmd, **kwargs):
            seen.append(cmd)
            return self._result(0, "blog-web-1\tUp 2 hours\t80/tcp\n")

        remote = state.host_by_id("vps")
        with mock.patch("mtbots.features.docker.compose.subprocess.run", side_effect=fake_run):
            ok, output = asyncio.run(dump_container_status(state, remote))

        self.assertTrue(ok)
        self.assertEqual(seen[0][0], "ssh")
        self.assertIn("docker ps -a", seen[0][-1])
        self.assertIn("blog-web-1", output)

    def test_remote_compose_probe_is_cached(self):
        """远端只有 docker-compose（老 NAS）也要能用：探测按主机缓存。"""
        state = self._state()
        state.remote_compose.pop("vps", None)
        seen: list = []

        def fake_run(cmd, **kwargs):
            seen.append(cmd)
            if cmd[-1].endswith("docker compose version"):
                return self._result(1, "", "docker: 'compose' is not a docker command")
            return self._result(0, "docker-compose version 1.29.2")

        remote = state.host_by_id("vps")
        with mock.patch("mtbots.features.docker.compose.subprocess.run", side_effect=fake_run):
            binary = state.get_remote_compose_bin(remote)

        self.assertEqual(binary, ["docker-compose"])
        self.assertEqual(state.get_remote_compose_bin(remote), ["docker-compose"])
        self.assertEqual(len(seen), 2, "第二次必须走缓存，不再探测")
        self.assertEqual(seen[0][0], "ssh")

    def test_make_state_loads_hosts_from_settings(self):
        from mtbots.features.docker.compose import make_state

        base = Path(tempfile.mkdtemp(prefix="mtbots-ms-"))
        key = base / "id_ed25519"
        key.write_text("PRIVATE", encoding="utf-8")
        hosts_file = base / "docker-hosts.json"
        hosts_file.write_text(
            json.dumps(
                {
                    "hosts": [
                        {"id": "nas", "kind": "local"},
                        {"id": "vps", "kind": "ssh", "target": "u@h", "identity": str(key)},
                    ]
                }
            ),
            encoding="utf-8",
        )
        settings = DockerSettings(hosts_file=hosts_file)
        state = make_state(settings)
        self.assertEqual([h.id for h in state.hosts], ["nas", "vps"])
        self.assertTrue(state.multi_host)


# ==================== 提速：服务列表懒加载 + 主机并行扫描 ====================
class LazyServicesTest(unittest.TestCase):
    """`compose config --services` 是每个项目一条子进程（远端=一次 SSH 握手），
    全量扫描时逐项目跑就是主面板慢到十来秒的原因——现在只对要渲染的那些项目取。"""

    @staticmethod
    def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> mock.Mock:
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    def _local_state(self) -> tuple[DockerState, str]:
        base = Path(tempfile.mkdtemp(prefix="mtbots-svc-"))
        state = DockerState(DockerSettings())
        state.compose_bin = ["docker", "compose"]
        return state, str(base)

    def _write_project(self, base: str, name: str) -> str:
        work = Path(base) / name
        work.mkdir(parents=True, exist_ok=True)
        (work / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        return str(work)

    def test_scan_does_not_run_config_services(self) -> None:
        state, base = self._local_state()
        work = self._write_project(base, "media")
        payload = json.dumps(
            [{"Name": "media", "Status": "running(1)", "ConfigFiles": os.path.join(work, "docker-compose.yml")}]
        )
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return self._result(0, payload, "")

        with mock.patch("mtbots.features.docker.compose.subprocess.run", side_effect=fake_run):
            projects = state.scan_projects_sync()

        self.assertEqual(len(calls), 1, "扫描只该跑 compose ls")
        self.assertIn("ls", calls[0])
        self.assertEqual(projects[0]["services"], [])
        self.assertFalse(projects[0]["services_loaded"])

    def test_services_are_fetched_once_and_reused_across_scans(self) -> None:
        state, base = self._local_state()
        work = self._write_project(base, "media")
        payload = json.dumps(
            [{"Name": "media", "Status": "running(1)", "ConfigFiles": os.path.join(work, "docker-compose.yml")}]
        )
        configs: list[int] = []

        def fake_run(cmd, **kwargs):
            if "config" in cmd:
                configs.append(1)
                return self._result(0, "emby\nweb\n", "")
            return self._result(0, payload, "")

        with mock.patch("mtbots.features.docker.compose.subprocess.run", side_effect=fake_run):
            projects = state.scan_projects_sync()
            asyncio.run(state.ensure_services(projects))
            self.assertEqual(projects[0]["services"], ["emby", "web"])
            self.assertTrue(projects[0]["services_loaded"])
            # 同一份缓存再渲染一次：不再跑 compose config
            asyncio.run(state.ensure_services(projects))
            # 缓存过期后重新扫描，服务列表直接从缓存复原
            state.projects_cache_time = 0.0
            again = state.scan_projects_sync()
            self.assertEqual(again[0]["services"], ["emby", "web"])
            self.assertTrue(again[0]["services_loaded"])

        self.assertEqual(len(configs), 1)

    def test_failed_service_lookup_is_not_cached_and_reports_error(self) -> None:
        state, base = self._local_state()
        work = self._write_project(base, "media")
        payload = json.dumps(
            [{"Name": "media", "Status": "running(1)", "ConfigFiles": os.path.join(work, "docker-compose.yml")}]
        )
        attempts: list[int] = []

        def fake_run(cmd, **kwargs):
            if "config" in cmd:
                attempts.append(1)
                return self._result(1, "", "no such file")
            if "ps" in cmd:  # 容器 label 兜底同样失败：两个来源都没取到
                return self._result(1, "", "docker: not found")
            return self._result(0, payload, "")

        with mock.patch("mtbots.features.docker.compose.subprocess.run", side_effect=fake_run):
            projects = state.scan_projects_sync()
            asyncio.run(state.ensure_services(projects))
        self.assertEqual(projects[0]["services"], [])
        self.assertFalse(projects[0]["services_loaded"], "取失败不能标成已加载")
        self.assertEqual(state.services_cache, {}, "取失败不能进缓存")
        self.assertTrue(projects[0]["services_error"], "失败原因要留给面板显示")
        self.assertEqual(len(attempts), 1)

    def test_service_lookup_falls_back_to_container_labels(self) -> None:
        """compose 文件解析失败时（典型：`.env` 归 root、容器用户读不到），
        服务名必须从容器 label 兜底取出来——不读文件、不需要额外权限。"""
        state, base = self._local_state()
        work = self._write_project(base, "cpa")
        payload = json.dumps(
            [{"Name": "cpa", "Status": "running(4)", "ConfigFiles": os.path.join(work, "docker-compose.yml")}]
        )

        def fake_run(cmd, **kwargs):
            if "config" in cmd:
                return self._result(1, "", "open /docker/cpa/.env: permission denied")
            if "ps" in cmd:
                # 兜底查询按**项目名**过滤并读 service label——这两个键写错会静默失效
                #（命令成功但返回空/无关结果），所以在这里钉住；顺便验证去重。
                self.assertIn("label=com.docker.compose.project=cpa", cmd)
                self.assertIn("-a", cmd, "已停止的容器也要算进来")
                self.assertIn("--format", cmd)
                self.assertTrue(
                    any("com.docker.compose.service" in str(part) for part in cmd),
                    "要读 com.docker.compose.service label",
                )
                return self._result(
                    0,
                    "cli-proxy-api\noh-my-cpa\ncpa-usage-keeper\ncpa-manager-plus\ncli-proxy-api\n",
                    "",
                )
            return self._result(0, payload, "")

        with mock.patch("mtbots.features.docker.compose.subprocess.run", side_effect=fake_run):
            projects = state.scan_projects_sync()
            asyncio.run(state.ensure_services(projects))
        self.assertEqual(
            projects[0]["services"],
            ["cli-proxy-api", "cpa-manager-plus", "cpa-usage-keeper", "oh-my-cpa"],
            "兜底结果要排序去重",
        )
        self.assertEqual(projects[0]["services_source"], "containers", "面板要能标出来源")
        self.assertTrue(projects[0]["services_loaded"])
        self.assertNotIn("services_error", projects[0])
        self.assertTrue(state.services_cache, "兜底结果同样进缓存")

    def test_failed_service_lookup_backs_off_then_retries(self) -> None:
        """取失败要退避（别每次渲染都重跑两条命令），窗口过后必须重试，且原因一直可见。"""
        state, base = self._local_state()
        work = self._write_project(base, "media")
        payload = json.dumps(
            [{"Name": "media", "Status": "running(1)", "ConfigFiles": os.path.join(work, "docker-compose.yml")}]
        )
        attempts: list[int] = []

        def fake_run(cmd, **kwargs):
            if "config" in cmd:
                attempts.append(1)
                return self._result(1, "", "no such file")
            if "ps" in cmd:
                return self._result(1, "", "docker: not found")
            return self._result(0, payload, "")

        with mock.patch("mtbots.features.docker.compose.subprocess.run", side_effect=fake_run):
            first = state.scan_projects_sync()
            asyncio.run(state.ensure_services(first))
            self.assertEqual(len(attempts), 1)
            self.assertTrue(first[0]["services_error"])

            # 退避窗口内：第二次渲染不再重查，但上次的原因要继续显示
            second = state.scan_projects_sync()
            asyncio.run(state.ensure_services(second))
            self.assertEqual(len(attempts), 1, "退避窗口内不该重查")
            self.assertTrue(second[0]["services_error"], "退避期间原因仍要可见")

            # 窗口过期后：必须重试
            with mock.patch("mtbots.features.docker.compose.SERVICES_FAIL_TTL", 0):
                third = state.scan_projects_sync()
                asyncio.run(state.ensure_services(third))
            self.assertEqual(len(attempts), 2, "窗口过后要重试")

    def test_service_backoff_is_cleared_by_refresh_and_invalidate(self) -> None:
        """🔄 强制刷新与升级/清理都要立刻清掉失败退避，否则修好权限还得干等窗口。"""
        state, _base = self._local_state()
        state.services_failed["x|media|/p/docker-compose.yml"] = (time.monotonic(), "boom")
        state.retry_failed_services()
        self.assertEqual(state.services_failed, {})

        state.services_failed["x|media|/p/docker-compose.yml"] = (time.monotonic(), "boom")
        state.invalidate_cache()
        self.assertEqual(state.services_failed, {})

    def test_force_refresh_clears_service_backoff(self) -> None:
        """首页 🔄（`docker.refresh(force=True)`）清退避；非 force 的 TTL 刷新不能清。"""
        from types import SimpleNamespace

        state, _base = self._local_state()
        core = SimpleNamespace(data={MODULE.id: state})
        calls: list[bool] = []

        async def fake_get_projects(*, force_refresh=False):
            calls.append(force_refresh)
            return []

        state.services_failed["x|media|/p/c.yml"] = (time.monotonic(), "boom")
        with mock.patch.object(state, "get_projects", new=fake_get_projects):
            asyncio.run(refresh(core, 1, force=False))
            self.assertTrue(state.services_failed, "非 force 不该清退避")
            asyncio.run(refresh(core, 1, force=True))
        self.assertEqual(state.services_failed, {})
        self.assertEqual(calls, [False, True])

    def test_command_path_ignores_service_backoff(self) -> None:
        """命令路径是明确操作：退避窗口内也要强取一次，否则修好 `.env` 后会被误报「没有服务」。"""
        state, base = self._local_state()
        work = self._write_project(base, "media")
        project = {
            "name": "media",
            "dir": work,
            "host": state.local_host.id,
            "config_files": [os.path.join(work, "docker-compose.yml")],
            "services": [],
            "services_loaded": False,
        }
        state.services_failed[state.services_key(project)] = (time.monotonic(), "boom")

        with mock.patch.object(state, "get_project_services", return_value=(["emby"], "")), \
                mock.patch.object(state, "get_project_services_from_containers", return_value=None):
            self.assertTrue(
                asyncio.run(docker_handlers._service_exists(state, project, "emby")),
                "退避窗口内也必须重取，而不是把空列表当成「没有这个服务」",
            )

    def test_invalidated_cache_drops_services(self) -> None:
        state, _base = self._local_state()
        state.services_cache["nas|media|/x/c.yml"] = (time.monotonic(), ["emby"], "compose")
        state.invalidate_cache()
        self.assertEqual(state.services_cache, {})

    def test_services_for_one_page_are_fetched_in_parallel(self) -> None:
        """当前页 3 个项目的 `config --services` 必须并发跑（否则一页又是 3 次串行等待）。"""
        import threading

        state, base = self._local_state()
        names = ["a", "b", "c"]
        payload = json.dumps(
            [
                {
                    "Name": name,
                    "Status": "running(1)",
                    "ConfigFiles": os.path.join(self._write_project(base, name), "docker-compose.yml"),
                }
                for name in names
            ]
        )
        barrier = threading.Barrier(len(names), timeout=3)

        def fake_run(cmd, **kwargs):
            if "config" in cmd:
                barrier.wait()  # 串行的话这里超时
                return self._result(0, "emby\n", "")
            return self._result(0, payload, "")

        with mock.patch("mtbots.features.docker.compose.subprocess.run", side_effect=fake_run):
            projects = state.scan_projects_sync()
            asyncio.run(state.ensure_services(projects))
        self.assertEqual([p["services"] for p in projects], [["emby"]] * 3)


class HostScanParallelismTest(unittest.TestCase):
    def test_hosts_are_scanned_in_parallel(self) -> None:
        """两台主机互不依赖：一台慢/连不上不该拖住另一台（串行时 barrier 会超时）。"""
        import threading

        base = Path(tempfile.mkdtemp(prefix="mtbots-par-"))
        work = base / "media"
        work.mkdir()
        (work / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        hosts = [
            DockerHost(id="nas", kind="local"),
            DockerHost(id="vps", kind="ssh", target="mtbots@10.0.0.5"),
        ]
        state = DockerState(DockerSettings(), hosts=hosts)
        state.compose_bin = ["docker", "compose"]
        state.remote_compose["vps"] = ["docker", "compose"]
        barrier = threading.Barrier(2, timeout=3)
        payload = json.dumps(
            [{"Name": "media", "Status": "running(1)", "ConfigFiles": str(work / "docker-compose.yml")}]
        )

        def fake_run(cmd, **kwargs):
            barrier.wait()
            return mock.Mock(returncode=0, stdout=payload, stderr="")

        with mock.patch("mtbots.features.docker.compose.subprocess.run", side_effect=fake_run):
            projects = state.scan_projects_sync()

        self.assertEqual(state.host_errors, {})
        self.assertEqual(sorted(p["host"] for p in projects), ["nas", "vps"])

    def test_host_with_config_error_is_never_probed(self) -> None:
        """配置本身有错（私钥不存在/target 非法）的主机绝不再发一次 ssh 去探测。"""
        host = DockerHost(id="vps", kind="ssh", target="u@h", error="私钥不存在：/x")
        state = DockerState(DockerSettings(), hosts=[DockerHost(id="nas", kind="local"), host])
        with mock.patch(
            "mtbots.features.docker.compose.subprocess.run",
            side_effect=AssertionError("配置错的主机不该被探测"),
        ):
            self.assertEqual(state.get_remote_compose_bin(host), [])

    def test_ssh_255_gives_up_instead_of_probing_the_second_candidate(self) -> None:
        """ssh 自己失败（255）时换 `docker-compose` 再试只是白等一个 ConnectTimeout。"""
        host = DockerHost(id="vps", kind="ssh", target="mtbots@10.0.0.5")
        state = DockerState(DockerSettings(), hosts=[DockerHost(id="nas", kind="local"), host])
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.Mock(returncode=255, stdout="", stderr="Permission denied")

        with mock.patch("mtbots.features.docker.compose.subprocess.run", side_effect=fake_run):
            self.assertEqual(state.get_remote_compose_bin(host), [])
        self.assertEqual(len(calls), 1)


class SshMultiplexTest(unittest.TestCase):
    """ssh 连接复用：同一台主机上第 2..N 条命令不该再握手（实测回环 0.36s/次）。"""

    def _host(self) -> DockerHost:
        return DockerHost(id="vps", kind="ssh", target="mtbots@10.0.0.5", port=2222)

    def test_multiplex_options_are_added_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SSH_MULTIPLEX", None)
            wrapped = self._host().command(["docker", "compose", "ls"])
        self.assertIn("ControlMaster=auto", wrapped)
        self.assertTrue(any(c.startswith("ControlPath=/tmp/mtbots-ssh-") for c in wrapped))
        self.assertIn("ControlPersist=60", wrapped)
        self.assertEqual(wrapped[-2], "mtbots@10.0.0.5")  # 目标仍在选项之后、命令之前

    def test_multiplex_can_be_disabled(self) -> None:
        with mock.patch.dict(os.environ, {"SSH_MULTIPLEX": "0"}):
            wrapped = self._host().command(["docker", "compose", "ls"])
        self.assertNotIn("ControlMaster=auto", wrapped)

    def test_control_path_follows_the_env_override(self) -> None:
        from mtbots.features.docker.hosts import control_path, multiplex_enabled

        self.assertEqual(
            control_path({"SSH_CONTROL_DIR": "/run/mtbots"}), "/run/mtbots/mtbots-ssh-%C"
        )
        self.assertEqual(control_path({"SSH_CONTROL_DIR": "/run/mtbots/"}), "/run/mtbots/mtbots-ssh-%C")
        self.assertFalse(multiplex_enabled({"SSH_MULTIPLEX": "off"}))
        self.assertTrue(multiplex_enabled({"SSH_MULTIPLEX": "1"}))


if __name__ == "__main__":
    unittest.main()
