"""测试替身：不联网、不依赖真实 Telegram 对象，就能把 router / panels / 模块 handler 跑通。

这些替身刻意只实现 MTBots 真正用到的那几个属性/方法，任何一个新用到的 Telegram API
都会在这里以 AttributeError 的形式暴露出来。
"""

from __future__ import annotations

import datetime
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from telegram import Bot, Chat, Message, User
from telegram.constants import ChatType

from mtbots.acl import ACL
from mtbots.config import Settings
from mtbots.core import Core
from mtbots.jobs import JobCenter
from mtbots.menu import MenuManager
from mtbots.panels import PanelManager


class FakeUser:
    def __init__(self, user_id: int = 123456789, first_name: str = "Tester", username: str = "tester"):
        self.id = user_id
        self.first_name = first_name
        self.username = username


class FakeChat:
    def __init__(self, chat_id: int = 123456789, chat_type: str = "private"):
        self.id = chat_id
        self.type = chat_type


class FakeMessage:
    def __init__(self, message_id: int, text: str = "", chat: Optional[FakeChat] = None, bot: Any = None):
        self.message_id = message_id
        self.text = text
        self.chat = chat or FakeChat()
        self._bot = bot
        self.replies: list[str] = []
        self.deleted = False

    async def reply_text(self, text: str, **kwargs: Any) -> "FakeMessage":
        self.replies.append(text)
        if self._bot is not None:
            self._bot.sent.append(SimpleNamespace(chat_id=self.chat.id, text=text, kwargs=kwargs, markup=kwargs.get("reply_markup")))
        return FakeMessage(self.message_id + 1, text, self.chat, self._bot)

    async def delete(self) -> None:
        self.deleted = True


class FakeQuery:
    def __init__(self, data: str, user: Optional[FakeUser] = None, message: Optional[FakeMessage] = None):
        self.data = data
        self.from_user = user or FakeUser()
        self.message = message
        self.answers: list[tuple[str, bool]] = []
        self.edits: list[tuple[str, Any]] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append((text, kwargs.get("reply_markup")))


class FakeBot:
    def __init__(self):
        self.sent: list[SimpleNamespace] = []
        self.edits: list[SimpleNamespace] = []
        self.deleted: list[tuple[int, int]] = []
        self.commands: list[tuple[Any, list[Any]]] = []
        self._next_id = 1000

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> FakeMessage:
        self._next_id += 1
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, kwargs=kwargs))
        return FakeMessage(self._next_id, text, FakeChat(chat_id), self)

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, **kwargs: Any) -> None:
        self.edits.append(SimpleNamespace(chat_id=chat_id, message_id=message_id, text=text, kwargs=kwargs))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))

    async def set_my_commands(self, commands: list[Any], scope: Any = None) -> None:
        self.commands.append((scope, list(commands)))

    @property
    def last_text(self) -> str:
        if self.edits:
            return self.edits[-1].text
        return self.sent[-1].text if self.sent else ""

    @property
    def last_markup(self):
        source = self.edits[-1] if self.edits else (self.sent[-1] if self.sent else None)
        return source.kwargs.get("reply_markup") if source else None


class FakeUpdate:
    def __init__(
        self,
        user: Optional[FakeUser] = None,
        chat: Optional[FakeChat] = None,
        text: str = "",
        bot: Optional[FakeBot] = None,
        query: Optional[FakeQuery] = None,
        update_id: int = 1,
    ):
        self.effective_user = user
        self.effective_chat = chat or FakeChat(getattr(user, "id", 1))
        self.callback_query = query
        self.update_id = update_id
        self._bot = bot or FakeBot()
        message = FakeMessage(500, text, self.effective_chat, self._bot)
        self.effective_message = message
        self.message = message

    def get_bot(self) -> FakeBot:
        return self._bot


class FakeContext:
    def __init__(self, core: Core, bot: Optional[FakeBot] = None, args: Optional[list[str]] = None):
        self.bot = bot or FakeBot()
        self.args = list(args or [])
        self.application = SimpleNamespace(bot_data={"core": core})


