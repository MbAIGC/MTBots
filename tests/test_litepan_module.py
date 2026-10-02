"""LitePan 模块单元测试（stdlib unittest，无网络、无真实 Telegram 连接）。

运行：
    PYTHONPATH=/root/DSH/MTBots/.vendor:/root/DSH/MTBots python3 -m unittest tests.test_litepan_module -v

覆盖：slug 构建（拼音 / 兜底 / 限长 24 / 重名 `_2`）、`UserProfile.from_dict` 校验、
`drives` 查表、`Discovery` 解析 canned JSON、`/refresh` 参数分类与路由、菜单片段预算、
回执渲染与分页。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mtbots.features.litepan import MODULE
from mtbots.features.litepan import discovery as lp_discovery
from mtbots.features.litepan import handlers as lp_handlers
from mtbots.features.litepan.config import ConfigError, LitePanConfig, UserProfile
from mtbots.features.litepan.discovery import Discovery, _fit_slug, _slugify
from tests.fakes import (
    FakeBot,
    FakeChat,
    FakeContext,
    FakeQuery,
    FakeUpdate,
    FakeUser,
    add_fake_module,
    make_core,
)


# ==================== 测试替身 ====================
class FakeAdminClient:
    """只实现 Discovery 需要的那一个方法，喂 canned JSON。"""

    def __init__(self, accounts, options, rules):
        self.accounts = accounts
        self.options = options
        self.rules = rules
        self.calls = []

    def admin_get(self, path):
        self.calls.append(path)
        if path == "/api/admin/accounts":
            return self.accounts
        if path == "/api/admin/automation/options":
            return self.options
        if path == "/api/admin/automation/rules":
            return self.rules
        raise AssertionError("未预期的接口：%s" % path)


def make_profile(**kwargs):
    data = dict(
        chat_ids=[1001],
        lite_url="http://litepan.local:8000",
        api_key="abcdefghijklmnop",
        admin_user="admin",
        admin_password="secret",
    )
    data.update(kwargs)
    return UserProfile(**data)


CANNED_ACCOUNTS = [
    {"id": 1, "name": "GY01"},
    {"id": 2, "name": "GY02"},
]

CANNED_OPTIONS = {
    "strm_tasks": [
        {"id": 11, "name": "刮削A", "account_id": 1},
        {"id": 12, "name": "刮削B", "account_id": 2},
    ],
    "organize_tasks": [
        {"id": 21, "name": "整理C", "account_id": 2},
    ],
}

CANNED_RULES = [
    {"id": 1, "name": "all", "trigger_type": "webhook", "trigger_config": {"event": "gy_all"},
     "actions": [{"type": "strm", "params": {"task_id": 11}}]},
    {"id": 2, "name": "GY01", "trigger_type": "webhook", "trigger_config": {"event": "gy01_refresh"},
     "actions": [{"type": "strm", "params": {"task_id": 11}}]},
    {"id": 3, "name": "GY02 org", "trigger_type": "webhook", "trigger_config": {"event": "gy02_org"},
     "actions": [{"type": "organize", "params": {"task_id": 21}}]},
    {"id": 4, "name": "multi", "trigger_type": "webhook", "trigger_config": {"event": "multi"},
     "actions": [{"type": "strm", "params": {"task_id": 11}},
                 {"type": "strm", "params": {"task_id": 12}}]},
    {"id": 5, "name": "ghost", "trigger_type": "webhook", "trigger_config": {"event": "ghost"},
     "actions": [{"type": "strm", "params": {"task_id": 999}}]},
    {"id": 6, "name": "cron-only", "trigger_type": "cron", "trigger_config": {"event": "ignored"},
     "actions": []},
    {"id": 7, "name": "no-event", "trigger_type": "webhook", "trigger_config": {}, "actions": []},
    {"id": 8, "name": "all", "trigger_type": "webhook", "trigger_config": {"event": "dup_all"},
     "actions": [{"type": "strm", "params": {"task_id": 11}}]},
]


def build_discovery():
    client = FakeAdminClient(CANNED_ACCOUNTS, CANNED_OPTIONS, CANNED_RULES)
    d = Discovery(make_profile(), client=client)
    d.fetch()
    return d


# ==================== slug ====================
class SlugTests(unittest.TestCase):
    def test_pinyin_slug_when_available(self):
        if not lp_discovery._PINYIN_AVAILABLE:
            self.skipTest("pypinyin 不可用")
        self.assertEqual(_slugify("剧集刮削"), "jujiguaxiao")

    def test_ascii_fallback_without_pinyin(self):
        with mock.patch.object(lp_discovery, "_PINYIN_AVAILABLE", False):
            self.assertEqual(_slugify("剧集刮削"), "")          # 纯中文无 ASCII -> 空
            self.assertEqual(_slugify("GY01 剧集"), "gy01")     # 只留 ASCII 部分
            self.assertEqual(_slugify("My-Rule.01"), "my_rule_01")

    def test_slug_length_cap_24(self):
        self.assertEqual(len(_fit_slug("a" * 40, set())), 24)
        self.assertEqual(_fit_slug("a" * 40, set()), "a" * 24)

    def test_duplicate_suffix(self):
        used = set()
        self.assertEqual(_fit_slug("abc", used), "abc")
        self.assertEqual(_fit_slug("abc", used), "abc_2")
        self.assertEqual(_fit_slug("abc", used), "abc_3")

    def test_duplicate_respects_cap(self):
        used = set()
        self.assertEqual(_fit_slug("b" * 24, used), "b" * 24)
        second = _fit_slug("b" * 24, used)
        self.assertEqual(len(second), 24)
        self.assertTrue(second.endswith("_2"))


# ==================== UserProfile ====================
class UserProfileTests(unittest.TestCase):
    def test_missing_url(self):
        with self.assertRaises(ConfigError):
            UserProfile.from_dict({"chat_ids": [1], "api_key": "k"})

    def test_missing_api_key(self):
        with self.assertRaises(ConfigError):
            UserProfile.from_dict({"chat_ids": [1], "litepan_url": "http://x"})

    def test_missing_chat_ids(self):
        with self.assertRaises(ConfigError):
            UserProfile.from_dict({"litepan_url": "http://x", "api_key": "k"})

    def test_duplicate_chat_ids(self):
        with self.assertRaises(ConfigError):
            UserProfile.from_dict(
                {"chat_ids": [7, 7], "litepan_url": "http://x", "api_key": "k"}
            )

    def test_bad_receipt_timeout_type(self):
        with self.assertRaises(ConfigError):
            UserProfile.from_dict(
                {"chat_ids": [1], "litepan_url": "http://x", "api_key": "k",
                 "receipt_timeout": "abc"}
            )

    def test_receipt_timeout_smaller_than_poll(self):
        with self.assertRaises(ConfigError):
            UserProfile.from_dict(
                {"chat_ids": [1], "litepan_url": "http://x", "api_key": "k",
                 "receipt_poll": 30, "receipt_timeout": 5}
            )

    def test_from_dict_all_fields(self):
        profile = UserProfile.from_dict(
            {
                "chat_ids": "1, 2，3",
                "litepan_url": "http://lite:8000/",
                "api_key": "abcdefghijklmnop",
                "default_event": "auto",
                "source": "webhook",
                "default_path": "/media",
                "message": "hello",
                "drives": {"剧集": "juji"},
                "admin_user": "admin",
                "admin_password": "pw",
                "lite_timeout": 30,
                "receipt_poll": 3,
                "receipt_timeout": 600,
                "show_url": True,
            }
        )
        self.assertEqual(profile.chat_ids, [1, 2, 3])
        self.assertEqual(profile.lite_url, "http://lite:8000")
        self.assertEqual(profile.default_event, "")  # "auto" 归一化为空
        self.assertEqual(profile.default_path, "/media")
        self.assertEqual(profile.message, "hello")
        self.assertEqual(profile.drives, {"剧集": "juji"})
        self.assertTrue(profile.receipt_enabled)
        self.assertEqual((profile.lite_timeout, profile.receipt_poll, profile.receipt_timeout), (30, 3, 600))
        self.assertTrue(profile.show_url)
        self.assertEqual(profile.masked_key(), "abcd****mnop")

    def test_single_chat_id_compat(self):
        profile = UserProfile.from_dict({"chat_id": 42, "litepan_url": "http://x", "api_key": "k"})
        self.assertEqual(profile.chat_ids, [42])
        self.assertFalse(profile.receipt_enabled)

    def test_from_env_same_variable_names(self):
        env = {
            "LITEPAN_URL": "http://lite:8000/",
            "LITEPAN_API_KEY": "abcdefghijklmnop",
            "LITEPAN_FALLBACK_EVENT": "my_event",
            "LITEPAN_SOURCE": "envsource",
            "LITEPAN_DEFAULT_PATH": "/data",
            "LITEPAN_MESSAGE": "m",
            "DRIVES": "剧集:juji,电影:movie",
            "LITEPAN_ADMIN_USER": "admin",
            "LITEPAN_ADMIN_PASSWORD": "pw",
            "LITEPAN_TIMEOUT": "20",
            "TG_RECEIPT_POLL_SECONDS": "3",
            "TG_RECEIPT_TIMEOUT_SECONDS": "60",
            "SHOW_LITEPAN_URL": "1",
        }
        profile = UserProfile.from_env(env)
        self.assertEqual(profile.chat_ids, [])
        self.assertEqual(profile.lite_url, "http://lite:8000")
        self.assertEqual(profile.default_event, "my_event")
        self.assertEqual(profile.source, "envsource")
        self.assertEqual(profile.drives, {"剧集": "juji", "电影": "movie"})
        self.assertTrue(profile.receipt_enabled)
        self.assertTrue(profile.show_url)
        self.assertEqual(profile.lite_timeout, 20)
        self.assertEqual(profile.receipt_poll, 3)
        self.assertEqual(profile.receipt_timeout, 60)

    def test_from_env_legacy_event_name(self):
        env = {
            "LITEPAN_URL": "http://lite",
            "LITEPAN_API_KEY": "k",
            "LITEPAN_EVENT": "old_event",
        }
        self.assertEqual(UserProfile.from_env(env).default_event, "old_event")

    def test_from_env_requires_url_and_key(self):
        with self.assertRaises(ConfigError):
            UserProfile.from_env({})


# ==================== drives ====================
class DrivesTests(unittest.TestCase):
    def test_lookup_is_case_insensitive_and_stripped(self):
        profile = make_profile(drives={"剧集": "juji", "Movie": "movie_ev"})
        self.assertEqual(profile.lookup_drive("剧集"), "juji")
        self.assertEqual(profile.lookup_drive("  movie "), "movie_ev")
        self.assertIsNone(profile.lookup_drive("不存在"))

    def test_empty_drives(self):
        profile = make_profile(drives={})
        self.assertIsNone(profile.lookup_drive("剧集"))
        self.assertEqual(profile.drive_list_text(), "（未配置盘名映射）")

    def test_drive_list_text(self):
        profile = make_profile(drives={"剧集": "juji"})
        self.assertIn("剧集 → juji", profile.drive_list_text())


# ==================== Discovery ====================
class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        # 关掉拼音，slug 才完全可预测（规则名用 ASCII）
        self._pinyin = mock.patch.object(lp_discovery, "_PINYIN_AVAILABLE", False)
        self._pinyin.start()
        self.addCleanup(self._pinyin.stop)
        self.d = build_discovery()

    def test_basic_parsing(self):
        self.assertEqual(self.d.accounts, {1: "GY01", 2: "GY02"})
        self.assertEqual(self.d.strm_tasks[11], {"name": "刮削A", "account_id": 1})
        self.assertEqual(self.d.organize_tasks[21], {"name": "整理C", "account_id": 2})
        # 非 webhook / 无 event 的规则被忽略：8 条里保留 6 条
        self.assertEqual([r["id"] for r in self.d.rules], [1, 2, 3, 4, 5, 8])
        first = self.d.rules[0]
        self.assertEqual(set(first), {"id", "name", "event", "tasks", "accounts", "slug"})
        self.assertEqual(first["event"], "gy_all")
        self.assertEqual(first["tasks"], ["刮削A"])
        self.assertEqual(first["accounts"], [1])

    def test_rule_slugs_and_duplicates(self):
        self.assertEqual(self.d.rule_by_slug["all"]["id"], 1)
        self.assertEqual(self.d.rule_by_slug["gy01"]["id"], 2)
        self.assertEqual(self.d.rule_by_slug["gy02_org"]["id"], 3)
        self.assertEqual(self.d.rule_by_slug["ghost"]["id"], 5)
        # 规则 8 也叫 all -> 重名加后缀（限长 24 内）
        self.assertEqual(self.d.rule_by_slug["all_2"]["id"], 8)

    def test_by_account_only_single_account_rules(self):
        self.assertEqual(self.d.by_account[1], {"gy_all", "gy01_refresh", "dup_all"})
        self.assertEqual(self.d.by_account[2], {"gy02_org"})
        self.assertNotIn(4, self.d.by_account)  # 多账号任务的规则不算单盘规则
        self.assertNotIn(5, self.d.by_account)  # 未知任务的规则不算

    def test_account_slugs_share_namespace_with_rules(self):
        # 规则 2 的 slug 已经是 gy01，账号 GY01 只能退到 gy01_2
        self.assertEqual(self.d.slugs, {"gy01_2": "GY01", "gy02": "GY02"})

    def test_account_rules_matching(self):
        ids = sorted(r["id"] for r in self.d.account_rules("GY01"))
        self.assertEqual(ids, [1, 2, 8])
        self.assertEqual([r["id"] for r in self.d.account_rules("gy02")], [3])
        self.assertEqual(self.d.account_rules("GY0"), [])  # 子串命中两个账号 -> 不猜
        self.assertEqual(self.d.account_events("GY01"), ["dup_all", "gy01_refresh", "gy_all"])
        self.assertEqual(self.d.account_events("GY02"), ["gy02_org"])

    def test_fetch_parses_without_pinyin_for_chinese_names(self):
        self.d.accounts[3] = "光鸭"
        self.d.by_account[3] = {"x"}
        self.d._build_slugs()
        self.assertEqual(self.d.slugs["pan1"], "光鸭")  # 纯中文 -> panN 兜底


# ==================== 自动发现缓存 ====================
class DiscoveryCacheTests(unittest.TestCase):
    def _state(self, ttl=60.0):
        state = lp_handlers.LitePanState(SimpleNamespace(), discovery_ttl=ttl)
        calls = []

        def spy(chat_id, profile):
            calls.append(chat_id)
            return None

        state.discovery = spy  # 覆盖真实实现：探测有没有真的发起发现
        return state, calls

    def test_failed_discovery_is_cached_for_ttl(self):
        state, calls = self._state()
        state.discovery_cache[1001] = (time.time(), None, True)
        self.assertIsNone(asyncio.run(lp_handlers.get_discovery(state, 1001, make_profile())))
        self.assertEqual(calls, [])  # 60s 内不再打 LitePan（缓存失败也是缓存）
        self.assertTrue(state.discovery_failed(1001))

    def test_fresh_success_is_cached(self):
        state, calls = self._state()
        sentinel = object()
        state.discovery_cache[1001] = (time.time(), sentinel, False)
        self.assertIs(asyncio.run(lp_handlers.get_discovery(state, 1001, make_profile())), sentinel)
        self.assertEqual(calls, [])

    def test_expired_cache_refetches(self):
        state, calls = self._state(ttl=0.0)
        state.discovery_cache[1001] = (time.time(), None, True)
        self.assertIsNone(asyncio.run(lp_handlers.get_discovery(state, 1001, make_profile())))
        self.assertEqual(calls, [1001])

    def test_discovery_disabled_without_admin(self):
        state, calls = self._state()
        profile = make_profile(admin_user="", admin_password="")
        self.assertIsNone(asyncio.run(lp_handlers.get_discovery(state, 1001, profile)))
        self.assertEqual(calls, [])


# ==================== /refresh 参数分类与路由 ====================
class RefreshArgTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(lp_handlers.classify_refresh_arg(""), ("empty", ""))
        self.assertEqual(lp_handlers.classify_refresh_arg("   "), ("empty", ""))
        self.assertEqual(lp_handlers.classify_refresh_arg("/data"), ("path", "/data"))
        self.assertEqual(lp_handlers.classify_refresh_arg("GY01"), ("name", "GY01"))
        self.assertEqual(lp_handlers.classify_refresh_arg("  光鸭-A "), ("name", "光鸭-A"))

    def test_fullwidth_slash_normalization(self):
        update = FakeUpdate(text="／refresh_gy01")
        self.assertEqual(lp_handlers._command_of(update), "/refresh_gy01")
        self.assertEqual(lp_handlers._arg_of(FakeUpdate(text="／refresh  光鸭-A")), "光鸭-A")


class RefreshRoutingTests(unittest.TestCase):
    """/refresh 的分支选择：全量规则 all 优先 / 全量回退 / 盘名 / DRIVES。"""

    def _run(self, arg, discovery, profile=None, account_rules=None):
        profile = profile or make_profile(default_event="")
        calls = []

        async def fake_get_discovery(state, chat_id, prof):
            return discovery

        async def fake_run_rules(ctx, rules):
            calls.append(("rules", [r["id"] for r in rules]))

        async def fake_trigger(ctx, event, source, path):
            calls.append(("event", event))

        async def fake_say(ctx, text, keyboard=None):
            calls.append(("say", text))

        if account_rules is not None and discovery is not None:
            discovery.account_rules = account_rules

        ctx = SimpleNamespace(state=SimpleNamespace(), profile=profile, chat_id=1)
        with mock.patch.object(lp_handlers, "get_discovery", fake_get_discovery), \
             mock.patch.object(lp_handlers, "run_rules_and_report", fake_run_rules), \
             mock.patch.object(lp_handlers, "trigger_and_report", fake_trigger), \
             mock.patch.object(lp_handlers, "say", fake_say):
            asyncio.run(lp_handlers.do_refresh(ctx, arg))
        return calls

    def test_empty_arg_prefers_all_rule(self):
        discovery = SimpleNamespace(
            rules=[
                {"id": 1, "slug": "all", "name": "all", "event": "e1"},
                {"id": 2, "slug": "gy01", "name": "GY01", "event": "e2"},
            ]
        )
        calls = self._run("", discovery)
        self.assertEqual(calls[-1], ("rules", [1]))            # 只触发全量规则
        self.assertIn("全量规则", calls[0][1])                 # 且先告知用户

    def test_empty_arg_falls_back_to_all_rules(self):
        discovery = SimpleNamespace(
            rules=[
                {"id": 2, "slug": "gy01", "name": "GY01", "event": "e2"},
                {"id": 3, "slug": "gy02", "name": "GY02", "event": "e3"},
            ]
        )
        self.assertEqual(self._run("", discovery)[0], ("rules", [2, 3]))

    def test_empty_arg_without_discovery_uses_default_event(self):
        profile = make_profile(default_event="tg_refresh", admin_user="", admin_password="")
        self.assertEqual(self._run("", None, profile=profile), [("event", "tg_refresh")])

    def test_empty_arg_without_discovery_and_no_event_warns(self):
        profile = make_profile(default_event="", admin_user="", admin_password="")
        calls = self._run("", None, profile=profile)
        self.assertEqual(calls[0][0], "say")

    def test_named_drive_uses_manual_mapping_first(self):
        profile = make_profile(drives={"光鸭-A": "manual_ev"})
        self.assertEqual(self._run("光鸭-A", None, profile=profile), [("event", "manual_ev")])

    def test_named_drive_falls_back_to_account_rules(self):
        rule = {"id": 9, "slug": "gy02", "name": "GY02", "event": "e9"}
        discovery = SimpleNamespace(rules=[rule])
        calls = self._run("GY02", discovery, account_rules=lambda name: [rule])
        self.assertEqual(calls[0], ("rules", [9]))

    def test_named_drive_not_found_hints(self):
        profile = make_profile(admin_user="", admin_password="")
        calls = self._run("不存在", None, profile=profile)
        self.assertEqual(calls[0][0], "say")
        self.assertIn("未找到盘名", calls[0][1])

    def test_path_arg_is_rejected(self):
        calls = self._run("/data", None)
        self.assertEqual(calls[0][0], "say")
        self.assertIn("路径参数已不再支持", calls[0][1])

    def test_slug_of_handles_wrappers_and_fullwidth(self):
        self.assertEqual(lp_handlers._slug_of(FakeUpdate(text="`/refresh_gy01`")), "gy01")
        self.assertEqual(lp_handlers._slug_of(FakeUpdate(text="／refresh_GY01")), "gy01")
        self.assertEqual(lp_handlers._slug_of(FakeUpdate(text="\u200b/refresh_all")), "all")
        self.assertEqual(lp_handlers._slug_of(FakeUpdate(text="/refresh")), "")
        self.assertEqual(lp_handlers._slug_of(FakeUpdate(text="/info")), "")


# ==================== 菜单片段预算 ====================
class MenuBudgetTests(unittest.TestCase):
    def test_budget_limits_dynamic_entries(self):
        rules = [("a", "A", 0, 1), ("b", "B", 5, 2), ("c", "C", 0, 3), ("d", "D", 1, 4)]
        entries = lp_handlers.menu_entries(lp_handlers.BASE_COMMANDS, rules, 2)
        self.assertEqual(len(entries), len(lp_handlers.BASE_COMMANDS) + 2)
        # 常用（usage 高）在前，其余按规则 ID 稳定排序
        self.assertEqual(entries[-2:], [("refresh_b", "B"), ("refresh_d", "D")])

    def test_zero_budget_keeps_base_only(self):
        rules = [("a", "A", 0, 1)]
        self.assertEqual(
            lp_handlers.menu_entries(lp_handlers.BASE_COMMANDS, rules, 0),
            lp_handlers.BASE_COMMANDS,
        )

    def test_base_commands_present(self):
        names = [name for name, _ in lp_handlers.BASE_COMMANDS]
        for expected in ("refresh", "info", "p_menu", "run", "strm"):
            self.assertIn(expected, names)

    def test_build_menu_entries_uses_profile_discovery(self):
        profile = make_profile()
        rule = {"id": 1, "name": "全量", "event": "e", "slug": "all"}
        fake_discovery = SimpleNamespace(rule_by_slug={"all": rule})

        async def fake_get_discovery(state, chat_id, prof):
            return fake_discovery

        state = SimpleNamespace(
            config=SimpleNamespace(all_profiles=lambda: [profile], menu_budget=1),
            usage={"all": 7},
        )
        with mock.patch.object(lp_handlers, "get_discovery", fake_get_discovery):
            entries = asyncio.run(lp_handlers.build_menu_entries(None, state))
        self.assertEqual(entries[-1], ("refresh_all", "全量"))


# ==================== 回执渲染 / 分页 ====================
class RenderTests(unittest.TestCase):
    def test_render_result_marks_steps(self):
        run = {
            "id": 7,
            "status": "failed",
            "message": "boom",
            "result": {"steps": [{"name": "刮削", "status": "success"},
                                 {"type": "整理", "status": "error"}]},
        }
        text = lp_handlers.render_result("规则A", run)
        self.assertIn("❌ 规则「规则A」执行完成", text)
        self.assertIn("运行ID：7", text)
        self.assertIn("消息：boom", text)
        self.assertIn("刮削✓ / 整理✗", text)

    def test_paginate_clamps(self):
        self.assertEqual(lp_handlers.paginate(list(range(7)), 2, 3), (2, 3, [3, 4, 5]))
        self.assertEqual(lp_handlers.paginate(list(range(7)), 99, 3)[0], 3)
        self.assertEqual(lp_handlers.paginate([], 5, 6), (1, 1, []))


# ==================== ModuleSpec ====================
class ModuleSpecTests(unittest.TestCase):
    def test_spec_contract(self):
        self.assertEqual(MODULE.id, "litepan")
        self.assertEqual(MODULE.icon, "🎬")
        self.assertEqual(MODULE.title, "LitePan 联动")
        self.assertEqual(MODULE.callback_prefix, "p")
        self.assertIs(MODULE.register, lp_handlers.register)
        self.assertIs(MODULE.startup, lp_handlers.startup)
        self.assertIs(MODULE.open_panel, lp_handlers.open_panel)
        self.assertIs(MODULE.show_status, lp_handlers.show_status)
        self.assertIs(MODULE.show_list, lp_handlers.show_list)
        self.assertIs(MODULE.check, lp_handlers.check)
        for name in ("refresh", "refresh_", "strm", "run", "info", "ping", "p_menu", "p_status", "p_list"):
            self.assertIn(name, MODULE.rescue)
        self.assertTrue(callable(MODULE.scope_chats))
        self.assertTrue(callable(MODULE.summary))
        self.assertTrue(callable(MODULE.id_lines))

    def test_commands_never_own_router_names(self):
        forbidden = {"start", "help", "status", "list", "menu", "home", "cancel", "jobs", "id"}
        names = {name for name, _ in lp_handlers.commands(None, 1)}
        self.assertFalse(names & forbidden)

    def test_summary_is_cache_only(self):
        class FakeJobs:
            def running(self, module=None):
                return []

            def recent(self, limit=1, module=None):
                return []

        core = SimpleNamespace(jobs=FakeJobs())
        self.assertEqual(asyncio.run(lp_handlers.summary(core, 1)), "🎬 LitePan · 点击进入")

    def test_check_is_offline_and_reports_missing_config(self):
        core = make_core()
        core.register(MODULE)
        lines = MODULE.check(core)
        self.assertTrue(lines)
        self.assertTrue(all(isinstance(line, str) for line in lines))
        self.assertTrue(any("LitePan" in line or "未找到" in line for line in lines))

    def test_check_reports_bound_profiles(self):
        tmp = tempfile.TemporaryDirectory(prefix="litepan-check-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        (base / "litepan-users.json").write_text(
            json.dumps({"users": [{"chat_ids": [1], "litepan_url": "http://lite",
                                   "api_key": "k", "admin_user": "a", "admin_password": "p"}]}),
            encoding="utf-8",
        )
        core = make_core(tmpdir=str(base))
        core.register(MODULE)
        text = "\n".join(MODULE.check(core))
        self.assertIn("1 个会话绑定", text)
        self.assertIn("1/1", text)


# ==================== 面板 / 回调 / 菜单集成（用 tests.fakes，无网络） ====================
IT_RULES = [
    {"id": 1, "name": "全量", "event": "gy_all", "tasks": ["刮削A"], "accounts": [1], "slug": "all"},
    {"id": 2, "name": "GY01", "event": "gy01_refresh", "tasks": ["刮削A"], "accounts": [1], "slug": "gy01"},
]


def make_fake_discovery(runs=None):
    runs = runs if runs is not None else [
        {"id": 9, "status": "success", "message": "done",
         "result": {"steps": [{"name": "刮削", "status": "success"}]}}
    ]
    client_cls = type(
        "FakeLitepanClient",
        (),
        {
            "__init__": lambda self, profile: setattr(self, "profile", profile),
            "health": lambda self: {},
            "max_run_id": lambda self: 5,
            "run_rule": lambda self, rid: {},
            "list_runs": lambda self, rid, limit=5: list(runs),
        },
    )
    discovery = SimpleNamespace(
        rules=IT_RULES,
        rule_by_slug={"all": IT_RULES[0], "gy01": IT_RULES[1]},
        slugs={},
        by_account={1: {"gy_all", "gy01_refresh"}},
        accounts={1: "GY01"},
        account_rules=lambda name: [IT_RULES[1]],
    )
    return discovery, client_cls


class PanelIntegrationTests(unittest.TestCase):
    """用 tests.fakes 把 open_panel / show_list / 回调 / 菜单跑通（不联网、不连 Telegram）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="litepan-it-")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        (base / "litepan-users.json").write_text(
            json.dumps(
                {
                    "users": [
                        {
                            "chat_ids": [123456789],
                            "litepan_url": "http://lite:8000",
                            "api_key": "k",
                            "admin_user": "a",
                            "admin_password": "p",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.core = make_core(tmpdir=str(base))
        self.core.register(MODULE)
        self.bot = FakeBot()
        self.update = FakeUpdate(user=FakeUser(123456789), chat=FakeChat(123456789), bot=self.bot)
        self.context = FakeContext(self.core, bot=self.bot)

    def _patches(self, discovery, client_cls):
        async def fake_get_discovery(state, chat_id, profile):
            return discovery

        return (
            mock.patch.object(lp_handlers, "get_discovery", fake_get_discovery),
            mock.patch.object(lp_handlers, "LitePanClient", client_cls),
        )

    @staticmethod
    async def _drain(core):
        state = lp_handlers.get_state(core)
        pending = [t for t in list(state.tasks) if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def test_open_panel_renders_info_with_rules_button(self):
        discovery, client_cls = make_fake_discovery()
        patch_a, patch_b = self._patches(discovery, client_cls)

        async def scenario():
            with patch_a, patch_b:
                await MODULE.open_panel(self.core, self.update, self.context)
                await self._drain(self.core)

        asyncio.run(scenario())
        self.assertIn("自动发现已开启", self.bot.last_text)
        callbacks = [b.callback_data for row in self.bot.last_markup.inline_keyboard for b in row]
        self.assertIn("p|rule_page|1", callbacks)

    def test_show_list_then_trigger_pushes_receipt(self):
        discovery, client_cls = make_fake_discovery()
        patch_a, patch_b = self._patches(discovery, client_cls)

        async def scenario():
            with patch_a, patch_b:
                await MODULE.show_list(self.core, self.update, self.context)
                query = FakeQuery("p|run_rule|1", user=FakeUser(123456789))
                update = FakeUpdate(
                    user=FakeUser(123456789), chat=FakeChat(123456789), bot=self.bot, query=query
                )
                await lp_handlers.on_callback(self.core, update, self.context)
                await self._drain(self.core)

        asyncio.run(scenario())
        texts = " ".join(s.text for s in self.bot.sent)
        self.assertIn("已提交执行规则：「全量」", texts)
        jobs = self.core.jobs.all_jobs()
        self.assertEqual(jobs[-1].status, "done")
        self.assertNotIn("执行中", jobs[-1].title)  # 完成后标题去掉「执行中」
        # 成功回执：下一步按钮是「再跑一次」，回调回到本模块
        markup = self.bot.sent[-1].kwargs["reply_markup"]
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("p|run_rule|1", callbacks)

    def test_failed_run_offers_docker_upgrade(self):
        """失败回执要给「去 Docker」的入口。

        本模块不再硬编一个假按钮（那个「⬆️ 升级 LitePan 容器」的 callback 其实就是打开
        Docker 面板），改由跨模块那一行按「已启用 + 有权限」自动列出 🐳。
        """
        add_fake_module(self.core, "docker")
        runs = [{"id": 9, "status": "failed", "message": "boom", "result": {}}]
        discovery, client_cls = make_fake_discovery(runs=runs)
        patch_a, patch_b = self._patches(discovery, client_cls)

        async def scenario():
            with patch_a, patch_b:
                query = FakeQuery("p|run_rule|1", user=FakeUser(123456789))
                update = FakeUpdate(
                    user=FakeUser(123456789), chat=FakeChat(123456789), bot=self.bot, query=query
                )
                await lp_handlers.on_callback(self.core, update, self.context)
                await self._drain(self.core)

        asyncio.run(scenario())
        markup = self.bot.sent[-1].kwargs["reply_markup"]
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("nav|open|docker", callbacks)
        self.assertNotIn("nav|jobs", callbacks, "任务中心不占这一行")
        self.assertNotIn("p|run_rule|1", callbacks, "失败时不给「再跑一次」")

    def test_success_receipt_offers_rerun_and_other_modules(self):
        """成功回执：本模块「再跑一次」+ 一行跨模块入口。"""
        add_fake_module(self.core, "docker")
        runs = [{"id": 9, "status": "success", "message": "ok", "result": {}}]
        discovery, client_cls = make_fake_discovery(runs=runs)
        patch_a, patch_b = self._patches(discovery, client_cls)

        async def scenario():
            with patch_a, patch_b:
                query = FakeQuery("p|run_rule|1", user=FakeUser(123456789))
                update = FakeUpdate(
                    user=FakeUser(123456789), chat=FakeChat(123456789), bot=self.bot, query=query
                )
                await lp_handlers.on_callback(self.core, update, self.context)
                await self._drain(self.core)

        asyncio.run(scenario())
        markup = self.bot.sent[-1].kwargs["reply_markup"]
        rows = [[b.callback_data for b in row] for row in markup.inline_keyboard]
        self.assertEqual(rows[0], ["p|run_rule|1"], "本模块动作在第一行")
        self.assertEqual(rows[1], ["nav|open|docker"], "跨模块入口在第二行")

    def test_unbound_chat_gets_explicit_message(self):
        discovery, client_cls = make_fake_discovery()
        patch_a, patch_b = self._patches(discovery, client_cls)
        bot = FakeBot()
        update = FakeUpdate(user=FakeUser(123456789), chat=FakeChat(999999), bot=bot)
        context = FakeContext(self.core, bot=bot)

        async def scenario():
            with patch_a, patch_b:
                await MODULE.show_status(self.core, update, context)

        asyncio.run(scenario())
        self.assertTrue(any("该会话未绑定 LitePan 实例" in s.text for s in bot.sent))

    def test_unauthorized_user_rejected(self):
        discovery, client_cls = make_fake_discovery()
        patch_a, patch_b = self._patches(discovery, client_cls)
        bot = FakeBot()
        update = FakeUpdate(user=FakeUser(42), chat=FakeChat(42), bot=bot)
        context = FakeContext(self.core, bot=bot)

        async def scenario():
            with patch_a, patch_b:
                await MODULE.show_list(self.core, update, context, page=1)

        asyncio.run(scenario())
        self.assertTrue(any("权限" in s.text for s in bot.sent))
        self.assertEqual(lp_handlers.get_state(self.core).discovery_cache, {})

    def test_menu_fragment_is_scoped_and_deduped(self):
        discovery, client_cls = make_fake_discovery()
        patch_a, patch_b = self._patches(discovery, client_cls)

        async def scenario():
            with patch_a, patch_b:
                ok = await lp_handlers.refresh_menu(self.core, self.bot, chat_id=123456789)
                self.assertTrue(ok)
                calls_after_first = len(self.bot.commands)
                # 内容没变：MenuManager 不应该再发请求
                await lp_handlers.refresh_menu(self.core, self.bot, chat_id=123456789)
                self.assertEqual(len(self.bot.commands), calls_after_first)

        asyncio.run(scenario())
        scoped = [c for c in self.bot.commands if c[0] is not None]
        self.assertTrue(scoped, "LitePan 命令应按会话作用域下发")
        names = {cmd.command for cmd in scoped[-1][1]}
        self.assertIn("refresh_all", names)
        self.assertIn("refresh_gy01", names)


class RegisterWiringTests(unittest.TestCase):
    """和 router 同处一个 Application 时，命令不能重复（/p_status、/p_list 归 router）。"""

    def test_router_and_litepan_do_not_duplicate_commands(self):
        from telegram.ext import ApplicationBuilder

        from mtbots.router import register as router_register

        tmp = tempfile.TemporaryDirectory(prefix="litepan-wire-")
        self.addCleanup(tmp.cleanup)
        core = make_core(tmpdir=tmp.name)
        core.register(MODULE)
        app = ApplicationBuilder().token("123456:TESTTOKEN").build()
        router_register(app, core)
        MODULE.register(app, core)

        seen = {}
        for group, handlers in app.handlers.items():
            for handler in handlers:
                for cmd in getattr(handler, "commands", []) or []:
                    self.assertNotIn(cmd, seen, "命令 /%s 被注册了两次" % cmd)
                    seen[cmd] = group
        for name in ("refresh", "strm", "run", "info", "ping", "p_menu"):
            self.assertIn(name, seen)
        # /p_status、/p_list 由 router 的永久别名注册，模块只提供 show_status/show_list
        self.assertIn("p_status", seen)
        self.assertIn("p_list", seen)


if __name__ == "__main__":
    unittest.main()
