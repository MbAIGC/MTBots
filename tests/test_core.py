"""MTBots core 的单元测试：text / acl / store / jobs / panels / menu / config / router。

运行：
    PYTHONPATH=$(pwd)/.vendor:$(pwd) python3 -m unittest tests.test_core -v
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ExtBot

from mtbots import text as textmod
from mtbots.acl import ACL
from mtbots.bot import SafeBot
from mtbots.config import Settings
from mtbots.jobs import DONE, JobCenter, card_text
from mtbots.menu import MenuManager
from mtbots.panels import (
    PanelManager,
    cb,
    cb_parse,
    merge_keyboards,
    nav_home,
    nav_jobs,
    nav_open,
    next_actions_keyboard,
)
from mtbots.store import JsonStore, StoreError, atomic_write_json
from tests.fakes import (
    FAKE_TOKEN,
    FakeBot,
    FakeChat,
    FakeContext,
    FakeQuery,
    FakeUpdate,
    FakeUser,
    add_fake_module,
    make_core,
)


class TextTests(unittest.TestCase):
    def test_split_message_keeps_html_balanced(self):
        body = "<b>标题</b>\n" + "<code>%s</code>\n" % ("x" * 120) + "<i>尾部</i>"
        chunks = textmod.split_message(body, 60)
        self.assertTrue(len(chunks) >= 2)
        for chunk in chunks:
            for tag in ("b", "code", "i"):
                self.assertEqual(chunk.count("<%s>" % tag), chunk.count("</%s>" % tag), chunk)

    def test_split_message_under_limit_returns_single(self):
        self.assertEqual(textmod.split_message("短消息", 100), ["短消息"])

    def test_progress_bar_bounds(self):
        self.assertEqual(len(textmod.progress_bar(50, 10)), 10)
        self.assertEqual(textmod.progress_bar(-5, 4), "░░░░")
        self.assertEqual(textmod.progress_bar(500, 4), "▓▓▓▓")

    def test_humanize(self):
        self.assertEqual(textmod.humanize_delta(5), "刚刚")
        self.assertEqual(textmod.humanize_delta(180), "3 分钟前")
        self.assertEqual(textmod.humanize_duration(80), "1 分 20 秒")

    def test_esc(self):
        self.assertEqual(textmod.esc("<a&b>"), "&lt;a&amp;b&gt;")
        self.assertEqual(textmod.esc(None), "")

    def test_safe_html_escapes_non_tag_literals(self):
        """`<盘名>` 这类字面量必须被转义，否则 Telegram 会整条消息报 unsupported start tag。"""
        cases = {
            "/refresh <盘名>": "/refresh &lt;盘名>",
            "<你的数据目录>": "&lt;你的数据目录>",
            "3 < 5 且 7 > 2": "3 &lt; 5 且 7 > 2",
            "<DATA_DIR>/config.json": "&lt;DATA_DIR>/config.json",
        }
        for raw, expected in cases.items():
            self.assertEqual(textmod.safe_html(raw), expected, raw)

    def test_safe_html_keeps_whitelisted_tags(self):
        body = '<b>粗</b> <i>斜</i> <code>码</code> <a href="https://x.y">链</a> <tg-spoiler>隐</tg-spoiler>'
        self.assertEqual(textmod.safe_html(body), body)
        # 已有实体不许被二次转义
        self.assertEqual(textmod.safe_html("a &lt; b"), "a &lt; b")

    def test_strip_tags_unescapes_for_plain_fallback(self):
        self.assertEqual(textmod.strip_tags("<b>粗</b> &lt;盘名>"), "粗 <盘名>")

    def test_safe_html_output_survives_splitting(self):
        """真实用例：LitePan 那段把整条面板打挂的文案。"""
        body = "用法：/refresh <盘名>、/refresh_<规则> 或 /run <事件>\n" + "x" * 200
        safe = textmod.safe_html(body)
        self.assertNotIn("<盘名>", safe)
        for chunk in textmod.split_message(safe, 80):
            for bad in ("<盘名>", "<规则>", "<事件>"):
                self.assertNotIn(bad, chunk)


class ACLTests(unittest.TestCase):
    def test_empty_whitelist_denies_everyone(self):
        acl = ACL([])
        self.assertFalse(acl.is_allowed(1))
        self.assertIsNone(acl.role(1))
        self.assertFalse(acl.can(1, "cline"))

    def test_listed_user_defaults_to_owner_everywhere(self):
        acl = ACL([7])
        self.assertTrue(acl.is_allowed(7))
        self.assertEqual(acl.role(7), "owner")
        for module in ("docker", "litepan", "cline"):
            self.assertTrue(acl.can(7, module))

    def test_user_role_cannot_touch_docker(self):
        acl = ACL([7], roles={7: "user"})
        self.assertFalse(acl.can(7, "docker"))
        self.assertTrue(acl.can(7, "litepan"))
        self.assertTrue(acl.can(7, "cline"))

    def test_module_roles_override_and_aliases(self):
        acl = ACL([7], roles={7: "user"}, module_roles={"d": {"user"}})
        self.assertTrue(acl.can(7, "docker"))  # 别名 'd' 归一化后生效
        self.assertTrue(ACL([7], roles={7: "user"}).can(7, "litepan"))
        self.assertFalse(ACL([7], roles={7: "user"}).can(7, "unknown-module"))


class StoreTests(unittest.TestCase):
    def test_roundtrip_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = JsonStore(path, {"users": {}})
            store.load()
            def write(data):
                data["users"].setdefault("1", {})["k"] = "v"

            store.mutate(write)
            self.assertEqual(JsonStore(path).load()["users"]["1"]["k"], "v")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            ok, detail = store.self_check()
            self.assertTrue(ok, detail)

    def test_corrupt_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(StoreError):
                JsonStore(path).load()

    def test_mutate_failure_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            atomic_write_json(path, {"n": 1})
            store = JsonStore(path)
            store.load()

            def boom(_data):
                raise RuntimeError("nope")

            with self.assertRaises(RuntimeError):
                store.mutate(boom)
            self.assertEqual(JsonStore(path).load(), {"n": 1})


class JobsTests(unittest.TestCase):
    def test_lifecycle_and_render(self):
        center = JobCenter()
        job = center.add("docker", "升级 emby")
        center.update(job, progress=62)
        self.assertIn("升级 emby", center.render({"docker": "🐳"}))
        self.assertIn("62%", center.render({"docker": "🐳"}))
        center.finish(job, DONE, "完成")
        self.assertFalse(center.has_running())
        self.assertEqual(center.recent(1)[0].id, job.id)

    def test_cancel_calls_callback(self):
        center = JobCenter()
        called = []
        job = center.add("docker", "任务", cancel=lambda: called.append(1), chat_id=1)
        self.assertTrue(center.cancel(job.id))
        self.assertEqual(called, [1])
        self.assertEqual(job.status, "cancelled")

    def test_announce_pushes_card(self):
        center = JobCenter()
        job = center.add("litepan", "刮削")
        center.finish(job, DONE)
        bot = FakeBot()
        asyncio.run(center.announce(bot, 42, job))
        self.assertIn("🎬", bot.sent[-1].text)
        self.assertIn("刮削", bot.sent[-1].text)

    def test_running_job_shows_last_output_line(self):
        """运行中的任务在 /jobs 里要显示最后一行输出（compose 每秒写进来的预览）。"""
        center = JobCenter()
        job = center.add("docker", "拉取镜像")
        center.update(job, progress=30, detail="a1b2c3: Downloading [==>   ]  12MB/48MB")
        text = center.render({"docker": "🐳"})
        self.assertIn("Downloading", text)
        self.assertIn("30%", text)

    def test_finish_never_flips_a_terminal_job(self):
        """先到的终态说了算：取消之后流程再 finish 也不能变成 ✅。"""
        center = JobCenter()
        job = center.add("docker", "升级项目 media")
        center.cancel(job.id)
        self.assertEqual(job.status, "cancelled")
        center.finish(job, DONE, "项目整体升级完成")
        self.assertEqual(job.status, "cancelled", "取消不能被后续 finish 覆盖")
        center.finish(job, "failed", "拉取失败")
        self.assertEqual(job.status, "cancelled")
        # 同状态允许补明细（批量升级被取消时要写清剩余几个项目）
        center.finish(job, "cancelled", "已中止（剩余 2 个项目）")
        self.assertEqual(job.detail, "已中止（剩余 2 个项目）")


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.core = make_core()
        add_fake_module(self.core, "docker")
        self.bot = FakeBot()
        self.update = FakeUpdate(FakeUser(), FakeChat(), bot=self.bot)

    def test_cb_roundtrip(self):
        data = cb("d", "page_turn", {"page": 2})
        prefix, action, payload = cb_parse(data)
        self.assertEqual((prefix, action), ("d", "page_turn"))
        self.assertEqual(payload, {"page": 2})
        self.assertLess(len(data), 64)

    def test_render_decorates_and_tracks_single_message(self):
        asyncio.run(self.core.panels.render("docker", self.update, "正文"))
        first = self.bot.sent[-1]
        self.assertIn("🏠 ›", first.text)
        self.assertIn("🧪", first.text)
        self.assertIn("🔄", first.text)
        flat = [b.callback_data for row in first.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn(nav_home(), flat)

        # 点按钮触发 → 原地编辑同一条（用户视线就在这条消息上）
        callback = FakeUpdate(FakeUser(), FakeChat(), bot=self.bot, query=FakeQuery(nav_home(), FakeUser()))
        asyncio.run(self.core.panels.render("docker", callback, "正文2"))
        self.assertEqual(len(self.bot.sent), 1, "回调触发必须原地编辑，不新发")
        self.assertIn("正文2", self.bot.edits[-1].text)

    def test_command_render_moves_panel_to_bottom(self):
        """命令触发要新发到最底部并删掉旧面板：否则用户看不到更新（「第二次 /start 没反应」）。"""
        chat_id = self.update.effective_chat.id
        asyncio.run(self.core.panels.render("docker", self.update, "第一版"))
        self.assertEqual(len(self.bot.sent), 1)
        asyncio.run(self.core.panels.render("docker", self.update, "第二版"))
        self.assertEqual(len(self.bot.sent), 2, "命令触发要新发")
        self.assertEqual(self.bot.edits, [], "命令触发不做原地编辑")
        self.assertEqual(self.bot.deleted, [(chat_id, 1001)], "旧面板要删掉")

    def test_panels_are_shared_across_modules(self):
        """home / docker / litepan 共用同一条面板消息——「🏠 返回」才一定看得见。"""
        chat_id = self.update.effective_chat.id
        asyncio.run(self.core.panels.render("docker", self.update, "docker 面板"))
        callback = FakeUpdate(FakeUser(), FakeChat(), bot=self.bot, query=FakeQuery(nav_home(), FakeUser()))
        asyncio.run(self.core.panels.render("home", callback, "首页总览"))
        self.assertEqual(len(self.bot.sent), 1, "换模块不允许新开一条消息")
        self.assertEqual(len(self.bot.edits), 1)
        self.assertIn("首页总览", self.bot.edits[-1].text)
        self.assertEqual(self.core.panels.tracked(chat_id), 1001)

    def test_render_force_new(self):
        asyncio.run(self.core.panels.render("docker", self.update, "a"))
        asyncio.run(self.core.panels.render("docker", self.update, "b", force_new=True))
        self.assertEqual(len(self.bot.sent), 2)

    def test_confirm_binds_initiator_and_expires(self):
        panels = self.core.panels
        update = FakeUpdate(FakeUser(111), FakeChat(111), bot=self.bot)
        asyncio.run(panels.ask_confirm("docker", update, "确定？", "d|do"))
        owner_query = FakeQuery("d|do", FakeUser(111))
        ok, msg = panels.validate_confirm(owner_query, "d|do")
        self.assertTrue(ok, msg)

        asyncio.run(panels.ask_confirm("docker", update, "确定？", "d|do2"))
        stranger = FakeQuery("d|do2", FakeUser(222))
        ok, msg = panels.validate_confirm(stranger, "d|do2")
        self.assertFalse(ok)
        self.assertIn("发起", msg)

        asyncio.run(panels.ask_confirm("docker", update, "确定？", "d|do3", ttl=-1))
        ok, msg = panels.validate_confirm(FakeQuery("d|do3", FakeUser(111)), "d|do3")
        self.assertFalse(ok)
        self.assertIn("超时", msg)

    def test_home_keyboard_only_lists_permitted_modules(self):
        add_fake_module(self.core, "litepan")
        self.core.acl = ACL([123456789], roles={123456789: "user"})
        markup = self.core.panels.home_keyboard(123456789)
        flat = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertNotIn(nav_open("docker"), flat)  # 普通用户看不到 Docker 运维模块
        self.assertIn(nav_open("litepan"), flat)


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.core = make_core()
        add_fake_module(self.core, "docker")
        self.bot = FakeBot()

    def test_apply_dedupes_and_forces(self):
        self.core.menu.set_module_commands("docker", [("d_list", "项目列表")])
        self.assertTrue(asyncio.run(self.core.menu.apply(self.bot)))
        self.assertEqual(len(self.bot.commands), 1)
        names = [c.command for c in self.bot.commands[0][1]]
        self.assertIn("d_list", names)

        self.assertTrue(asyncio.run(self.core.menu.apply(self.bot)))
        self.assertEqual(len(self.bot.commands), 1)  # 内容未变 -> 不再请求

        self.assertTrue(asyncio.run(self.core.menu.apply(self.bot, force=True)))
        self.assertEqual(len(self.bot.commands), 2)

    def test_permission_filters_entries(self):
        self.core.menu.set_module_commands("docker", [("d_list", "项目列表")])
        self.core.acl = ACL([1], roles={1: "user"})
        self.assertNotIn("d_list", [c.command for c in self.core.menu.render_for(1)])

    def test_chat_scope_applied(self):
        self.core.menu.set_module_commands("litepan", [("refresh_x", "规则X")], scope_chats=[555])
        asyncio.run(self.core.menu.apply(self.bot))
        scopes = [scope.chat_id if scope is not None else None for scope, _ in self.bot.commands]
        self.assertIn(555, scopes)

    def test_group_scope_keeps_module_commands(self):
        """群/频道的会话菜单不能按「chat_id 当 user_id」过滤——那样群里只剩基础命令。

        Telegram 有会话作用域就覆盖默认作用域，所以群里被下发成 6 条基础命令时，
        用户看到的就是「菜单丢了」。
        """
        self.core.menu.set_module_commands("docker", [("d_list", "项目列表")])
        group = -1001234567890
        names = [c.command for c in self.core.menu.render_for_chat(group)]
        self.assertIn("d_list", names, "群作用域要列出模块命令")
        self.assertIn("start", names, "基础命令照旧")

        asyncio.run(self.core.menu.apply(self.bot, chats=[group]))
        payload = {scope.chat_id if scope else None: cmds for scope, cmds in self.bot.commands}
        self.assertIn("d_list", [c.command for c in payload[group]])

    def test_private_scope_is_still_acl_filtered(self):
        """私聊的 chat_id 就是 user_id：仍然按本人权限裁剪，各人菜单不同。"""
        self.core.menu.set_module_commands("docker", [("d_list", "项目列表")])
        self.core.acl = ACL([1], roles={1: "user"})
        names = [c.command for c in self.core.menu.render_for_chat(1)]
        self.assertNotIn("d_list", names)
        self.assertIn("start", names)

    def test_outsider_private_chat_gets_base_only(self):
        self.core.menu.set_module_commands("docker", [("d_list", "项目列表")])
        names = [c.command for c in self.core.menu.render_for_chat(999)]
        self.assertNotIn("d_list", names)
        self.assertIn("start", names)

    def test_empty_scope_is_never_pushed(self):
        """空片段宁可不发：`set_my_commands([])` 会把那个作用域的菜单擦干净。"""
        core = make_core()
        core.menu = MenuManager(core, [])
        core.menu.attach(core)
        add_fake_module(core, "docker")
        core.menu.set_module_commands("docker", [])
        self.assertTrue(asyncio.run(core.menu.apply(self.bot, chats=[123456789])))
        self.assertEqual(self.bot.commands, [], "空菜单一次都不该下发")


class ConfigTests(unittest.TestCase):
    def test_token_precedence_and_id_union(self):
        settings = Settings.from_env(
            {
                "MTBOTS_BOT_TOKEN": "111:aaa",
                "BOT_TOKEN": "222:bbb",
                "ALLOWED_USER_IDS": "1, 2",
                "TG_ALLOWED_IDS": "2,3",
            }
        )
        self.assertEqual(settings.bot_token, "111:aaa")
        self.assertEqual(settings.allowed_user_ids, frozenset({1, 2, 3}))

    def test_modules_and_paths(self):
        settings = Settings.from_env(
            {"MTBOTS_MODULES": "docker, cline", "DATA_DIR": "/tmp/mtbots-x", "PAGE_SIZE": "9"}
        )
        self.assertEqual(settings.modules_enabled, ("docker", "cline"))
        self.assertEqual(settings.config_file, Path("/tmp/mtbots-x/config.json"))
        self.assertEqual(settings.users_file, Path("/tmp/mtbots-x/litepan-users.json"))
        self.assertEqual(settings.page_size, 9)
        self.assertTrue(settings.module_enabled("cline"))
        self.assertFalse(settings.module_enabled("litepan"))

    def test_problems_when_empty(self):
        issues = Settings.from_env({}).problems()
        self.assertEqual(len(issues), 2)

    def test_custom_api_base(self):
        self.assertIsNone(Settings.from_env({}).custom_api_base())
        self.assertEqual(
            Settings.from_env({"TG_API_BASE": "https://tg.example.com/"}).custom_api_base(),
            "https://tg.example.com",
        )


class LoggingTests(unittest.TestCase):
    def test_redacting_filter_scrubs_exception_traceback(self):
        import logging
        import sys

        from mtbots.logging_setup import RedactingFilter

        secret = "123456:AAFabcdefghijklmnopqrstuvwxyz012345"
        try:
            raise RuntimeError("InvalidToken: %s" % secret)
        except RuntimeError:
            exc_info = sys.exc_info()
        record = logging.LogRecord("mtbots.test", logging.ERROR, __file__, 1, "boom", None, exc_info)
        self.assertTrue(RedactingFilter().filter(record))
        text = record.exc_text or ""
        self.assertNotIn(secret, text)  # traceback 里的明文 Token 必须被抹掉
        self.assertIn("bot<TOKEN>", text)
        self.assertIsNone(record.exc_info)  # 交给 handler 用已脱敏的 exc_text

    def test_redact_patterns(self):
        from mtbots.logging_setup import redact

        self.assertIn("sk_<KEY>", redact("key=sk_abcdefghijklmn"))
        self.assertIn("lpk_<KEY>", redact("key=lpk_api_abcdefghijkl"))
        self.assertNotIn("user@example.com", redact("mail user@example.com"))
        self.assertIn("***", redact('{"admin_password": "hunter2"}'))
        self.assertIn("***", redact("admin_password=hunter2&x=1"))

    def test_token_mask_filter(self):
        import logging

        from mtbots.logging_setup import TokenMaskFilter

        token = "123456:AAFabcdefghijklmnopqrstuvwxyz012345"
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "url %s", (token,), None)
        self.assertTrue(TokenMaskFilter(token).filter(record))
        self.assertIn("[REDACTED_BOT_TOKEN]", record.args[0])


class StoreKindTests(unittest.TestCase):
    def test_kinds_are_distinguishable(self):
        with tempfile.TemporaryDirectory() as tmp:
            corrupt = Path(tmp) / "corrupt.json"
            corrupt.write_text("{oops", encoding="utf-8")
            with self.assertRaises(StoreError) as ctx:
                JsonStore(corrupt).load()
            self.assertEqual(ctx.exception.kind, "corrupt")

            shape = Path(tmp) / "shape.json"
            shape.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertRaises(StoreError) as ctx:
                JsonStore(shape).load()
            self.assertEqual(ctx.exception.kind, "shape")


class CoreStateTests(unittest.TestCase):
    def test_state_slot_does_not_clobber_custom_object(self):
        core = make_core()
        holder = object()
        core.data["docker"] = holder
        bucket = core.state("docker")
        self.assertIsInstance(bucket, dict)
        self.assertIs(core.data["docker"], holder)  # 自定义对象没被覆盖
        bucket["k"] = 1
        self.assertEqual(core.state("docker")["k"], 1)

    def test_state_slot_creates_dict(self):
        core = make_core()
        self.assertEqual(core.state("litepan"), {})
        self.assertIs(core.data["litepan"], core.state("litepan"))


class RouterTests(unittest.TestCase):
    def setUp(self):
        import mtbots.router as router

        self.router = router
        self.core = make_core()
        self.calls = {}
        add_fake_module(self.core, "docker", self.calls)
        add_fake_module(self.core, "litepan", self.calls)
        add_fake_module(self.core, "cline", self.calls)
        self.bot = FakeBot()
        self.user = FakeUser(123456789)
        self.chat = FakeChat(123456789)
        self.context = FakeContext(self.core, self.bot)

    def _update(self, text="", query=None):
        return FakeUpdate(self.user, self.chat, text, self.bot, query=query)

    def test_home_renders_summary_and_buttons(self):
        asyncio.run(self.router.home_panel(self.core, self._update(), self.context))
        body = self.bot.sent[-1].text
        self.assertIn("控制台", body)
        self.assertIn("假模块", body)
        flat = [b.callback_data for row in self.bot.sent[-1].kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn(nav_open("docker"), flat)
        self.assertIsNone(self.core.module_of_chat(self.chat.id))

    def test_status_without_module_goes_home(self):
        asyncio.run(self.router.status_command(self.core, self._update(), self.context))
        self.assertNotIn("show_status", self.calls)
        self.assertIn("控制台", self.bot.sent[-1].text)

    def test_status_inside_module_delegates(self):
        self.core.set_module(self.chat.id, "litepan")
        asyncio.run(self.router.status_command(self.core, self._update(), self.context))
        self.assertEqual(self.calls.get("show_status"), 1)

    def test_list_defaults_to_docker_but_litepan_uses_rules(self):
        asyncio.run(self.router.list_command(self.core, self._update(), self.context))
        self.assertEqual(self.calls.get("show_list"), 1)

        self.core.set_module(self.chat.id, "litepan")
        asyncio.run(self.router.list_command(self.core, self._update(), self.context))
        self.assertEqual(self.calls.get("show_list"), 2)

    def test_alias_sets_module_context(self):
        handler = self.router.alias_handler("cline", "status")
        asyncio.run(handler(self._update(), self.context))
        self.assertEqual(self.core.module_of_chat(self.chat.id), "cline")
        self.assertEqual(self.calls.get("show_status"), 1)

    def test_nav_open_callback(self):
        query = FakeQuery(nav_open("docker"), self.user)
        update = FakeUpdate(self.user, self.chat, bot=self.bot, query=query)
        asyncio.run(self.router.callback_router(self.core, update, self.context))
        self.assertEqual(self.core.module_of_chat(self.chat.id), "docker")
        self.assertEqual(self.calls.get("open_panel"), 1)

    def test_nav_home_callback(self):
        self.core.set_module(self.chat.id, "docker")
        query = FakeQuery(nav_home(), self.user)
        update = FakeUpdate(self.user, self.chat, bot=self.bot, query=query)
        asyncio.run(self.router.callback_router(self.core, update, self.context))
        self.assertIsNone(self.core.module_of_chat(self.chat.id))
        self.assertIn("控制台", self.bot.sent[-1].text)

    def test_jobs_cancel_callback(self):
        job = self.core.jobs.add("docker", "升级", chat_id=self.chat.id)
        query = FakeQuery("job|cancel|%s" % job.id, self.user)
        update = FakeUpdate(self.user, self.chat, bot=self.bot, query=query)
        asyncio.run(self.router.callback_router(self.core, update, self.context))
        self.assertEqual(job.status, "cancelled")

    def test_unknown_command_rescues_fullwidth_slash(self):
        update = self._update("／status")
        asyncio.run(self.router.unknown_command(update, self.context))
        self.assertIn("控制台", self.bot.sent[-1].text)

    def test_unknown_command_rescues_dynamic_prefix(self):
        from mtbots.core import ModuleSpec

        calls: list[str] = []

        async def handler(core, update, context):
            calls.append("refresh_")

        async def ptb_style(update, context):
            calls.append("ptb")

        self.core.register(
            ModuleSpec(
                id="lite",
                icon="🧪",
                title="动态",
                description="",
                callback_prefix="x",
                register=lambda app, c: None,
                rescue={"refresh_": handler, "twoparam": ptb_style},
            )
        )
        asyncio.run(self.router.unknown_command(self._update("／refresh_光鸭A"), self.context))
        self.assertEqual(calls, ["refresh_"])
        asyncio.run(self.router.unknown_command(self._update("／twoparam"), self.context))
        self.assertEqual(calls, ["refresh_", "ptb"])

    def test_unknown_command_explains(self):
        update = self._update("／nosuchcmd")
        asyncio.run(self.router.unknown_command(update, self.context))
        self.assertIn("没识别出", update.effective_message.replies[-1])

    def test_deny_for_unlisted_user(self):
        self.core.acl = ACL([999])
        allowed = asyncio.run(self.router.ensure_allowed(self.core, self._update()))
        self.assertFalse(allowed)
        self.assertIn("白名单", self.bot.sent[-1].text)


def _register_module(core, module_id: str, title: str, icon: str) -> None:
    """注册一个只有外观信息的模块，专门用来验「跨模块入口」的取舍。"""
    from mtbots.core import ModuleSpec

    core.register(
        ModuleSpec(
            id=module_id,
            icon=icon,
            title=title,
            description="测试用",
            callback_prefix="x",
            register=lambda app, c: None,
            commands=lambda c, uid: [],
            summary=lambda c, uid: "",
            help_text=lambda c, uid: "",
            open_panel=None,
            show_status=None,
            show_list=None,
        )
    )


class NextActionsTests(unittest.TestCase):
    """收尾面上的「下一步」一行：三个 Bot 合并后，任务跑完要能顺手跳到另一条线。"""

    def _core_with_three(self):
        core = make_core()
        _register_module(core, "docker", "Docker 管理", "🐳")
        _register_module(core, "litepan", "LitePan 联动", "🎬")
        _register_module(core, "cline", "Cline 额度", "🤖")
        return core

    def test_lists_other_modules_in_one_row(self):
        core = self._core_with_three()
        markup = next_actions_keyboard(core, 123456789, "docker")
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertEqual(len(markup.inline_keyboard), 1, "跨模块入口只占一行")
        self.assertEqual(callbacks, [nav_open("litepan"), nav_open("cline")])
        self.assertEqual(labels, ["🎬 LitePan", "🤖 Cline"])
        self.assertNotIn(nav_open("docker"), callbacks, "当前模块不用再给一个按钮")
        self.assertNotIn(nav_jobs(), callbacks, "任务中心不占收尾行（首页里有，一步可达）")

    def test_disabled_modules_are_not_offered(self):
        core = make_core()
        _register_module(core, "docker", "Docker 管理", "🐳")
        _register_module(core, "litepan", "LitePan 联动", "🎬")
        markup = next_actions_keyboard(core, 123456789, "docker")
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(callbacks, [nav_open("litepan")])

    def test_single_module_gets_no_row(self):
        core = make_core()
        _register_module(core, "docker", "Docker 管理", "🐳")
        self.assertIsNone(next_actions_keyboard(core, 123456789, "docker"))

    def test_acl_blocks_modules_without_permission(self):
        core = self._core_with_three()
        core.acl = ACL([999999])
        self.assertIsNone(
            next_actions_keyboard(core, 123456789, "docker"), "没权限的模块一个都不给"
        )
        self.assertIsNone(
            next_actions_keyboard(core, None, "docker"), "认不出用户就按默认拒绝处理"
        )

    def test_too_many_modules_falls_back_to_none(self):
        core = self._core_with_three()
        self.assertIsNone(next_actions_keyboard(core, 123456789, "docker", limit=1))

    def test_merge_keyboards_skips_none(self):
        core = self._core_with_three()
        base = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 再跑一次", callback_data="p|x")]])
        merged = merge_keyboards(base, None, next_actions_keyboard(core, 123456789, "litepan"))
        self.assertEqual(len(merged.inline_keyboard), 2)
        self.assertEqual(merged.inline_keyboard[0][0].callback_data, "p|x")
        self.assertIsNone(merge_keyboards(None, None))


class JobCardTests(unittest.TestCase):
    """任务收尾卡片文案：交互式面板和后台推送共用同一份，避免两边各写一套。"""

    def test_card_text_carries_status_module_title_and_detail(self):
        core = make_core()
        job = core.jobs.add("docker", "升级项目 mt", chat_id=1)
        core.jobs.finish(job, DONE, "项目整体升级完成")
        text = card_text(job)
        self.assertIn("✅", text)
        self.assertIn("🐳", text)
        self.assertIn("升级项目 mt", text)
        self.assertIn("项目整体升级完成", text)

    def test_card_text_escapes_title_and_detail(self):
        core = make_core()
        job = core.jobs.add("litepan", "规则 <x>", chat_id=1)
        core.jobs.finish(job, "failed", "失败 <b>原因</b>")
        text = card_text(job)
        self.assertIn("❌", text)
        self.assertNotIn("<b>原因</b>", text)
        self.assertIn("&lt;b&gt;原因&lt;/b&gt;", text)


class SafeBotTests(unittest.IsolatedAsyncioTestCase):
    """出站兜底：非法 `<` 转义 + 解析失败降级纯文本（模块里漏 esc 也不会再整条挂掉）。"""

    def _bot(self) -> SafeBot:
        return SafeBot(FAKE_TOKEN)

    async def test_send_message_sanitizes_html(self):
        bot = self._bot()
        parent = mock.AsyncMock(return_value="ok")
        with mock.patch.object(ExtBot, "send_message", new=parent):
            await bot.send_message(1, "看 <盘名>", parse_mode="HTML")
        args, kwargs = parent.call_args
        self.assertEqual(args[1], "看 &lt;盘名>")
        self.assertEqual(kwargs["parse_mode"], "HTML")

    async def test_send_message_leaves_non_html_alone(self):
        bot = self._bot()
        parent = mock.AsyncMock(return_value="ok")
        with mock.patch.object(ExtBot, "send_message", new=parent):
            await bot.send_message(1, "看 <盘名>", parse_mode="MarkdownV2")
        args, _kwargs = parent.call_args
        self.assertEqual(args[1], "看 <盘名>")

    async def test_edit_message_text_sanitizes_html(self):
        bot = self._bot()
        parent = mock.AsyncMock(return_value="ok")
        with mock.patch.object(ExtBot, "edit_message_text", new=parent):
            await bot.edit_message_text("换 <规则>", chat_id=1, message_id=2, parse_mode="HTML")
        args, kwargs = parent.call_args
        self.assertEqual(args[0], "换 &lt;规则>")
        self.assertEqual(kwargs["message_id"], 2)

    async def test_parse_error_falls_back_to_plain_text(self):
        bot = self._bot()
        calls: list[tuple[str, object]] = []

        async def side_effect(chat_id, text, parse_mode=None, **kwargs):
            calls.append((text, parse_mode))
            if parse_mode:
                raise BadRequest('Can\'t parse entities: Can\'t find end tag corresponding to start tag "b"')
            return "ok"

        parent = mock.AsyncMock(side_effect=side_effect)
        with mock.patch.object(ExtBot, "send_message", new=parent):
            await bot.send_message(1, "x <b>没闭合", parse_mode="HTML")

        self.assertEqual(len(calls), 2, "第一次 HTML 失败后必须再试一次纯文本")
        self.assertEqual(calls[0], ("x <b>没闭合", "HTML"))
        # 降级时会去掉真标签、但把被转义的字面量还原成肉眼可见的 `<...>`
        self.assertEqual(calls[1], ("x 没闭合", None))

    async def test_unrelated_bad_request_is_not_swallowed(self):
        bot = self._bot()
        with mock.patch.object(ExtBot, "send_message", new=mock.AsyncMock(side_effect=BadRequest("chat not found"))):
            with self.assertRaises(BadRequest):
                await bot.send_message(1, "<b>x</b>", parse_mode="HTML")


if __name__ == "__main__":
    unittest.main()
