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
from mtbots.features.docker import MODULE, commands, help_text, id_lines, register, summary
from mtbots.features.docker.compose import (
    DockerState,
    filter_pull_noise,
    format_prune_snapshot,
    is_pull_noise,
    normalize_image_id,
    paginate_projects,
    scan_hint,
    select_unused_images,
    socket_group_hint,
    sort_projects_for_display,
)
from mtbots.features.docker.config import DockerSettings
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

        asyncio.run(state.get_projects(force_refresh=True))
        self.assertEqual(len(calls), 2)
        self.assertTrue(state.has_scan())

        state.invalidate_cache()
        self.assertFalse(state.has_scan())
        self.assertEqual(state.cached_projects()[0]["name"], "a")  # 失效只动时间戳

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

        async def fake_capture(_state, *args, timeout=None):
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

        async def fake_capture(_state, *args, timeout=None):
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
        self.assertIn("挂进容器", " ".join(scan_hint(state)))

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

        self.assertEqual([p["name"] for p in projects], ["media"])
        self.assertEqual(projects[0]["services"], ["emby"])
        self.assertEqual(state.last_scan_error, "")
        self.assertEqual(state.hidden_dirs, [])
        self.assertEqual(scan_hint(state), [])


if __name__ == "__main__":
    unittest.main()
