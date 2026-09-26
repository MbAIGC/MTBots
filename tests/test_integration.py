"""装配级集成测试：三个真实模块 + 路由 + 菜单，必须在同一个 Application 里共存。

这里不联网、不轮询 Telegram，只验证「装得起来、命令不打架、菜单是合并出来的」。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mtbots.app import build_application, build_core, load_modules, modules_summary
from mtbots.config import Settings
from tests.fakes import FakeBot, make_recording_app, real_update
from tests.htmlcheck import assert_html_valid

ROOT = Path(__file__).resolve().parent.parent
RESERVED = {"start", "help", "status", "list", "home", "menu", "jobs", "id", "cancel"}


def _command_names(application) -> "list[tuple[int, str]]":
    """按注册顺序返回 (group, 命令名)，用于验证路由优先与不冲突。"""
    found: list[tuple[int, str]] = []
    for group, handlers in application.handlers.items():
        for index, handler in enumerate(handlers):
            commands = getattr(handler, "commands", None)
            if commands:
                for cmd in commands:
                    found.append((index, cmd))
    return found


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings.from_env(
            {
                "MTBOTS_BOT_TOKEN": "123456:INTEGRATIONTEST",
                "ALLOWED_USER_IDS": "123456789",
                "DATA_DIR": self.tmp.name,
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_three_modules_load(self):
        core = load_modules(build_core(self.settings))
        self.assertEqual(sorted(core.modules), ["cline", "docker", "litepan"])
        for spec in core.modules.values():
            self.assertTrue(callable(spec.register))
            self.assertTrue(spec.callback_prefix)

    def test_application_builds_and_commands_do_not_conflict(self):
        core = load_modules(build_core(self.settings))
        application = build_application(self.settings, core)
        names = _command_names(application)
        seen: dict[str, int] = {}
        for index, cmd in names:
            self.assertNotIn(cmd, seen, "命令 /%s 被注册了两次" % cmd)
            seen[cmd] = index

        # 冲突命令必须由路由（注册序号最小）认领
        for reserved in ("status", "list", "start", "help"):
            self.assertIn(reserved, seen, "缺少全局命令 /%s" % reserved)
        router_commands = {"start", "help", "status", "list", "home", "menu", "jobs", "id", "cancel"}
        router_indexes = [seen[c] for c in router_commands if c in seen]
        self.assertTrue(router_indexes)
        for cmd in ("addkey", "keys", "quota", "refresh", "upgrade", "prune", "d_list", "c_status"):
            if cmd in seen:
                self.assertGreaterEqual(seen[cmd], min(router_indexes), "/%s 抢在路由之前注册了" % cmd)

    def test_menu_contains_visible_module_commands(self):
        import asyncio

        core = load_modules(build_core(self.settings))
        bot = FakeBot()
        # 模拟 LitePan 的 startup 语义：它会在启动后提交规则命令片段
        self.assertTrue(asyncio.run(core.menu.apply(bot, force=True)))
        commands = [c.command for c in bot.commands[0][1]]
        self.assertIn("start", commands)
        for module in core.modules.values():
            for name, _desc in module.commands(core, 123456789):
                self.assertIn(name, commands, "菜单缺少 %s 的命令 %s" % (module.id, name))

    def test_module_check_functions_do_not_raise(self):
        core = load_modules(build_core(self.settings))
        for line in modules_summary(core):
            self.assertIsInstance(line, str)

    def test_cli_check_runs_offline(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = "%s%s%s" % (ROOT / ".vendor", os.pathsep, ROOT)
        env.update(
            {
                "MTBOTS_BOT_TOKEN": "123456:CLITEST",
                "ALLOWED_USER_IDS": "123456789",
                "DATA_DIR": self.tmp.name,
            }
        )
        result = subprocess.run(
            [sys.executable, "-m", "mtbots", "--check"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            env=env,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("配置自检", result.stdout)
        self.assertIn("模块", result.stdout)


class DispatcherTests(unittest.TestCase):
    """端到端：真 `telegram.Update` → PTB 自己的 dispatcher → 假 Bot 记录到的调用。

    这一层专门盯「装配」才会犯的错：
      · 兜底 handler 放错 group，导致每条命令被执行两次；
      · 全角斜杠 / 代码块粘贴过来的命令救不回来；
      · 模块被下线后旧按钮一直转圈；
      · 面板没有原地编辑而是每次新发。
    """

    def _drive(self, app, update):
        import asyncio
        import warnings

        async def run():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # PTB 会在非 running 状态下建 task，纯噪音
                await app.process_update(update)
            await asyncio.sleep(0)

        asyncio.run(run())

    def test_start_renders_home_exactly_once(self):
        app, _core, bot = make_recording_app(modules="docker")
        self._drive(app, real_update(bot, text="/start"))
        self.assertEqual(len(bot.rec["sent"]), 1, bot.sent_texts)
        self.assertEqual(bot.rec["edits"], [])  # 兜底没有再执行一次首页
        self.assertIn("控制台", bot.sent_texts[0])
        self.assertIn("Docker", bot.sent_texts[0])

    def test_commands_are_not_executed_twice(self):
        """一条命令只渲染一次面板；兜底 handler 放错 group 时这里会看到 2 条消息。"""
        app, _core, bot = make_recording_app(modules="docker")
        self._drive(app, real_update(bot, text="/start", update_id=1))
        self.assertEqual(len(bot.rec["sent"]), 1, bot.sent_texts)
        self.assertEqual(bot.rec["edits"], [])  # 首次没有面板可编辑
        self._drive(app, real_update(bot, text="/status", update_id=2))
        self.assertEqual(len(bot.rec["sent"]), 2, "命令触发的面板要新发到最底部")
        self.assertEqual(bot.rec["edits"], [], "命令触发时不做原地编辑")
        self.assertEqual(len(bot.rec["deleted"]), 1, "旧面板要删掉，避免聊天里堆一摞死面板")
        self.assertIn("控制台", bot.sent_texts[-1])

    def test_full_width_command_is_rescued(self):
        app, _core, bot = make_recording_app(modules="docker")
        self._drive(app, real_update(bot, text="／start"))
        self.assertEqual(len(bot.rec["sent"]), 1, "全角斜杠的命令必须被兜底救回来")
        self.assertIn("控制台", bot.sent_texts[0])

    def test_unknown_command_gets_actionable_reply(self):
        app, _core, bot = make_recording_app(modules="docker")
        self._drive(app, real_update(bot, text="/nosuchthing"))
        self.assertTrue(bot.sent_texts, "未知命令必须有反馈")
        self.assertIn("没识别出", bot.sent_texts[-1])
        self.assertNotIn("处理时出错了", bot.sent_texts[-1], "未知命令不该走异常路径")

    def test_help_lists_every_module(self):
        app, _core, bot = make_recording_app(modules="docker,litepan,cline")
        self._drive(app, real_update(bot, text="/help"))
        text = bot.last_text
        for expected in ("Docker", "LitePan", "Cline"):
            self.assertIn(expected, text)

    def test_jobs_panel_renders(self):
        app, _core, bot = make_recording_app(modules="docker")
        self._drive(app, real_update(bot, text="/jobs"))
        self.assertIn("任务", bot.last_text)

    def test_docker_list_panel_explains_empty_scan(self):
        """空列表必须带上原因：以前只有一句「暂未检测到」，用户只能猜是不是权限。"""
        app, core, bot = make_recording_app(modules="docker", docker_projects=[])
        core.data["docker"].last_scan_error = (
            "permission denied while trying to connect to the Docker daemon socket"
        )
        # 实测 GID 那一行依赖运行环境（sandbox 里是 root、CI 里根本没 socket），
        # 这里只验证它被拼进面板；内容本身由 tests/test_docker_module.py 覆盖。
        with mock.patch(
            "mtbots.features.docker.compose.socket_group_hint",
            return_value=["   实测：GID-MARKER"],
        ):
            self._drive(app, real_update(bot, text="/d_list"))

        text = bot.last_text
        self.assertIn("暂未检测到", text)
        self.assertIn("permission denied", text)
        self.assertIn("stat -c", text)
        self.assertIn("GID-MARKER", text)

    def test_every_panel_is_valid_telegram_html(self):
        """所有面板文案都要能被 Telegram 的 HTML 解析器接受。

        历史事故：LitePan 的 `/refresh <盘名>` 把整条消息打成
        `Can't parse entities: unsupported start tag "盘名"`，面板刷不出来，按钮看起来失灵。
        """
        app, _core, bot = make_recording_app(modules="docker,litepan,cline")
        commands = (
            "/start",
            "/help",
            "/id",
            "/jobs",
            "/status",
            "/list",
            "/d_list",
            "/p_list",
            "/p_status",
            "/c_list",
            "/c_status",
            "/refresh_光鸭A",
            "/upgrade",
        )
        for index, command in enumerate(commands, start=1):
            self._drive(app, real_update(bot, text=command, update_id=index))

        bodies = [text for _chat, text, _kwargs in bot.rec["sent"]]
        bodies += [text for text in bot.rec["edits"] if text]
        self.assertTrue(bodies, "这些命令至少要产出一些文案")
        for body in bodies:
            assert_html_valid(self, body, "面板文案里有 Telegram 不认的标签")

    def test_nav_callback_answers_and_edits_same_panel(self):
        """点按钮一律原地改同一条面板消息：home / help / 模块面板共用一条。"""
        app, _core, bot = make_recording_app(modules="docker")
        self._drive(app, real_update(bot, text="/start", update_id=1))
        self._drive(app, real_update(bot, data="nav|help", update_id=2))
        self.assertEqual(bot.rec["answers"], [""], "导航回调必须静默应答一次")
        self.assertEqual(len(bot.rec["sent"]), 1, "帮助面板要复用同一条消息，不新发")
        self.assertEqual(len(bot.rec["edits"]), 1)
        self.assertIn("帮助", bot.rec["edits"][0])
        # 再回首页：还是编辑这一条，用户视线里一定看得见
        self._drive(app, real_update(bot, data="nav|home", update_id=3))
        self.assertEqual(len(bot.rec["sent"]), 1)
        self.assertEqual(len(bot.rec["edits"]), 2)
        self.assertIn("控制台", bot.rec["edits"][-1])

    def test_back_button_from_module_panel_edits_the_same_message(self):
        """模块面板里的「🏠 返回」必须原地改回首页（这条路径以前会改到另一条看不见的消息上）。"""
        app, _core, bot = make_recording_app(modules="docker")
        self._drive(app, real_update(bot, text="/d_list", update_id=1))
        self.assertEqual(len(bot.rec["sent"]), 1)
        panel_id = bot.rec["sent"][0]
        back = self._find_back_data(bot)
        self.assertIsNotNone(back, "模块面板必须带一个 🏠 返回 按钮")
        self._drive(app, real_update(bot, data=back, update_id=2))
        self.assertEqual(len(bot.rec["sent"]), 1, "返回不许新发消息")
        self.assertEqual(len(bot.rec["edits"]), 1)
        self.assertIn("控制台", bot.rec["edits"][0])
        self.assertEqual(bot.rec["sent"][0], panel_id)

    @staticmethod
    def _find_back_data(bot) -> str | None:
        markup = bot.rec["sent"][-1][2].get("reply_markup")
        for row in getattr(markup, "inline_keyboard", []) or []:
            for button in row:
                if "返回" in (button.text or ""):
                    return button.callback_data
        return None

    def test_callback_of_disabled_module_gets_explained(self):
        app, _core, bot = make_recording_app(modules="docker")
        self._drive(app, real_update(bot, data="p|list", update_id=1))
        self.assertEqual(bot.rec["edits"], [], "没有 LitePan 模块就不该有人去改面板")
        self.assertTrue(bot.rec["answers"], "旧按钮必须得到明确反馈，不能一直转圈")
        self.assertIn("不可用", bot.rec["answers"][-1])

    def test_enabled_module_callback_is_not_stolen_by_fallback(self):
        app, core, bot = make_recording_app(modules="docker")
        core.set_module(123456789, None)
        # 用 docker 自己的回调前缀，确认模块 handler 先拿到（而不是被兜底抢走）
        self._drive(app, real_update(bot, data="d|nope|1", update_id=1))
        joined = " ".join(bot.rec["answers"])
        self.assertNotIn("模块可能已下线", joined)


class FinishFlowTests(unittest.TestCase):
    """收尾只留一条消息：交互式长任务的结论画在面板上，不再另发「✅ 🐳」卡片。

    升级一个项目以前会留下四条消息：面板、`✅ 拉取新镜像 - mt 完成`、
    `✅ 重建与启动 - mt 完成`、完成卡片。现在两步执行消息成功即删，结论合并进面板。
    """

    def _run_bulk_upgrade(self, extra_modules=()):
        import asyncio

        from mtbots.features.docker import handlers as docker_handlers
        from tests.fakes import add_fake_module

        app, core, bot = make_recording_app(modules="docker")
        for module_id in extra_modules:
            add_fake_module(core, module_id)
        state = core.data["docker"]
        state.compose_bin = ["docker", "compose"]  # 不依赖宿主真的装了 docker
        update = real_update(bot, data="d|upgrade_all_confirm|tok")

        calls: list[dict] = []

        async def fake_run(_state, _message, _cmd, **kwargs):
            calls.append(kwargs)
            return True

        with mock.patch.object(docker_handlers, "run_command_with_feedback", new=fake_run):
            asyncio.run(docker_handlers._do_upgrade_all(core, update, None))
        return core, bot, calls

    def test_bulk_upgrade_finishes_on_the_panel_only(self):
        _core, bot, calls = self._run_bulk_upgrade()

        self.assertEqual(len(bot.rec["sent"]), 1, "只发面板这一条，不再推完成卡片")
        self.assertEqual(len(bot.rec["edits"]), 1, "收尾就是把面板改成完成态")
        final = bot.rec["edits"][-1]
        self.assertIn("✅ 🐳 <b>批量升级全部项目</b>", final, "完成卡片那行画在面板上")
        self.assertIn("全部 2 个项目升级完成", final)
        self.assertIn("成功 (2)", final)
        assert_html_valid(self, final)

        self.assertTrue(calls, "两条命令都跑过")
        self.assertTrue(all(c.get("delete_on_success") for c in calls), "执行消息成功即删")

        markup = bot.rec["edit_kwargs"][-1]["reply_markup"]
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("🔙 返回列表", labels)
        self.assertIn("🧰 任务中心", labels)
        self.assertIn("🏠 返回", labels)

    def test_next_actions_row_lists_other_modules(self):
        _core, bot, _calls = self._run_bulk_upgrade(extra_modules=("litepan", "cline"))
        markup = bot.rec["edit_kwargs"][-1]["reply_markup"]
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("nav|open|litepan", callbacks)
        self.assertIn("nav|open|cline", callbacks)
        self.assertIn("nav|jobs", callbacks)
        self.assertNotIn("nav|open|docker", callbacks, "当前模块不再重复给按钮")


class HtmlGuardTests(unittest.TestCase):
    """守卫本身也要有守卫：确认它能抓到线上那次 `<盘名>` 事故。"""

    def test_old_litepan_text_would_have_been_caught(self):
        from tests.htmlcheck import html_problems

        broken = "未配置默认事件：可先 /info 查看规则，再用 /refresh <盘名>、/refresh_<规则> 或 /run <事件>。"
        problems = html_problems(broken)
        self.assertTrue(problems, "这个文案必须被判定为不合法")
        self.assertTrue(any("盘名" in p for p in problems), problems)
        # 转义之后必须干净
        self.assertEqual(html_problems("未配置默认事件：/refresh &lt;盘名>、/run &lt;事件>。"), [])

    def test_unbalanced_tag_is_caught(self):
        from tests.htmlcheck import html_problems

        self.assertTrue(any("未闭合" in p for p in html_problems("<b>没闭合")))
        self.assertTrue(any("不匹配" in p for p in html_problems("<b>x</i>")))
        self.assertEqual(html_problems("<b>粗</b> <code>码</code> <a href=\"https://x.y\">链</a>"), [])


if __name__ == "__main__":
    unittest.main()