def make_core(*, allowed=(123456789,), page_size: int = 6, tmpdir: Optional[str] = None) -> Core:
    base = Path(tmpdir or tempfile.mkdtemp(prefix="mtbots-test-"))
    settings = Settings(
        bot_token="123456:TESTTOKEN",
        allowed_user_ids=frozenset(allowed),
        data_dir=base,
        config_file=base / "config.json",
        users_file=base / "litepan-users.json",
        log_dir=base / "logs",
        page_size=page_size,
    )
    core = Core(
        settings=settings,
        acl=ACL(allowed),
        panels=PanelManager(),
        jobs=JobCenter(),
        menu=MenuManager(None, [("start", "首页")]),
    )
    core.panels.attach(core)
    core.menu.attach(core)
    return core


def add_fake_module(core: Core, module_id: str = "docker", calls: Optional[dict] = None) -> None:
    """注册一个只记录调用的假模块，用来验证路由消歧。"""
    from mtbots.core import ModuleSpec

    calls = calls if calls is not None else {}

    async def open_panel(core_, update, context, **kwargs):
        calls["open_panel"] = calls.get("open_panel", 0) + 1

    async def show_status(core_, update, context, **kwargs):
        calls["show_status"] = calls.get("show_status", 0) + 1

    async def show_list(core_, update, context, **kwargs):
        calls["show_list"] = calls.get("show_list", 0) + 1

    async def summary(core_, uid):
        return "%s 假模块 · 一切正常" % module_id

    core.register(
        ModuleSpec(
            id=module_id,
            icon="🧪",
            title="假模块",
            description="测试用",
            callback_prefix="d",
            register=lambda app, c: None,
            commands=lambda c, uid: [("d_list", "假列表")],
            summary=summary,
            help_text=lambda c, uid: "假帮助",
            open_panel=open_panel,
            show_status=show_status,
            show_list=show_list,
        )
    )


# ==================== 真 Update + 假 Bot：让 PTB 自己的 dispatcher 跑起来 ====================
#: 形状合法（CTB 的 token 校验会检查）但永远不会联网的假 token
FAKE_TOKEN = "123456789:AAF" + "x" * 32


class RecordingBot(Bot):
    """继承真 ``Bot``（PTB 的 ``process_update`` 需要它），但把所有网络调用换成记录。

    这样测试就能走完完整链路：``Update`` → 真 `handler.check_update` → handler → ``bot.*`` 调用。
    PTB 的 ``TelegramObject.__setattr__`` 只允许写 ``__slots__`` 里的字段，所以记录容器用
    ``object.__setattr__`` 绕过。
    """

    def __init__(self, token: str = FAKE_TOKEN):
        super().__init__(token)
        object.__setattr__(
            self,
            "_rec",
            {"sent": [], "edits": [], "edit_kwargs": [], "deleted": [], "commands": [],
             "answers": [], "deleted_commands": [], "actions": []},
        )
        self._bot_user = User(id=999, first_name="FakeBot", is_bot=True, username="fakebot")

    # ---- 记录容器 ----
    @property
    def rec(self) -> dict:
        return object.__getattribute__(self, "_rec")

    @property
    def sent_texts(self) -> list[str]:
        return [item[1] for item in self.rec["sent"]]

    @property
    def last_text(self) -> str:
        if self.rec["edits"]:
            return self.rec["edits"][-1] or ""
        return self.rec["sent"][-1][1] if self.rec["sent"] else ""

    # ---- 被 MTBots 用到的 Telegram API（全部本地实现） ----
    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Message:
        self.rec["sent"].append((chat_id, text, kwargs))
        return Message(
            1000 + len(self.rec["sent"]),
            datetime.datetime.now(datetime.timezone.utc),
            Chat(chat_id, ChatType.PRIVATE),
            text=text,
        )

    async def edit_message_text(self, text: str = None, chat_id: int = None, message_id: int = None, **kwargs: Any) -> bool:
        self.rec["edits"].append(text)
        self.rec["edit_kwargs"].append(kwargs)
        return True

    async def edit_message_reply_markup(self, chat_id: int = None, message_id: int = None, **kwargs: Any) -> bool:
        self.rec["edits"].append(None)
        return True

    async def delete_message(self, chat_id: int = None, message_id: int = None, **kwargs: Any) -> bool:
        self.rec["deleted"].append((chat_id, message_id))
        return True

    async def set_my_commands(self, commands: list[Any], **kwargs: Any) -> bool:
        self.rec["commands"].append((kwargs.get("scope"), list(commands)))
        return True

    async def delete_my_commands(self, **kwargs: Any) -> bool:
        self.rec["deleted_commands"].append(kwargs)
        return True

    async def answer_callback_query(self, callback_query_id: str, **kwargs: Any) -> bool:
        self.rec["answers"].append(kwargs.get("text", "") or "")
        return True

    async def send_chat_action(self, *args: Any, **kwargs: Any) -> bool:
        self.rec["actions"].append(args)
        return True


