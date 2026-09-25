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

from mtbots.app import build_application, build_core, load_modules, modules_summary
from mtbots.config import Settings
from tests.fakes import FakeBot, make_recording_app, real_update

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
        app, _core, bot = make_recording_app(modules="docker")
        self._drive(app, real_update(bot, text="/start", update_id=1))
        self._drive(app, real_update(bot, text="/status", update_id=2))
        self.assertEqual(len(bot.rec["sent"]), 1, "第二条命令应编辑已有面板而不是新发")
        self.assertEqual(len(bot.rec["edits"]), 1)
        self.assertIn("控制台", bot.rec["edits"][0])

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

    def test_nav_callback_answers_and_edits_same_panel(self):
        app, _core, bot = make_recording_app(modules="docker")
        self._drive(app, real_update(bot, text="/start", update_id=1))
        self._drive(app, real_update(bot, data="nav|help", update_id=2))
        self.assertEqual(bot.rec["answers"], [""], "导航回调必须静默应答一次")
        self.assertEqual(len(bot.rec["sent"]), 2, "帮助面板是第一次出现，应该新发一条")
        # 再回首页：这次必须原地编辑，而不是又新发一条
        self._drive(app, real_update(bot, data="nav|home", update_id=3))
        self.assertEqual(len(bot.rec["sent"]), 2)
        self.assertEqual(len(bot.rec["edits"]), 1)
        self.assertIn("控制台", bot.rec["edits"][0])

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


if __name__ == "__main__":
    unittest.main()
