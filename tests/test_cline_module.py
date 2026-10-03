"""MTBots Cline 模块的单元测试（unittest，纯标准库；不联网、不调用 Telegram）。

来源：``ClinePass-TG-Bot/tests/test_core.py`` 的移植 + 合并契约新增用例
（Settings 默认拒绝、ModuleSpec 装配、summary/id_lines 不发网络请求）。

运行：

    cd /root/DSH/MTBots
    PYTHONPATH=/root/DSH/MTBots/.vendor:/root/DSH/MTBots python3 -m unittest tests.test_cline_module -v
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import mtbots.store as mtbots_store  # noqa: E402
from mtbots.acl import ACL  # noqa: E402
from mtbots.config import Settings as GlobalSettings  # noqa: E402
from mtbots.core import Core  # noqa: E402
from mtbots.features.cline import MODULE  # noqa: E402
from mtbots.features.cline import core as cli  # noqa: E402
from mtbots.features.cline import handlers as cline_handlers  # noqa: E402
from mtbots.jobs import JobCenter  # noqa: E402
from mtbots.menu import MenuManager  # noqa: E402
from mtbots.panels import PanelManager  # noqa: E402

# 真实接口（2026-09 实测）的原样返回，用于回归测试
REAL_LIMITS_PAYLOAD = {
    "data": {
        "limits": [
            {"type": "five_hour", "percentUsed": 2, "resetsAt": "2026-09-25T14:32:27.073666206Z"},
            {"type": "weekly", "percentUsed": 57, "resetsAt": "2026-09-30T12:08:27.075836336Z"},
            {"type": "monthly", "percentUsed": 28, "resetsAt": "2026-10-23T12:08:27.07803017Z"},
        ]
    },
    "success": True,
}


# ==================== 假 HTTP（绝不联网） ====================
class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, routes):
        """routes: {path: FakeResponse | list[FakeResponse] | Exception}"""
        self.routes = routes
        self.headers = {}
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        path = url.split("cline.bot", 1)[-1]
        self.calls.append(path)
        if path not in self.routes:
            return FakeResponse(404, {"error": "Not Found", "success": False})
        route = self.routes[path]
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, Exception):
            raise route
        return route


def settings_for(tmp: str, **kwargs) -> cli.Settings:
    base = dict(
        config_file=os.path.join(tmp, "config.json"),
        api_base="https://api.cline.bot",
        http_retries=0,
        retry_backoff=0.0,
        status_cooldown=0.0,
        max_keys_per_user=3,
    )
    base.update(kwargs)
    return cli.Settings(**base)


# ==================== 假 Core / 状态注入 ====================
class ExplodingClient:
    """summary / id_lines 一旦碰它就该失败（证明它们不发网络请求）。"""

    async def fetch_all(self, items):  # pragma: no cover - 触发了就是回归
        raise AssertionError("summary 不允许发起网络请求")

    async def fetch_snapshot(self, alias, api_key):  # pragma: no cover
        raise AssertionError("summary 不允许发起网络请求")


def make_core(tmp: str) -> Core:
    settings = GlobalSettings(
        bot_token="123456:TEST",
        allowed_user_ids=frozenset({1}),
        data_dir=Path(tmp),
        config_file=Path(tmp) / "config.json",
        users_file=Path(tmp) / "users.json",
        log_dir=Path(tmp) / "logs",
    )
    return Core(
        settings=settings,
        acl=ACL(frozenset({1})),
        panels=PanelManager(),
        jobs=JobCenter(),
        menu=MenuManager(None, []),
    )


def inject_state(core: Core, tmp: str) -> cline_handlers.ClineState:
    """把测试用的 ClineState 直接塞进 core.data，避免读进程环境变量。"""
    settings = settings_for(tmp)
    state = cline_handlers.ClineState(
        settings=settings,
        store=cli.ConfigStore(settings.config_file, settings.max_keys_per_user),
        client=ExplodingClient(),
        cooldown=cli.Cooldown(0.0),
    )
    core.data["cline"] = {"state": state}
    return state


class FakeApp:
    """只收集 handler，不碰 Telegram。"""

    def __init__(self):
        self.handlers: list[object] = []

    def add_handler(self, handler, group: int = 0):
        self.handlers.append(handler)


# ==================== 渲染工具 ====================
class TestHelpers(unittest.TestCase):
    def test_progress_bar_edges(self):
        width = cli.BAR_WIDTH
        self.assertEqual(cli.progress_bar(0), "░" * width)
        self.assertEqual(cli.progress_bar(100), "█" * width)
        self.assertEqual(cli.progress_bar(-5), "░" * width)
        self.assertEqual(cli.progress_bar(1000), "█" * width)
        self.assertEqual(len(cli.progress_bar(63)), width)
        self.assertEqual(cli.progress_bar(50, 4), "██░░")

    def test_progress_bar_is_wide_enough(self):
        """用户反馈进度条太短，已从 10 格拉到 16 格。"""
        self.assertGreaterEqual(cli.BAR_WIDTH, 16)

    def test_show_identity_defaults_to_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(settings_for(tmp).show_identity)
            self.assertTrue(settings_for(tmp, show_identity=True).show_identity)

    def test_mask_key(self):
        self.assertEqual(cli.mask_key("sk_1234567890"), "sk_1…7890")
        self.assertEqual(cli.mask_key("short"), "****")
        self.assertEqual(cli.mask_key(""), "****")

    def test_sanitize_alias(self):
        self.assertEqual(cli.sanitize_alias(" 主账号 "), "主账号")
        self.assertEqual(cli.sanitize_alias("work-1.v2"), "work-1.v2")
        self.assertIsNone(cli.sanitize_alias(""))
        self.assertIsNone(cli.sanitize_alias("a" * 25))
        self.assertIsNone(cli.sanitize_alias("bad;rm -rf"))

    def test_split_message(self):
        self.assertEqual(cli.split_message("hi", 100), ["hi"])
        text = "\n".join(f"line-{i}" for i in range(200))
        chunks = cli.split_message(text, 100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))

    def test_split_message_hard_cut(self):
        chunks = cli.split_message("x" * 250, 100)
        self.assertEqual([len(c) for c in chunks], [100, 100, 50])

    def test_cooldown(self):
        cd = cli.Cooldown(5)
        self.assertEqual(cd.hit(1, now=100.0), 0.0)
        self.assertEqual(cd.hit(1, now=102.0), 3.0)
        self.assertEqual(cd.hit(1, now=106.0), 0.0)
        self.assertEqual(cd.hit(2, now=106.0), 0.0)  # 不同用户互不影响
        self.assertEqual(cli.Cooldown(0).hit(1), 0.0)

    def test_parse_timestamp(self):
        # 官方返回纳秒精度 + Z，Python 3.11 的 fromisoformat 吃不下，必须规范化
        self.assertEqual(
            cli.parse_timestamp("2026-09-25T14:32:27.073666206Z"),
            datetime(2026, 9, 25, 14, 32, 27, 73666, tzinfo=timezone.utc),
        )
        self.assertEqual(
            cli.parse_timestamp("2026-09-25T14:32:27Z"),
            datetime(2026, 9, 25, 14, 32, 27, tzinfo=timezone.utc),
        )
        self.assertEqual(cli.parse_timestamp("2026-09-25T14:32:27+08:00").utcoffset(), timedelta(hours=8))
        self.assertEqual(cli.parse_timestamp("2026-09-25 14:32:27").tzinfo, timezone.utc)
        self.assertIsNone(cli.parse_timestamp("not a time"))
        self.assertIsNone(cli.parse_timestamp(""))
        self.assertIsNone(cli.parse_timestamp(None))

    def test_humanize_delta(self):
        self.assertEqual(cli.humanize_delta(-1), "已到重置时间")
        self.assertEqual(cli.humanize_delta(30), "不到 1 分钟")
        self.assertEqual(cli.humanize_delta(600), "10 分钟")
        self.assertEqual(cli.humanize_delta(3600 * 2 + 60 * 5), "2 小时 5 分")
        self.assertEqual(cli.humanize_delta(3600 * 24 * 3), "3 天")

    def test_describe_reset(self):
        now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        window = cli.Window("w", 10, reset_dt=datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc))
        text = cli.describe_reset(window, now)
        self.assertIn("还有 2 小时", text)
        self.assertRegex(text, r"^\d{2}-\d{2} \d{2}:\d{2}（还有 2 小时）$")
        self.assertEqual(cli.describe_reset(cli.Window("w", 10, reset="周一 08:00"), now), "周一 08:00")
        self.assertIsNone(cli.describe_reset(cli.Window("w", 10), now))


# ==================== 配置存储 ====================
class TestConfigStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")
        self.store = cli.ConfigStore(self.path, max_keys_per_user=3)

    def test_load_cleans_invisible_chars_in_stored_keys(self):
        """0.0.5 之前绑定的 Key 可能夹着零宽字符，加载时要自动清掉并落盘。"""
        dirty = "sk_abc\u200bdef\ufeffghi"
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "user_keys": {"42": {"主账号": dirty}}}, fh)
        data = self.store.load()
        self.assertEqual(data["user_keys"]["42"]["主账号"], "sk_abcdefghi")
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["user_keys"]["42"]["主账号"], "sk_abcdefghi")

    def test_load_keeps_clean_keys_untouched(self):
        key = "sk_" + "a" * 64
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "user_keys": {"42": {"主账号": key}}}, fh)
        stamp = os.path.getmtime(self.path) - 10
        os.utime(self.path, (stamp, stamp))
        data = self.store.load()
        self.assertEqual(data["user_keys"]["42"]["主账号"], key)
        self.assertEqual(os.path.getmtime(self.path), stamp)  # 没动过文件

    def test_load_survives_weird_user_keys_shape(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "user_keys": {"42": "not-a-dict", "43": {"a": 123}}}, fh)
        data = self.store.load()
        self.assertEqual(data["user_keys"]["42"], "not-a-dict")
        self.assertEqual(data["user_keys"]["43"], {"a": 123})

    def test_creates_file_on_first_load(self):
        data = self.store.load()
        self.assertEqual(data["user_keys"], {})
        self.assertTrue(os.path.isfile(self.path))

    def test_add_delete_persist(self):
        self.store.add(1, "主账号", "sk_aaaaaaaaaaaa")
        self.store.add(1, "备用", "sk_bbbbbbbbbbbb")
        self.assertEqual(self.store.keys(1), {"主账号": "sk_aaaaaaaaaaaa", "备用": "sk_bbbbbbbbbbbb"})
        self.assertTrue(self.store.delete(1, "备用"))
        self.assertFalse(self.store.delete(1, "不存在"))
        # 重新读盘确认真的落盘
        self.assertEqual(cli.ConfigStore(self.path).keys(1), {"主账号": "sk_aaaaaaaaaaaa"})
        self.assertEqual(self.store.clear(1), 1)
        self.assertEqual(self.store.keys(1), {})

    def test_key_limit(self):
        for i in range(3):
            self.store.add(1, f"k{i}", "sk_123456789")
        with self.assertRaises(cli.KeyLimitError):
            self.store.add(1, "overflow", "sk_123456789")
        # 覆盖已有别名不受限制
        self.store.add(1, "k0", "sk_updatedvalue")

    def test_file_permissions(self):
        self.store.add(1, "a", "sk_123456789")
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode, 0o600, f"配置文件权限应为 600，实际 {oct(mode)}")

    def test_corrupt_file_is_backed_up_not_wiped(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        data = self.store.load()
        self.assertEqual(data["user_keys"], {})
        backups = [f for f in os.listdir(self.tmp.name) if ".corrupt-" in f]
        self.assertEqual(len(backups), 1)

    def test_directory_path_raises_config_error(self):
        os.makedirs(os.path.join(self.tmp.name, "as-dir.json"))
        store = cli.ConfigStore(os.path.join(self.tmp.name, "as-dir.json"))
        with self.assertRaises(cli.ConfigError) as cm:
            store.load()
        self.assertIn("目录", str(cm.exception))

    def test_non_object_json_rejected(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump([1, 2, 3], fh)
        with self.assertRaises(cli.ConfigError):
            self.store.load()

    def test_atomic_write_leaves_no_temp_files(self):
        self.store.add(1, "a", "sk_123456789")
        leftovers = [f for f in os.listdir(self.tmp.name) if f.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_permission_error_becomes_config_error(self):
        """线上 v0.0.1 的坑：目录不可写时 mkstemp 抛 PermissionError，直接崩进程。"""
        with mock.patch.object(mtbots_store.tempfile, "mkstemp", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(cli.ConfigError) as cm:
                self.store.add(1, "a", "sk_123456789")
        message = str(cm.exception)
        self.assertIn("权限不足", message)
        self.assertIn("chown", message)

    def test_write_oserror_becomes_config_error(self):
        with mock.patch.object(mtbots_store.tempfile, "mkstemp", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(cli.ConfigError):
                self.store.add(1, "a", "sk_123456789")

    def test_replace_failure_cleans_up_temp_file(self):
        with mock.patch.object(mtbots_store.os, "replace", side_effect=OSError(1, "Operation not permitted")):
            with self.assertRaises(cli.ConfigError):
                self.store.add(1, "a", "sk_123456789")
        leftovers = [f for f in os.listdir(self.tmp.name) if f.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_load_never_raises_bare_oserror(self):
        """ConfigError 之外的 OSError 不应该漏给调用方（否则会崩在启动阶段）。"""
        with mock.patch.object(mtbots_store.tempfile, "mkstemp", side_effect=PermissionError(13, "denied")):
            try:
                self.store.load()
            except cli.ConfigError:
                pass
            except OSError as exc:  # pragma: no cover - 失败即为回归
                self.fail(f"应当转成 ConfigError，实际漏出 {exc!r}")


# ==================== 额度解析 ====================
class TestParseUsage(unittest.TestCase):
    def test_envelope_shape(self):
        windows = cli.parse_usage(
            {
                "success": True,
                "data": {
                    "h5": {"percent": 63, "remaining_str": "1h 52m", "reset_time": "18:32"},
                    "week": {"percent": 48.4, "reset_str": "周一 08:00"},
                    "month": {"percent": 31},
                },
            }
        )
        self.assertIsNotNone(windows)
        assert windows is not None
        self.assertEqual([w.label for w in windows], ["5 小时额度", "本周额度", "本月额度"])
        self.assertEqual(windows[0].remaining, "1h 52m")
        self.assertEqual(windows[1].percent, 48.4)

    def test_toplevel_and_aliases(self):
        windows = cli.parse_usage({"5h": 10, "weekly": {"used_percent": 20}, "monthly": {"percent": 0}})
        self.assertIsNotNone(windows)
        assert windows is not None
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[2].percent, 0.0)

    def test_nested_usage_key(self):
        windows = cli.parse_usage({"data": {"usage": {"h5": {"percent": 5}}}})
        assert windows is not None
        self.assertEqual(windows[0].percent, 5.0)

    def test_unparseable_returns_none(self):
        self.assertIsNone(cli.parse_usage({"data": {"email": "a@b.c"}}))
        self.assertIsNone(cli.parse_usage({"h5": {"foo": "bar"}}))
        self.assertIsNone(cli.parse_usage("nope"))
        self.assertIsNone(cli.parse_usage({}))

    def test_percent_clamped_and_nan_rejected(self):
        windows = cli.parse_usage({"h5": {"percent": 250}, "week": {"percent": "abc"}, "month": {"percent": -3}})
        assert windows is not None
        self.assertEqual(windows[0].percent, 100.0)
        self.assertEqual([w.label for w in windows], ["5 小时额度", "本月额度"])


# ==================== 官方额度接口 ====================
class TestOfficialLimits(unittest.TestCase):
    def test_real_payload(self):
        windows = cli.parse_usage(REAL_LIMITS_PAYLOAD)
        self.assertIsNotNone(windows)
        assert windows is not None
        self.assertEqual([w.label for w in windows], ["5 小时额度", "本周额度", "本月额度"])
        self.assertEqual([w.percent for w in windows], [2.0, 57.0, 28.0])
        self.assertEqual([w.remaining_percent for w in windows], [98.0, 43.0, 72.0])
        self.assertIsNotNone(windows[0].reset_dt)

    def test_limits_without_envelope(self):
        windows = cli.parse_limits_list(
            {"limits": [{"type": "weekly", "percentUsed": 10, "resetsAt": "2026-09-30T12:08:27Z"}]}
        )
        assert windows is not None
        self.assertEqual(windows[0].label, "本周额度")
        self.assertEqual(windows[0].percent, 10.0)

    def test_type_aliases(self):
        # 社区实现里出现过 '5-hour' 与线上 'five_hour' 不一致的问题，这里两种都要认
        for raw in ("five_hour", "5-hour", "5_hour", "5h", "FIVE_HOUR"):
            windows = cli.parse_usage({"data": {"limits": [{"type": raw, "percentUsed": 7}]}})
            assert windows is not None, raw
            self.assertEqual(windows[0].label, "5 小时额度", raw)

    def test_unknown_type_is_kept(self):
        windows = cli.parse_usage(
            {"data": {"limits": [{"type": "daily", "percentUsed": 12}, {"type": "weekly", "percentUsed": 30}]}}
        )
        assert windows is not None
        self.assertEqual([w.label for w in windows], ["本周额度", "daily"])

    def test_malformed_entries_skipped(self):
        self.assertIsNone(cli.parse_usage({"data": {"limits": []}}))
        self.assertIsNone(cli.parse_usage({"data": {"limits": ["nope", {"type": ""}]}}))
        self.assertIsNone(cli.parse_limits_list({"data": {}}))

    def test_warning_thresholds(self):
        self.assertEqual(cli.Window("w", 79.9).warning, "")
        self.assertEqual(cli.Window("w", 80).warning, "⚠️")
        self.assertEqual(cli.Window("w", 95).warning, "⛔️")
        self.assertEqual(cli.Window("w", None).warning, "")

    def test_rendered_panel_shows_quota_and_countdown(self):
        snapshot = cli.Snapshot(
            alias="主账号", key_mask="sk_7…aaaa", windows=cli.parse_usage(REAL_LIMITS_PAYLOAD)
        )
        text = cli.render_snapshot(snapshot, now=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc))
        self.assertIn("剩余 98%", text)
        self.assertIn("剩余 43%", text)
        self.assertIn("重置：", text)
        self.assertIn("还有", text)
        self.assertNotIn("⚠️", text)  # 2% / 57% / 28% 都不触发告警
        self.assertNotIn("⛔️", text)

    def test_badges_appear_when_hot(self):
        self.assertIn("⚠️", cli.render_snapshot(cli.Snapshot("a", "k", windows=[cli.Window("本周额度", 80.0)])))
        self.assertIn("⛔️", cli.render_snapshot(cli.Snapshot("a", "k", windows=[cli.Window("本周额度", 96.0)])))


# ==================== API 客户端 ====================
class TestClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = settings_for(self.tmp.name)

    def client(self, routes):
        return cli.ClinePassClient(self.settings, session=FakeSession(routes))

    def test_happy_path_with_real_envelope(self):
        session = FakeSession(
            {
                "/api/v1/users/me": FakeResponse(200, {"success": True, "data": {"email": "a@b.c", "displayName": "Tester"}}),
                "/api/v1/users/me/plan": FakeResponse(
                    200,
                    {
                        "success": True,
                        "data": {
                            "plan": {"displayName": "Cline Pass (Monthly)", "interval": "Monthly", "isActive": True},
                            "currentPeriodStart": "2026-09-23T12:03:54Z",
                            "currentPeriodEnd": "2026-10-23T12:03:54Z",
                        },
                    },
                ),
                "/api/v1/users/me/plan/usage-limits": FakeResponse(200, REAL_LIMITS_PAYLOAD),
            }
        )
        snapshot = cli.ClinePassClient(self.settings, session=session).fetch_snapshot_sync("主账号", "sk_test123456")
        self.assertEqual(snapshot.account["email"], "a@b.c")
        self.assertEqual(snapshot.plan["displayName"], "Cline Pass (Monthly)")
        self.assertEqual([w.percent for w in snapshot.windows], [2.0, 57.0, 28.0])
        self.assertEqual(snapshot.warnings, [])

    def test_unauthorized_queries_every_endpoint_and_names_them(self):
        """401 时不再短路：三个接口都试一遍，才能指出到底是谁拒的。"""
        unauthorized = FakeResponse(401, {"error": "Unauthorized"})
        session = FakeSession(
            {
                "/api/v1/users/me": unauthorized,
                "/api/v1/users/me/plan": unauthorized,
                "/api/v1/users/me/plan/usage-limits": unauthorized,
            }
        )
        snapshot = cli.ClinePassClient(self.settings, session=session).fetch_snapshot_sync("x", "sk_bad")
        self.assertFalse(snapshot.has_usage)
        self.assertEqual(
            session.calls,
            ["/api/v1/users/me", "/api/v1/users/me/plan", "/api/v1/users/me/plan/usage-limits"],
        )
        joined = "\n".join(snapshot.warnings)
        self.assertIn("账号接口", joined)
        self.assertIn("额度接口", joined)
        self.assertIn("401", joined)

    def test_account_401_still_reports_quota_if_usage_works(self):
        """只有账号接口 401 时，额度该显示还得显示。"""
        session = FakeSession(
            {
                "/api/v1/users/me": FakeResponse(401, {"error": "Unauthorized"}),
                "/api/v1/users/me/plan": FakeResponse(404, {"error": "Not Found"}),
                "/api/v1/users/me/plan/usage-limits": FakeResponse(200, REAL_LIMITS_PAYLOAD),
            }
        )
        snapshot = cli.ClinePassClient(self.settings, session=session).fetch_snapshot_sync("x", "sk_test123456")
        self.assertTrue(snapshot.has_usage)
        self.assertEqual([w.percent for w in snapshot.windows], [2.0, 57.0, 28.0])
        self.assertIn("账号接口", "\n".join(snapshot.warnings))

    def test_invalid_key_reports_cline_message_and_shape(self):
        """Cline 的原话 + Key 长度都要带出来 —— 这是排查 401 的关键线索。"""
        session = FakeSession(
            {"/api/v1/users/me": FakeResponse(401, {"error": "Unauthorized: please re-authenticate"})}
        )
        snapshot = cli.ClinePassClient(self.settings, session=session).fetch_snapshot_sync("x", "sk_shortkey12345")
        joined = "\n".join(snapshot.warnings)
        self.assertIn("re-authenticate", joined)
        self.assertIn("16 字符", joined)
        self.assertIn(cli.key_fingerprint("sk_shortkey12345"), joined)
        self.assertIn("sha256sum", joined)
        self.assertIn("sk_s…2345", snapshot.key_mask)

    def test_long_invalid_key_gets_no_length_warning(self):
        key = "sk_" + "a" * 64
        session = FakeSession({"/api/v1/users/me": FakeResponse(401, {"error": "Unauthorized"})})
        snapshot = cli.ClinePassClient(self.settings, session=session).fetch_snapshot_sync("x", key)
        joined = "\n".join(snapshot.warnings)
        self.assertNotIn("很可能复制", joined)
        self.assertIn("长度与实测可用的 Key 一致", joined)
        self.assertIn("67 字符", snapshot.key_mask)

    def test_missing_usage_endpoint_is_reported_not_faked(self):
        session = FakeSession(
            {
                "/api/v1/users/me": FakeResponse(200, {"data": {"email": "a@b.c"}}),
                "/api/v1/users/me/plan": FakeResponse(200, {"data": {"plan": {"name": "Cline Pass"}}}),
            }
        )
        snapshot = cli.ClinePassClient(self.settings, session=session).fetch_snapshot_sync("x", "sk_test123456")
        self.assertIn("/api/v1/users/me/plan/usage-limits", session.calls)  # 默认打官方额度接口
        self.assertFalse(snapshot.has_usage)
        self.assertTrue(any("404" in w for w in snapshot.warnings))
        text = cli.render_snapshot(snapshot)
        self.assertIn("暂无可显示的额度数据", text)
        self.assertNotIn("63%", text)

    def test_retry_on_server_error_then_success(self):
        session = FakeSession(
            {
                "/api/v1/users/me": FakeResponse(200, {"data": {"email": "a@b.c"}}),
                "/api/v1/users/me/plan": FakeResponse(200, {"data": {}}),
                "/api/v1/users/me/plan/usage-limits": [
                    FakeResponse(503, {"error": "boom"}),
                    FakeResponse(200, REAL_LIMITS_PAYLOAD),
                ],
            }
        )
        settings = settings_for(self.tmp.name, http_retries=1)
        with mock.patch.object(cli.time, "sleep", lambda *_: None):
            snapshot = cli.ClinePassClient(settings, session=session).fetch_snapshot_sync("x", "sk_test123456")
        self.assertEqual(snapshot.windows[0].percent, 2.0)

    def test_network_error_classified(self):
        import requests

        session = FakeSession({"/api/v1/users/me": requests.ConnectionError("no route")})
        snapshot = cli.ClinePassClient(self.settings, session=session).fetch_snapshot_sync("x", "sk_test123456")
        self.assertTrue(any("网络" in w for w in snapshot.warnings))

    def test_non_json_body(self):
        session = FakeSession({"/api/v1/users/me": FakeResponse(200, None, text="<html>oops</html>")})
        snapshot = cli.ClinePassClient(self.settings, session=session).fetch_snapshot_sync("x", "sk_test123456")
        self.assertTrue(any("账号信息获取失败" in w for w in snapshot.warnings))

    def test_demo_mode_marks_sample_data(self):
        settings = settings_for(self.tmp.name, demo_mode=True)
        session = FakeSession(
            {
                "/api/v1/users/me": FakeResponse(200, {"data": {"email": "a@b.c"}}),
                "/api/v1/users/me/plan": FakeResponse(200, {"data": {}}),
            }
        )
        snapshot = cli.ClinePassClient(settings, session=session).fetch_snapshot_sync("x", "sk_test123456")
        self.assertTrue(any("DEMO_MODE" in w for w in snapshot.warnings))
        self.assertEqual(len(snapshot.windows), 3)


# ==================== 渲染与安全 ====================
class TestRender(unittest.TestCase):
    def test_html_escaping(self):
        snapshot = cli.Snapshot(
            alias="<script>alert(1)</script>",
            key_mask=cli.mask_key("sk_1234567890"),
            account={"email": "<b>x</b>@y.z"},
            windows=[cli.Window("5 小时额度", 63.0, "1h 52m", "18:32")],
        )
        moment = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
        text = cli.render_panel([snapshot], now=moment, show_identity=True)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;@y.z", text)
        # 时间戳由 PanelManager 统一加在面板底部，正文里不能再出现一次（会重复）
        self.assertNotIn("更新时间", text)

    def test_email_and_name_are_not_shown_by_default(self):
        """邮箱与账号名是敏感信息：默认面板里一个字都不出现。"""
        snapshot = cli.Snapshot(
            alias="主账号",
            key_mask="sk_7…aaaa · 67 字符",
            account={"email": "someone@example.com", "displayName": "Wang Jays"},
            windows=[cli.Window("本周额度", 58.0)],
        )
        text = cli.render_panel([snapshot])
        self.assertNotIn("someone@example.com", text)
        self.assertNotIn("Wang", text)
        self.assertNotIn("👤", text)

    def test_identity_shown_only_when_asked(self):
        snapshot = cli.Snapshot(
            alias="主账号",
            key_mask="sk_7…aaaa · 67 字符",
            account={"email": "someone@example.com", "displayName": "Wang Jays"},
        )
        text = cli.render_panel([snapshot], show_identity=True)
        self.assertIn("someone@example.com", text)
        self.assertIn("Wang Jays", text)

    def test_plan_interval_is_not_repeated(self):
        """`Cline Pass (Monthly)（Monthly · ✅ 生效）` 里的重复周期要去掉。"""
        snapshot = cli.Snapshot(
            alias="主账号",
            key_mask="sk_1…7890",
            plan={"displayName": "Cline Pass (Monthly)", "interval": "Monthly", "isActive": True},
        )
        text = cli.render_snapshot(snapshot)
        self.assertIn("Cline Pass (Monthly)（✅ 生效）", text)
        self.assertNotIn("Monthly · ✅", text)

    def test_plan_interval_kept_when_not_in_the_name(self):
        snapshot = cli.Snapshot(
            alias="主账号",
            key_mask="sk_1…7890",
            plan={"displayName": "Cline Pass", "interval": "Monthly", "isActive": True},
        )
        self.assertIn("Cline Pass（Monthly · ✅ 生效）", cli.render_snapshot(snapshot))

    def test_quota_blocks_are_separated_by_blank_lines(self):
        snapshot = cli.Snapshot(
            alias="主账号",
            key_mask="sk_1…7890",
            plan={"displayName": "Cline Pass", "interval": "Monthly", "isActive": True},
            plan_period={"start": "2026-09-23T00:00:00Z", "end": "2026-10-23T00:00:00Z"},
            windows=[cli.Window("5 小时额度", 5.0), cli.Window("本周额度", 58.0), cli.Window("本月额度", 29.0)],
        )
        lines = cli.render_snapshot(snapshot).split("\n")
        for index, line in enumerate(lines):
            if line.startswith("📊"):
                self.assertEqual(lines[index - 1], "", f"{line} 上面应该有且只有一个空行")
        # 计费周期下面也要空一行（第一条额度块之前）
        period = next(i for i, line in enumerate(lines) if line.startswith("📆"))
        self.assertEqual(lines[period + 1], "")

    def test_no_leading_blank_line_when_identity_hidden(self):
        snapshot = cli.Snapshot(alias="x", key_mask="sk_1…7890", windows=[cli.Window("本周额度", 1.0)])
        self.assertFalse(cli.render_snapshot(snapshot).startswith("\n"))

    def test_alias_with_markdown_chars_is_rendered_literally(self):
        snapshot = cli.Snapshot(alias="a_b*c", key_mask="sk_1…7890", windows=[cli.Window("本周额度", 50.0)])
        text = cli.render_snapshot(snapshot)
        self.assertIn("a_b*c", text)
        self.assertNotIn("**", text)

    def test_missing_fields_do_not_crash(self):
        text = cli.render_panel([cli.Snapshot(alias="x", key_mask="****")], now=datetime(2026, 9, 25, tzinfo=timezone.utc))
        self.assertIn("账号/别名：x", text)

    def test_split_panel_keeps_all_aliases(self):
        snapshots = [
            cli.Snapshot(alias=f"acc{i}", key_mask="sk_1…7890", windows=[cli.Window("本周额度", 50.0)])
            for i in range(60)
        ]
        chunks = cli.split_message(
            cli.render_panel(snapshots, now=datetime(2026, 9, 25, tzinfo=timezone.utc)), 700
        )
        joined = "\n".join(chunks)
        for i in range(60):
            self.assertIn(f"acc{i}", joined)
        # mtbots.text 会在分片边界补闭合标签，所以留一点余量（原实现在这里会切坏 HTML）
        self.assertTrue(all(len(c) <= 800 for c in chunks))


# ==================== 存储自检 ====================
class TestSelfCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.json")
        self.store = cli.ConfigStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_writable_dir_is_ok(self):
        ok, detail = self.store.self_check()
        self.assertTrue(ok, detail)
        self.assertIn("可写", detail)

    def test_probe_leaves_no_files(self):
        self.store.self_check()
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_unwritable_dir_reports_false(self):
        with mock.patch.object(mtbots_store.tempfile, "mkstemp", side_effect=PermissionError(13, "denied")):
            ok, detail = self.store.self_check()
        self.assertFalse(ok)
        self.assertIn("不可写", detail)

    def test_directory_path_reports_false(self):
        ok, _ = cli.ConfigStore(self.tmp.name).self_check()
        self.assertFalse(ok)

    def test_add_returns_key_count(self):
        self.assertEqual(self.store.add(1, "a", "sk_123456789"), 1)
        self.assertEqual(self.store.add(1, "b", "sk_123456789"), 2)
        self.assertEqual(self.store.add(1, "a", "sk_987654321"), 2)


# ==================== Key 掩码与长度提示 ====================
class TestKeyMaskAndShape(unittest.TestCase):
    def test_mask_without_length(self):
        self.assertEqual(cli.mask_key("sk_0123456789abcdef"), "sk_0…cdef")

    def test_mask_with_length(self):
        self.assertEqual(cli.mask_key("sk_0123456789abcdef", show_length=True), "sk_0…cdef · 19 字符")

    def test_mask_short_key(self):
        self.assertEqual(cli.mask_key("short"), "****")
        self.assertEqual(cli.mask_key(""), "****")
        self.assertEqual(cli.mask_key("short", show_length=True), "**** · 5 字符")

    def test_shape_note_empty(self):
        self.assertIn("空的", cli.key_shape_note(""))

    def test_shape_note_too_short(self):
        note = cli.key_shape_note("sk_05b052fdfgdfgdfgfdg")  # 22 位，明显是截断的
        self.assertIn("22 个字符", note)
        self.assertIn(str(cli.TYPICAL_KEY_LENGTH), note)

    def test_shape_note_flags_non_ascii(self):
        """多复制了一个中文句号 —— 长度看着正常，服务端照样 401。"""
        note = cli.key_shape_note("sk_" + "a" * 63 + "。")
        self.assertIn("非 ASCII", note)
        self.assertIn("U+3002", note)

    def test_shape_note_flags_overlong_key(self):
        self.assertIn("多复制", cli.key_shape_note("sk_" + "a" * 100))

    def test_shape_note_has_no_html(self):
        """这条提示会被 esc() 转义，不能自带标签。"""
        for key in ("", "sk_short", "sk_" + "a" * 100, "sk_" + "a" * 63 + "。"):
            self.assertNotIn("<", cli.key_shape_note(key))

    def test_shape_note_silent_for_plausible_lengths(self):
        self.assertEqual(cli.key_shape_note("sk_" + "a" * 59), "")
        self.assertEqual(cli.key_shape_note("sk_" + "a" * 64), "")


class TestApiErrorFriendly(unittest.TestCase):
    def test_unauthorized_includes_server_detail(self):
        err = cli.ApiError("unauthorized", "Unauthorized: please re-authenticate", 401)
        self.assertIn("re-authenticate", err.friendly)
        self.assertIn("401", err.friendly)

    def test_detail_is_flattened_and_truncated(self):
        err = cli.ApiError("unauthorized", "a\n\nb " + "x" * 500, 401)
        self.assertNotIn("\n\n", err.friendly)
        self.assertLessEqual(len(err.friendly), 220)

    def test_other_kinds_do_not_leak_detail(self):
        err = cli.ApiError("rate_limited", "internal detail", 429)
        self.assertNotIn("internal", err.friendly)

    def test_empty_detail_is_fine(self):
        self.assertIn("401", cli.ApiError("unauthorized", "", 401).friendly)


# ==================== 命令文本修复 ====================
class TestCommandNormalize(unittest.TestCase):
    def test_plain_command(self):
        self.assertEqual(cli.parse_command_tokens("/addkey Cline01 sk_x"), ("addkey", ["Cline01", "sk_x"]))

    def test_ideographic_space_from_chinese_ime(self):
        """中文输入法的全角空格：Telegram 不认，实体会把别名也吞进命令名。"""
        self.assertEqual(
            cli.parse_command_tokens("/addkey\u3000Cline01 sk_x"), ("addkey", ["Cline01", "sk_x"])
        )

    def test_zero_width_inside_command_name_is_deleted(self):
        self.assertEqual(
            cli.parse_command_tokens("/add\u200bkey Cline-03 sk_x"),
            ("addkey", ["Cline-03", "sk_x"]),
        )

    def test_zero_width_used_as_separator_is_a_space(self):
        """零宽字符也可能被当成命令与参数之间的分隔符。"""
        self.assertEqual(
            cli.parse_command_tokens("/addkey\u200bCline01 sk_x", "space"),
            ("addkey", ["Cline01", "sk_x"]),
        )

    def test_candidates_cover_both_meanings(self):
        self.assertIn(("addkey", ["Cline01", "sk_x"]), cli.parse_command_candidates("/addkey\u200bCline01 sk_x"))
        self.assertIn(("addkey", ["Cline-03", "sk_x"]), cli.parse_command_candidates("/add\u200bkey Cline-03 sk_x"))
        self.assertEqual(len(cli.parse_command_candidates("/keys")), 1)

    def test_full_width_slash(self):
        self.assertEqual(cli.parse_command_tokens("／status"), ("status", []))
        self.assertEqual(cli.parse_command_tokens("／addkey Cline01 sk_x"), ("addkey", ["Cline01", "sk_x"]))

    def test_code_block_wrapper(self):
        self.assertEqual(
            cli.parse_command_tokens("`/addkey Cline01 sk_x`"), ("addkey", ["Cline01", "sk_x"])
        )
        self.assertEqual(cli.parse_command_tokens("``/status``"), ("status", []))

    def test_bot_mention_is_stripped(self):
        self.assertEqual(cli.parse_command_tokens("/addkey@MyBot a sk_x"), ("addkey", ["a", "sk_x"]))

    def test_command_name_is_lowercased(self):
        self.assertEqual(cli.parse_command_tokens("/ADDKEY a sk_x")[0], "addkey")

    def test_junk_input(self):
        for text in ("", "   ", "在吗", "``"):
            name, args = cli.parse_command_tokens(text)
            self.assertEqual(args, [], text)
        self.assertEqual(cli.parse_command_tokens("在吗")[0], "在吗")

    def test_suspicious_chars_are_named(self):
        self.assertEqual(
            cli.suspicious_chars("/addkey\u3000Cline01 sk_x"),
            ["全角空格 U+3000"],
        )
        self.assertEqual(cli.suspicious_chars("／addkey"), ["全角斜杠 U+FF0F"])
        self.assertEqual(cli.suspicious_chars("/addkey Cline01 sk_x"), [])

    def test_suspicious_chars_are_deduplicated(self):
        self.assertEqual(cli.suspicious_chars("/a\u200b\u200bb"), ["零宽空格 U+200B"])

    def test_normalize_does_not_touch_the_key(self):
        text = cli.normalize_command_text("／addkey\u3000Cline-01 sk_05b0abcdef123456")
        self.assertIn("sk_05b0abcdef123456", text)


# ==================== API Key 清洗 ====================
class TestNormalizeApiKey(unittest.TestCase):
    def test_long_key_is_fine(self):
        """`sk_` + 59 位很正常，长度不设上限。"""
        key = "sk_" + "a" * 59
        self.assertEqual(cli.normalize_api_key(key), (key, False))

    def test_even_longer_key_is_fine(self):
        key = "sk_" + "9" * 200
        cleaned, changed = cli.normalize_api_key(key)
        self.assertEqual(cleaned, key)
        self.assertFalse(changed)

    def test_zero_width_chars_are_removed(self):
        cleaned, changed = cli.normalize_api_key("sk_abc\u200bdef\ufeffghi")
        self.assertEqual(cleaned, "sk_abcdefghi")
        self.assertTrue(changed)

    def test_surrounding_whitespace_is_trimmed_without_flag(self):
        """首尾空白本来就会被 strip，不算"清理掉了不可见字符"。"""
        cleaned, changed = cli.normalize_api_key("  sk_abcdefghijk  ")
        self.assertEqual(cleaned, "sk_abcdefghijk")
        self.assertFalse(changed)

    def test_empty_input(self):
        self.assertEqual(cli.normalize_api_key(""), ("", False))


# ==================== 日志脱敏（再导出） ====================
class TestRedaction(unittest.TestCase):
    def test_telegram_token_in_url_is_redacted(self):
        text = "HTTP Request: POST https://api.telegram.org/bot8123456789:AAHsecretsecretsecret123/getUpdates"
        out = cli.redact(text)
        self.assertNotIn("AAHsecretsecretsecret123", out)
        self.assertIn("bot<TOKEN>", out)

    def test_api_key_is_redacted(self):
        out = cli.redact("已保存 sk_05b0abcdef123456 成功")
        self.assertNotIn("05b0abcdef123456", out)
        self.assertIn("sk_<KEY>", out)

    def test_plain_text_is_untouched(self):
        self.assertEqual(cli.redact("普通日志 123"), "普通日志 123")
        self.assertEqual(cli.redact(""), "")

    def test_email_is_redacted(self):
        out = cli.redact("账号 someone@example.com 订阅到期")
        self.assertNotIn("someone@example.com", out)
        self.assertIn("***@***", out)

    def test_bare_token_in_exception_message_is_redacted(self):
        """PTB 的 InvalidToken 会把 Token 原样写进异常消息里。"""
        out = cli.redact("The token `8999999999:AAFaketokenfortesting1234567890` was rejected")
        self.assertNotIn("AAFaketokenfortesting1234567890", out)
        self.assertIn("bot<TOKEN>", out)

    def test_filter_rewrites_the_record(self):
        record = logging.LogRecord(
            "httpx",
            logging.INFO,
            __file__,
            1,
            "url=%s",
            ("https://api.telegram.org/bot123456789:AAHsecretsecretsecret123/x",),
            None,
        )
        self.assertTrue(cli.RedactingFilter().filter(record))
        message = record.getMessage()
        self.assertIn("bot<TOKEN>", message)
        self.assertNotIn("AAHsecretsecretsecret123", message)

    def test_filter_redacts_exc_text(self):
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", (), None)
        record.exc_text = "RuntimeError: The token `8999999999:AAFaketokenfortesting1234567890` was rejected"
        self.assertTrue(cli.RedactingFilter().filter(record))
        self.assertNotIn("AAFaketokenfortesting1234567890", record.exc_text)
        self.assertIn("bot<TOKEN>", record.exc_text)


# ==================== 版本 ====================
class TestVersion(unittest.TestCase):
    def test_version_is_semver(self):
        self.assertRegex(cli.__version__, r"^\d+\.\d+\.\d+$")

    def test_panel_body_has_no_module_title(self):
        """正文不写模块标题/版本号：面包屑与底部时间戳由 PanelManager 统一加，写两遍就是重复。"""
        text = cli.render_panel(
            [cli.Snapshot("a", "k")], now=datetime(2026, 9, 25, tzinfo=timezone.utc)
        )
        self.assertNotIn("ClinePass Status Panel", text)
        self.assertNotIn(f"v{cli.__version__}", text)
        self.assertNotIn("更新时间", text)
        self.assertIn("账号/别名", text, "正文第一行就该是内容")


# ==================== /addkey 参数拆分 ====================
class TestAliasSplit(unittest.TestCase):
    def test_alias_allows_hyphen_dot_underscore(self):
        for alias in ("主账号", "Cline-01", "cline_01", "Cline.01", "01", "a" * 24, "Cline 01"):
            self.assertEqual(cli.sanitize_alias(alias), alias, alias)

    def test_alias_rejects_bad_input(self):
        for alias in ("", "   ", "a" * 25, "-bad", "Cline#01", "Cline/01", "Cline:01", "主账号（备用）", "🚀"):
            self.assertIsNone(cli.sanitize_alias(alias), alias)

    def test_last_token_is_the_key(self):
        self.assertEqual(
            cli.split_alias_and_key(["Cline-01", "sk_1234567890"]),
            ("Cline-01", "sk_1234567890"),
        )

    def test_multiword_alias_is_joined_back(self):
        """Telegram 按空白切参数，多词别名要能拼回来。"""
        self.assertEqual(
            cli.split_alias_and_key(["Cline", "01", "sk_1234567890"]),
            ("Cline 01", "sk_1234567890"),
        )

    def test_needs_at_least_two_tokens(self):
        for args in ([], ["onlyalias"], ["", "   "]):
            alias, key = cli.split_alias_and_key(args)
            self.assertEqual(key, "", args)
            self.assertIsNone(alias, args)

    def test_whitespace_is_trimmed(self):
        self.assertEqual(
            cli.split_alias_and_key([" 主账号 ", " sk_1234567890 "]),
            ("主账号", "sk_1234567890"),
        )

    def test_oversized_joined_alias_is_rejected_key_kept(self):
        alias, key = cli.split_alias_and_key(["a" * 20, "b" * 10, "sk_1234567890"])
        self.assertIsNone(alias)
        self.assertEqual(key, "sk_1234567890")


# ==================== Settings（默认拒绝 + 继承全局） ====================
class TestSettings(unittest.TestCase):
    def test_defaults_and_bad_values(self):
        s = cli.Settings.from_env({})
        self.assertEqual(s.api_base, "https://api.cline.bot")
        self.assertEqual(s.max_keys_per_user, 10)
        self.assertFalse(s.demo_mode)
        # 安全修复：旧的「白名单留空 = 全放开」必须消失
        self.assertFalse(s.is_allowed(123))

        s = cli.Settings.from_env(
            {
                "MAX_KEYS_PER_USER": "not-a-number",
                "REQUEST_TIMEOUT": "abc",
                "DEMO_MODE": "true",
                "ALLOWED_USER_IDS": "1, 2; x 3",
                "CLINEPASS_API_BASE": "https://example.com/",
            }
        )
        self.assertEqual(s.max_keys_per_user, 10)
        self.assertEqual(s.request_timeout, 12.0)
        self.assertTrue(s.demo_mode)
        self.assertEqual(s.allowed_user_ids, frozenset({1, 2, 3}))
        self.assertEqual(s.api_base, "https://example.com")
        self.assertTrue(s.is_allowed(2))
        self.assertFalse(s.is_allowed(99))

    def test_clamping(self):
        s = cli.Settings.from_env({"HTTP_RETRIES": "99", "MAX_PARALLEL": "0", "MESSAGE_LIMIT": "99999"})
        self.assertEqual(s.http_retries, 5)
        self.assertEqual(s.max_parallel, 1)
        self.assertEqual(s.message_limit, 4096)

    def test_default_deny_even_with_explicit_empty_whitelist(self):
        for raw in ("", "   ", ",,", "x, y"):
            s = cli.Settings.from_env({"ALLOWED_USER_IDS": raw})
            self.assertEqual(s.allowed_user_ids, frozenset(), raw)
            self.assertFalse(s.is_allowed(1), raw)
        self.assertFalse(cli.Settings.from_env({}).is_allowed(None))

    def test_acl_is_the_real_authority(self):
        self.assertFalse(ACL(frozenset()).can(1, "cline"))  # 默认拒绝
        allowed = ACL(frozenset({1}))
        self.assertTrue(allowed.can(1, "cline"))
        self.assertFalse(allowed.can(2, "cline"))

    def test_inherits_defaults_from_global_settings(self):
        global_settings = GlobalSettings(
            config_file=Path("/tmp/mtbots-data/config.json"),
            demo_mode=True,
            show_identity=True,
        )
        s = cli.Settings.from_env({}, global_settings=global_settings)
        self.assertEqual(s.config_file, "/tmp/mtbots-data/config.json")
        self.assertTrue(s.demo_mode)
        self.assertTrue(s.show_identity)

        # 自己的环境变量优先级更高
        s2 = cli.Settings.from_env(
            {"CONFIG_FILE": "/tmp/other.json", "DEMO_MODE": "0", "SHOW_IDENTITY": "false"},
            global_settings=global_settings,
        )
        self.assertEqual(s2.config_file, "/tmp/other.json")
        self.assertFalse(s2.demo_mode)
        self.assertFalse(s2.show_identity)

    def test_fallback_config_file_is_a_json_path(self):
        s = cli.Settings.from_env({})
        self.assertTrue(s.config_file.endswith("config.json"), s.config_file)


# ==================== ModuleSpec 装配 ====================
def _command_names(app: FakeApp) -> set[str]:
    from telegram.ext import CommandHandler

    names: set[str] = set()
    for handler in app.handlers:
        if isinstance(handler, CommandHandler):
            names |= set(handler.commands)
    return names


class TestModuleSpecWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = make_core(self.tmp.name)

    def test_module_fields(self):
        self.assertEqual(MODULE.id, "cline")
        self.assertEqual(MODULE.icon, "🤖")
        self.assertEqual(MODULE.title, "Cline 额度")
        self.assertEqual(MODULE.description, "Cline/ClinePass 多账号额度面板")
        self.assertEqual(MODULE.callback_prefix, "c")
        self.assertIs(MODULE.register, cline_handlers.register)
        self.assertIsNone(MODULE.startup)
        for field_name in ("commands", "summary", "help_text", "id_lines", "open_panel", "show_status", "show_list"):
            self.assertIsNotNone(getattr(MODULE, field_name), field_name)

    def test_register_wires_expected_commands_only(self):
        """`/c_status`、`/c_list` 由 router 的永久别名注册（模块再注册就重复了）。"""
        app = FakeApp()
        MODULE.register(app, self.core)
        names = _command_names(app)
        self.assertEqual(names, {"quota", "addkey", "delkey", "keys", "clear"})
        for forbidden in (
            "start",
            "help",
            "status",
            "list",
            "menu",
            "home",
            "cancel",
            "jobs",
            "id",
            "c_status",
            "c_list",
        ):
            self.assertNotIn(forbidden, names)

    def test_register_wires_c_callback_namespace(self):
        from telegram.ext import CallbackQueryHandler

        app = FakeApp()
        MODULE.register(app, self.core)
        patterns = [h.pattern for h in app.handlers if isinstance(h, CallbackQueryHandler)]
        self.assertEqual(len(patterns), 1)
        self.assertTrue(re.search(patterns[0], "c|refresh"))
        self.assertIsNone(re.search(patterns[0], "d|page"))
        self.assertIsNone(re.search(patterns[0], "nav|home"))

    def test_commands_help_and_rescue_are_wired(self):
        names = [name for name, _ in MODULE.commands(self.core, 1)]
        for expected in ("c_status", "quota", "addkey", "delkey", "keys", "clear"):
            self.assertIn(expected, names)
        help_text = MODULE.help_text(self.core, 1)
        self.assertIn("/c_status", help_text)
        self.assertIn("/addkey", help_text)
        for key in ("start", "help", "id", "status", "quota", "addkey", "delkey", "keys", "clear"):
            self.assertIn(key, MODULE.rescue)

    def test_summary_uses_cache_and_never_calls_http(self):
        state = inject_state(self.core, self.tmp.name)
        text = asyncio.run(MODULE.summary(self.core, 7))
        self.assertIn("未绑定 Key", text)

        state.store.add(7, "主账号", "sk_" + "a" * 64)
        text = asyncio.run(MODULE.summary(self.core, 7))
        self.assertIn("1 个 Key", text)
        self.assertIn("点击进入", text)

        state.snapshots[7] = [
            cli.Snapshot("主账号", "sk_a…aaaa", windows=[cli.Window("本周额度", 57.0)])
        ]
        # 单个 Key：压成一行
        one = asyncio.run(MODULE.summary(self.core, 7))
        self.assertEqual(one, "🤖 Cline · 1 个 Key · 主账号 周 57%")
        self.assertNotIn("\n", one)

    def test_summary_lists_every_key_on_its_own_line(self):
        """多个 Key：首行报总数与正常/失败，再一 Key 一行（首页直接看额度）。"""
        state = inject_state(self.core, self.tmp.name)
        for alias in ("k1", "k2", "k3"):
            state.store.add(7, alias, "sk_" + alias * 8 + "x" * 40)
        state.snapshots[7] = [
            cli.Snapshot(
                "k1",
                "sk_…1",
                windows=[cli.Window("5 小时额度", 15.0), cli.Window("本周额度", 30.0), cli.Window("本月额度", 20.0)],
            ),
            cli.Snapshot("k2", "sk_…2", windows=[cli.Window("本周额度", 88.0)]),
            cli.Snapshot("k3", "sk_…3", warnings=["❌ 无权限"]),  # 没有任何可用窗口 = 失败
        ]
        lines = asyncio.run(MODULE.summary(self.core, 7)).splitlines()
        self.assertEqual(lines[0], "🤖 Cline · 3 个 Key（正常 2 · 失败 1）")
        self.assertEqual(lines[1], "• k1 · 5时 15% / 周 30% / 月 20%")
        self.assertEqual(lines[2], "• k2 · 周 88%")
        self.assertEqual(lines[3], "• k3 · ⚠️ 无额度数据")

    def test_summary_never_mixes_counts_with_stale_snapshots(self):
        """计数来自 store、明细来自快照缓存：两者对不上时必须退回一行，不能自相矛盾。"""
        state = inject_state(self.core, self.tmp.name)
        for alias in ("k1", "k2"):
            state.store.add(7, alias, "sk_" + alias * 4 + "x" * 50)
        state.snapshots[7] = [
            cli.Snapshot("k1", "sk_…1", windows=[cli.Window("本周额度", 10.0)]),
            cli.Snapshot("k2", "sk_…2", windows=[cli.Window("本周额度", 20.0)]),
            cli.Snapshot("k3", "sk_…3", windows=[cli.Window("本周额度", 30.0)]),  # 已删的 Key
        ]
        lines = asyncio.run(MODULE.summary(self.core, 7)).splitlines()
        self.assertNotIn("k3", "\n".join(lines), "已删的 Key 不能再出现在首页")
        self.assertEqual(len(lines), 3, "2 个 Key + 1 行标题")
        self.assertIn("2 个 Key", lines[0])

        # 新加的 Key 还没有快照：只能给一行，不能拿旧的凑数
        state.store.add(7, "k4", "sk_" + "d" * 60)
        fresh = asyncio.run(MODULE.summary(self.core, 7))
        self.assertEqual(fresh, "🤖 Cline · 3 个 Key · 点击进入")

    def test_delkey_drops_cached_snapshots(self):
        """删掉一个 Key 后不能继续显示它的额度（哪怕 alias 过滤也救不了计数口径）。"""
        state = inject_state(self.core, self.tmp.name)
        state.store.add(1, "k1", "sk_" + "a" * 60)
        state.snapshots[1] = [cli.Snapshot("k1", "sk_…", windows=[cli.Window("本周额度", 10.0)])]

        replies: list[str] = []

        async def reply_text(text, **kwargs):
            replies.append(text)

        message = SimpleNamespace(reply_text=reply_text)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            effective_message=message,
            effective_chat=SimpleNamespace(id=1, type="private"),
            callback_query=None,
        )
        context = SimpleNamespace(
            args=["k1"], application=SimpleNamespace(bot_data={"core": self.core})
        )
        asyncio.run(cline_handlers.cmd_delkey(update, context))
        self.assertTrue(any("已删除别名" in r for r in replies), replies)
        self.assertNotIn(1, state.snapshots, "删 Key 后旧快照必须作废")

    def test_addkey_drops_cached_snapshots(self):
        """给同一个别名换 Key 后，旧额度不能顶在新 Key 头上。"""
        state = inject_state(self.core, self.tmp.name)
        state.store.add(1, "k1", "sk_" + "a" * 60)
        state.snapshots[1] = [cli.Snapshot("k1", "sk_…", windows=[cli.Window("本周额度", 10.0)])]

        replies: list[str] = []

        async def reply_text(text, **kwargs):
            replies.append(text)

        async def delete():
            return None

        message = SimpleNamespace(reply_text=reply_text, delete=delete)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            effective_message=message,
            effective_chat=SimpleNamespace(id=1, type="private"),
            callback_query=None,
        )
        sent: list[str] = []

        async def send_message(chat_id, text, **kwargs):
            sent.append(text)

        context = SimpleNamespace(
            args=["k1", "sk_" + "b" * 60],
            bot=SimpleNamespace(send_message=send_message),
            application=SimpleNamespace(bot_data={"core": self.core}),
        )
        asyncio.run(cline_handlers.cmd_addkey(update, context))
        self.assertTrue(replies or sent, "addkey 至少要有回执")
        self.assertNotIn(1, state.snapshots, "换 Key 后旧快照必须作废")

    def test_refresh_fetches_and_caches_snapshots(self):
        """首页刷新会把额度查一遍并缓存；summary 只读这份缓存。"""
        state = inject_state(self.core, self.tmp.name)
        state.store.add(7, "主账号", "sk_" + "a" * 64)
        calls: list[list[tuple[str, str]]] = []

        async def fake_fetch(items):
            calls.append(list(items))
            return [cli.Snapshot("主账号", "sk_a…aaaa", windows=[cli.Window("本周额度", 12.0)])]

        with mock.patch.object(state.client, "fetch_all", side_effect=fake_fetch):
            asyncio.run(MODULE.refresh(self.core, 7))
        self.assertEqual(calls, [[("主账号", "sk_" + "a" * 64)]])
        self.assertIn("周 12%", asyncio.run(MODULE.summary(self.core, 7)))

    def test_refresh_without_keys_clears_snapshots(self):
        state = inject_state(self.core, self.tmp.name)
        state.snapshots[7] = [cli.Snapshot("旧", "sk_…")]
        asyncio.run(MODULE.refresh(self.core, 7))
        self.assertNotIn(7, state.snapshots)

    def test_refresh_skips_when_a_fetch_is_already_running(self):
        """面板手动刷新正在查时，首页刷新不再叠加一轮（一个 Key 要打 3 个接口）。

        走的是**额度查询锁**（`fetch_lock_for`），不是 Key 存储那把锁。
        """
        state = inject_state(self.core, self.tmp.name)
        state.store.add(7, "主账号", "sk_" + "a" * 64)
        called: list[int] = []

        async def fake_fetch(items):
            called.append(1)
            return []

        async def scenario():
            async with state.fetch_lock_for(7):
                with mock.patch.object(state.client, "fetch_all", side_effect=fake_fetch):
                    await MODULE.refresh(self.core, 7)

        asyncio.run(scenario())
        self.assertEqual(called, [])

    def test_refresh_does_not_take_the_key_storage_lock(self):
        """额度查询不能占着 Key 存储的写锁跑网络：否则一轮查询期间 /addkey 全被堵住。"""
        state = inject_state(self.core, self.tmp.name)
        state.store.add(7, "主账号", "sk_" + "a" * 64)
        self.assertIsNot(state.lock_for(7), state.fetch_lock_for(7))
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_fetch(items):
            started.set()
            await release.wait()
            return []

        async def scenario():
            with mock.patch.object(state.client, "fetch_all", side_effect=slow_fetch):
                task = asyncio.ensure_future(MODULE.refresh(self.core, 7))
                await started.wait()
                # 查询还在飞：此时拿存储锁必须立刻成功（不是等查询结束）
                async with state.lock_for(7):
                    await asyncio.to_thread(state.store.add, 7, "第二把", "sk_" + "b" * 64)
                release.set()
                await task

        asyncio.run(scenario())
        self.assertIn("第二把", state.store.keys(7))

    def test_refresh_is_registered_with_a_longer_ttl_than_docker(self):
        """额度接口比本地扫描金贵：首页刷新的 TTL 必须更长。"""
        from mtbots.features.docker import MODULE as docker_module

        self.assertIsNotNone(MODULE.refresh)
        self.assertGreater(MODULE.refresh_ttl, docker_module.refresh_ttl)

    def test_summary_reports_broken_storage_without_crashing(self):
        injection_core = self.core
        state = inject_state(injection_core, self.tmp.name)
        with mock.patch.object(cli.ConfigStore, "keys", side_effect=cli.ConfigError("boom")):
            text = asyncio.run(MODULE.summary(injection_core, 7))
        self.assertIn("存储不可用", text)
        self.assertIsInstance(state, cline_handlers.ClineState)

    def test_id_lines_contain_no_key_material(self):
        secret = "sk_" + "b" * 64
        state = inject_state(self.core, self.tmp.name)
        state.store.add(7, "主账号", secret)
        lines = asyncio.run(MODULE.id_lines(self.core, 7))
        joined = "\n".join(lines)
        self.assertIn("已绑定：1 个", joined)
        self.assertIn("config.json", joined)
        self.assertIn("存储：✅ 可读写", joined)
        self.assertNotIn(secret, joined)
        self.assertNotIn("主账号", joined)  # 连别名也不给（/id 是公开面板）

    def test_rescue_handlers_are_coroutine_functions(self):
        for name, handler in MODULE.rescue.items():
            self.assertTrue(asyncio.iscoroutinefunction(handler), name)


# ==================== 公开 API 契约 ====================
class TestPublicApiSurface(unittest.TestCase):
    """``docs/porting-contract.md`` §7.3 要求保留的公开名字一个都不能少。"""

    REQUIRED = (
        "Settings",
        "ConfigStore",
        "ClinePassClient",
        "Cooldown",
        "ApiError",
        "ConfigError",
        "KeyLimitError",
        "Window",
        "Snapshot",
        "parse_limits_list",
        "parse_usage",
        "parse_timestamp",
        "describe_reset",
        "humanize_delta",
        "progress_bar",
        "esc",
        "render_snapshot",
        "render_panel",
        "split_message",
        "mask_key",
        "key_fingerprint",
        "key_shape_note",
        "normalize_api_key",
        "sanitize_alias",
        "split_alias_and_key",
        "parse_command_tokens",
        "parse_command_candidates",
        "suspicious_chars",
        "normalize_command_text",
        "redact",
        "RedactingFilter",
        "__version__",
    )

    def test_every_required_name_exists(self):
        for name in self.REQUIRED:
            self.assertTrue(hasattr(cli, name), name)

    def test_redaction_helpers_are_the_shared_ones(self):
        from mtbots.logging_setup import RedactingFilter as shared_filter
        from mtbots.logging_setup import redact as shared_redact

        self.assertIs(cli.redact, shared_redact)
        self.assertIs(cli.RedactingFilter, shared_filter)


# ==================== handler 行为（假 Telegram 对象，不联网） ====================
class FakeMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.deleted = False
        self.replies: list[str] = []
        self.edits: list[str] = []

    async def delete(self):
        self.deleted = True

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return self

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)
        return self


class FakeBot:
    def __init__(self):
        self.messages: list[FakeMessage] = []

    async def send_message(self, chat_id, text, **kwargs):
        message = FakeMessage(text)
        self.messages.append(message)
        return message


class FakeUser:
    def __init__(self, user_id: int = 1):
        self.id = user_id
        self.first_name = "Tester"


class FakeChat:
    def __init__(self, chat_id: int = 555, chat_type: str = "private"):
        self.id = chat_id
        self.type = chat_type


class FakeUpdate:
    def __init__(self, message=None, chat_type: str = "private", user_id: int = 1, bot=None):
        self.effective_message = message
        self.effective_user = FakeUser(user_id)
        self.effective_chat = FakeChat(chat_type=chat_type)
        self.callback_query = None
        self._bot = bot

    def get_bot(self):
        if self._bot is None:
            raise RuntimeError("no bot associated")
        return self._bot


class FakeApplication:
    def __init__(self, core):
        self.bot_data = {"core": core}


class FakeContext:
    """假 CallbackContext：cmd_* handler 用 core_of(context) 取 Core。"""

    def __init__(self, args=(), core=None):
        self.args = list(args)
        self.bot = FakeBot()
        self.application = FakeApplication(core) if core is not None else None


class CapturingPanels:
    """只记录渲染/发送调用，不碰 Telegram。"""

    def __init__(self):
        self.rendered: list[tuple] = []
        self.sent: list[tuple] = []

    async def render(self, module_id, update, text, keyboard=None, **kwargs):
        self.rendered.append((module_id, text, keyboard, kwargs))

    async def send(self, chat_id, text, keyboard=None, *, bot=None, **kwargs):
        self.sent.append((chat_id, text))
        return FakeMessage(text)


class FakeClineClient:
    """返回固定快照的假客户端（证明面板路径不需要网络）。"""

    def __init__(self, windows=None):
        self.windows = windows if windows is not None else [cli.Window("本周额度", 57.0)]
        self.calls = 0

    async def fetch_all(self, items):
        self.calls += 1
        return [cli.Snapshot(alias, cli.mask_key(key), windows=list(self.windows)) for alias, key in items]


class TestHandlersWithFakes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = make_core(self.tmp.name)
        self.core.panels = CapturingPanels()
        self.state = inject_state(self.core, self.tmp.name)

    # ---- /quota、/c_status ----
    def test_show_status_queries_then_renders_the_panel(self):
        secret = "sk_" + "a" * 64
        self.state.store.add(1, "主账号", secret)
        self.state.client = FakeClineClient()
        update, context = FakeUpdate(FakeMessage()), FakeContext()

        asyncio.run(cline_handlers.show_status(self.core, update, context))

        # 占位消息发出后又删掉，结果只进面板（不刷屏）
        self.assertEqual(len(context.bot.messages), 1)
        self.assertIn("正在查询 1 个账号", context.bot.messages[0].text)
        self.assertTrue(context.bot.messages[0].deleted)
        self.assertEqual(self.state.client.calls, 1)

        module_id, text, keyboard, _kwargs = self.core.panels.rendered[0]
        self.assertEqual(module_id, "cline")
        self.assertIn("主账号", text)
        self.assertIn("本周额度", text)
        self.assertNotIn(secret, text)  # 面板里永远没有明文
        callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
        self.assertIn("c|refresh", callbacks)

    def test_show_status_without_keys_never_touches_the_api(self):
        self.state.client = FakeClineClient()
        message = FakeMessage()
        asyncio.run(cline_handlers.show_status(self.core, FakeUpdate(message), FakeContext()))
        self.assertEqual(self.state.client.calls, 0)
        self.assertTrue(any("还没有绑定任何 Key" in r for r in message.replies))
        self.assertEqual(self.core.panels.rendered, [])

    def test_overflow_panel_is_chunked_before_the_last_panel(self):
        """面板本身超长时：前面的分片先发，最后一片仍可原地刷新。"""
        settings = cli.Settings(
            config_file=self.state.settings.config_file, message_limit=600, max_keys_per_user=100
        )
        self.state.settings = settings
        self.state.store = cli.ConfigStore(settings.config_file, 100)
        for i in range(40):
            self.state.store.add(1, f"acc{i}", "sk_" + "a" * 64)
        self.state.client = FakeClineClient()

        asyncio.run(
            cline_handlers.show_status(self.core, FakeUpdate(FakeMessage(), bot=FakeBot()), FakeContext())
        )

        self.assertGreater(len(self.core.panels.sent), 0)  # 溢出的分片确实发出去了
        self.assertEqual(len(self.core.panels.rendered), 1)  # 最后一片是可刷新的面板
        joined = "".join(text for _chat_id, text in self.core.panels.sent) + self.core.panels.rendered[0][1]
        self.assertIn("acc39", joined)
        self.assertTrue(all(len(text) <= 700 for _chat_id, text in self.core.panels.sent))

    def test_cooldown_blocks_the_second_query(self):
        self.state.store.add(1, "主账号", "sk_" + "a" * 64)
        self.state.client = FakeClineClient()
        self.state.cooldown = cli.Cooldown(60.0)

        first = FakeMessage()
        asyncio.run(cline_handlers.show_status(self.core, FakeUpdate(first), FakeContext()))
        second = FakeMessage()
        asyncio.run(cline_handlers.show_status(self.core, FakeUpdate(second), FakeContext()))

        self.assertEqual(self.state.client.calls, 1)
        self.assertTrue(any("操作太快了" in r for r in second.replies))

    # ---- 权限 ----
    def test_unlisted_user_is_refused(self):
        self.core.acl = ACL(frozenset())  # 默认拒绝
        message = FakeMessage()
        asyncio.run(cline_handlers.show_status(self.core, FakeUpdate(message), FakeContext()))
        self.assertTrue(any("白名单" in r for r in message.replies))
        self.assertEqual(self.core.panels.rendered, [])

    def test_key_command_is_refused_outside_private_chat(self):
        secret = "sk_" + "b" * 64
        message = FakeMessage(f"/addkey 主账号 {secret}")
        context = FakeContext(["主账号", secret], core=self.core)
        asyncio.run(
            cline_handlers.cmd_addkey(FakeUpdate(message, chat_type="group"), context)
        )
        self.assertTrue(any("私聊" in r for r in message.replies))
        self.assertEqual(self.state.store.keys(1), {})  # 群聊里绝不落盘

    # ---- /addkey ----
    def test_addkey_deletes_the_message_and_reports_mask_only(self):
        secret = "sk_" + "b" * 64
        message = FakeMessage(f"/addkey 主账号 {secret}")
        context = FakeContext(["主账号", secret], core=self.core)

        asyncio.run(cline_handlers.cmd_addkey(FakeUpdate(message), context))

        self.assertTrue(message.deleted)  # 含明文 Key 的消息必须被撤回
        joined = "\n".join(m.text for m in context.bot.messages)
        self.assertIn("已保存 Key", joined)
        self.assertIn("指纹", joined)
        self.assertNotIn(secret, joined)
        self.assertIn(cli.mask_key(secret, show_length=True), joined)
        self.assertIn(cli.key_fingerprint(secret), joined)
        self.assertEqual(self.state.store.keys(1), {"主账号": secret})

    def test_addkey_rejects_bad_alias(self):
        message = FakeMessage()
        context = FakeContext(["bad#alias", "sk_" + "c" * 64], core=self.core)
        asyncio.run(cline_handlers.cmd_addkey(FakeUpdate(message), context))
        joined = "\n".join(m.text for m in context.bot.messages)
        self.assertIn("别名不合法", joined)
        self.assertEqual(self.state.store.keys(1), {})

    def test_addkey_reports_key_limit(self):
        for i in range(self.state.settings.max_keys_per_user):
            self.state.store.add(1, f"k{i}", "sk_" + "d" * 64)
        context = FakeContext(["overflow", "sk_" + "e" * 64], core=self.core)
        asyncio.run(cline_handlers.cmd_addkey(FakeUpdate(FakeMessage()), context))
        joined = "\n".join(m.text for m in context.bot.messages)
        self.assertIn("最多保存", joined)

    # ---- /delkey、/clear ----
    def test_delkey_and_clear(self):
        self.state.store.add(1, "主账号", "sk_" + "f" * 64)
        message = FakeMessage()
        asyncio.run(cline_handlers.cmd_delkey(FakeUpdate(message), FakeContext(["主账号"], core=self.core)))
        self.assertTrue(any("已删除别名" in r for r in message.replies))
        self.assertEqual(self.state.store.keys(1), {})

        self.state.store.add(1, "主账号", "sk_" + "f" * 64)
        ask = FakeMessage()
        asyncio.run(cline_handlers.cmd_clear(FakeUpdate(ask), FakeContext(core=self.core)))
        self.assertTrue(any("确认请输入" in r for r in ask.replies))
        self.assertEqual(len(self.state.store.keys(1)), 1)  # 没有 confirm 就不动手

        done = FakeMessage()
        asyncio.run(cline_handlers.cmd_clear(FakeUpdate(done), FakeContext(["confirm"], core=self.core)))
        self.assertTrue(any("已清空 1 个 Key" in r for r in done.replies))
        self.assertEqual(self.state.store.keys(1), {})

    # ---- /keys 与面板回调 ----
    def test_keys_panel_is_masked(self):
        secret = "sk_" + "9" * 64
        self.state.store.add(1, "主账号", secret)
        asyncio.run(cline_handlers.show_list(self.core, FakeUpdate(FakeMessage()), FakeContext()))
        _module_id, text, _keyboard, _kwargs = self.core.panels.rendered[0]
        self.assertIn("主账号", text)
        self.assertIn(cli.key_fingerprint(secret), text)
        self.assertNotIn(secret, text)

    def test_router_alias_c_status_reaches_our_panel(self):
        """/c_status 由 router 的别名表注册 → 最终调用本模块的 show_status。"""
        from mtbots.router import alias_handler

        self.core.register(MODULE)  # 路由需要能在 core.modules 里找到 cline
        self.state.store.add(1, "主账号", "sk_" + "a" * 64)
        self.state.client = FakeClineClient()
        update = FakeUpdate(FakeMessage())
        context = FakeContext(core=self.core)

        asyncio.run(alias_handler("cline", "status")(update, context))

        self.assertEqual(self.core.panels.rendered[0][0], "cline")
        self.assertEqual(self.core.module_of_chat(update.effective_chat.id), "cline")

    def test_registered_handlers_are_coroutines(self):
        app = FakeApp()
        MODULE.register(app, self.core)
        for handler in app.handlers:
            self.assertTrue(asyncio.iscoroutinefunction(handler.callback), handler)


if __name__ == "__main__":
    unittest.main()