def real_update(
    bot: RecordingBot,
    *,
    text: str = "",
    data: Optional[str] = None,
    user_id: int = 123456789,
    chat_id: Optional[int] = None,
    update_id: int = 1,
) -> Any:
    """构造一个真正的 ``telegram.Update``（消息或按钮），并把假 Bot 绑到它身上。"""
    from telegram import CallbackQuery, Chat, Message, Update

    chat_id = chat_id if chat_id is not None else user_id
    user = User(id=user_id, first_name="Tester", is_bot=False, username="tester")
    chat = Chat(chat_id, ChatType.PRIVATE)
    now = datetime.datetime.now(datetime.timezone.utc)
    message = Message(500, now, chat, from_user=user, text=text)
    message.set_bot(bot)
    if data is None:
        update = Update(update_id, message=message)
    else:
        query = CallbackQuery(id="cb%d" % update_id, from_user=user, chat_instance="ci", data=data, message=message)
        update = Update(update_id, callback_query=query)
        query.set_bot(bot)
    update.set_bot(bot)
    return update


#: 集成测试用的假 compose 项目（Docker 模块的真实扫描要跑 `docker compose ls`，测试里必须替换掉）
FAKE_PROJECTS = [
    {
        "name": "media",
        "dir": "/docker/media",
        "status": "running(1)",
        "services": ["emby"],
        "config_files": ["/docker/media/docker-compose.yml"],
    },
    {
        "name": "tools",
        "dir": "/docker/tools",
        "status": "exited(2)",
        "services": ["uptime-kuma", "watchtower"],
        "config_files": ["/docker/tools/docker-compose.yml"],
    },
]


def make_recording_app(
    *,
    modules: str = "docker",
    docker_projects: Optional[list[dict]] = None,
):
    """构建真 Application + RecordingBot（不联网、不 run_polling）。"""
    from mtbots.app import build_application, build_core, load_modules

    settings = Settings.from_env(
        {
            "MTBOTS_BOT_TOKEN": FAKE_TOKEN,
            "ALLOWED_USER_IDS": "123456789",
            "DATA_DIR": tempfile.mkdtemp(prefix="mtbots-dispatch-"),
            "MTBOTS_MODULES": modules,
        }
    )
    core = load_modules(build_core(settings))
    app = build_application(settings, core)
    # Docker 的项目扫描会真的执行宿主机命令，集成测试一律用假数据。
    # 注意：DockerState 是 build_application() 里 register() 时才挂到 core.data["docker"] 的，
    # 所以必须在装配**之后**再装 scan_hook（模块把状态存在 core.data，不是 core.state() 的 dict 槽）。
    state = core.data.get("docker")
    if state is not None and hasattr(state, "scan_hook"):
        state.scan_hook = lambda: list(FAKE_PROJECTS if docker_projects is None else docker_projects)
    bot = RecordingBot()
    app.bot = bot  # 让 context.bot 也指向假 Bot
    app._initialized = True  # 跳过 initialize()（那会真的调 getMe）
    return app, core, bot


__all__ = [
    "FakeUser",
    "FakeChat",
    "FakeMessage",
    "FakeQuery",
    "FakeBot",
    "FakeUpdate",
    "FakeContext",
    "make_core",
    "add_fake_module",
    "FAKE_TOKEN",
    "RecordingBot",
    "real_update",
    "make_recording_app",
    "FAKE_PROJECTS",
]
