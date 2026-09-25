# MTBots 合并施工契约（v1）

> 目标：把 **LDMG**（`/root/workspace/LDMG/bot.py`）、**LitePan-TGBot**（`/root/LitePan-TGBot/tgbot.py`）、
> **ClinePass-TG-Bot**（`/root/ClinePass-TG-Bot/bot.py` + `core.py`）合并成**一个 python-telegram-bot 21.6
> 异步进程**：`MTBots`。
> 设计依据：[three-bots-merge-design.md](three-bots-merge-design.md)、[three-bots-merge-ux.md](three-bots-merge-ux.md)（方案 A，单人 owner 形态）。
> 本文是**接口契约**：core 与三个 feature 包必须严格按此对接，否则集成会失败。

---

## 0. 目录结构（最终形态）

```
/root/DSH/MTBots/
├── mtbots/
│   ├── __init__.py            # __version__
│   ├── __main__.py            # python -m mtbots [--check|--health|--version]
│   ├── app.py                 # build_application / post_init / main
│   ├── config.py              # 全局 Settings（token/白名单/路径/模块开关/ACL 文件）
│   ├── acl.py                 # 角色 + 模块权限（默认拒绝）
│   ├── logging_setup.py       # 统一日志 + redact()/RedactingFilter/TokenMaskFilter
│   ├── text.py                # esc/split_message/progress_bar/humanize_delta/now_stamp…
│   ├── store.py               # JsonStore：原子写 + 0600 + self_check
│   ├── panels.py              # PanelManager：单会话单面板 + 面包屑 + 返回 + 两步确认 + callback store
│   ├── jobs.py                # JobCenter：跨模块任务中心
│   ├── menu.py                # MenuManager：命令菜单片段合并 + 去重 + scope 下发
│   ├── core.py                # Core 容器 + ModuleSpec + core_of()
│   ├── router.py              # 首页 / 面包屑路由 / 4 个冲突命令消歧 / 帮助 / /jobs / /id / 兜底救援
│   └── features/
│       ├── __init__.py        # load_modules(core) -> list[ModuleSpec]
│       ├── docker/            # ← LDMG 移植
│       │   ├── __init__.py    # MODULE = ModuleSpec(...)
│       │   ├── config.py      # DockerSettings.from_env(settings)
│       │   ├── compose.py     # compose 探测/扫描/执行/任务锁（LDMG 全局状态收进一个对象）
│       │   └── handlers.py    # cmd_list/cmd_status/cmd_upgrade/cmd_prune/button 分发 + open_panel/show_status/show_list
│       ├── litepan/           # ← LitePan-TGBot 移植（同步 stdlib → asyncio.to_thread）
│       │   ├── __init__.py    # MODULE = ModuleSpec(...)
│       │   ├── config.py      # UserProfile / LitePanConfig（users.json + LITEPAN_* env 单用户兜底）
│       │   ├── discovery.py   # Discovery：60s 缓存 + 单飞 + slug（pypinyin/限长24/去重）
│       │   ├── client.py      # LitePanClient（urllib，原样保留逻辑）
│       │   └── handlers.py    # /refresh /refresh_<slug> /strm /run /info /ping /p_status /p_list + 回执任务
│       └── cline/             # ← ClinePass 移植
│           ├── __init__.py    # MODULE = ModuleSpec(...)
│           ├── core.py        # ← ClinePass core.py 几乎原样（Settings/ConfigStore/Client/渲染/解析）
│           └── handlers.py    # ← ClinePass bot.py 的 handler 层
├── tests/                     # unittest（stdlib），可用 .vendor 里的 PTB
├── docs/                      # 本目录（设计稿 + 契约）
├── Dockerfile  docker-compose.yml  requirements.txt  .env.example  README.md  Makefile
└── .vendor/                   # 本地测试用依赖（gitignore），非交付物
```

红线（设计文档 §6）：**任一渲染函数都必须带 `user_id` 做归属断言；默认拒绝；统一脱敏；密钥只落 `data/`（0600）。**

---

## 1. Core API（`mtbots/core.py`）

```python
@dataclass
class Core:
    settings: Settings                 # mtbots/config.py
    acl: ACL                           # mtbots/acl.py
    panels: PanelManager               # mtbots/panels.py
    jobs: JobCenter                    # mtbots/jobs.py
    menu: MenuManager                  # mtbots/menu.py
    modules: dict[str, "ModuleSpec"]   # id -> spec（按注册顺序）
    data: dict[str, Any]               # 模块私有状态（core.data["docker"] 之类），别放密钥

    def register(self, spec: "ModuleSpec") -> None: ...
    def get(self, module_id: str) -> "ModuleSpec": ...          # KeyError 表示模块未启用
    def has(self, module_id: str) -> bool: ...
    def module_of_chat(self, chat_id: int) -> str | None: ...   # 当前模块（面包屑路由用）
    def set_module(self, chat_id: int, module_id: str | None) -> None: ...

def core_of(context: ContextTypes.DEFAULT_TYPE) -> Core:        # application.bot_data["core"]
```

## 2. ModuleSpec（`mtbots/core.py`）—— 每个 feature 包唯一的对接口

```python
@dataclass(frozen=True)
class ModuleSpec:
    id: str                    # "docker" | "litepan" | "cline"
    icon: str                  # "🐳" | "🎬" | "🤖"
    title: str                 # "Docker 管理" | "LitePan 联动" | "Cline 额度"
    description: str           # 首页一行简介（静态）
    callback_prefix: str       # "d" | "p" | "c"（回调命名空间，见 §4）
    register: Callable[[Application, Core], None]     # 注册自己的 PTB handler（必须）
    commands: Callable[[Core, int], list[tuple[str, str]]] = 空列表    # 菜单片段 (命令名, 描述)
    summary: Callable[[Core, int], Awaitable[str]] | None = None       # 首页总览一行（必须快速、用缓存）
    help_text: Callable[[Core, int], str] | None = None                # 帮助章节（HTML）
    id_lines: Callable[[Core, int], Awaitable[list[str]]] | None = None # 贡献给全局 /id
    open_panel: Callable[..., Awaitable[None]] | None = None           # (core, update, context, *, page=1)
    show_status: Callable[..., Awaitable[None]] | None = None          # /<prefix>_status
    show_list: Callable[..., Awaitable[None]] | None = None            # /<prefix>_list
    rescue: dict[str, Callable] = field(default_factory=dict)           # 兜底命令名 -> async handler(core, update, context)
    startup: Callable[[Core, Application], Awaitable[None]] | None = None  # post_init 里 await
```

* `summary` 只能读缓存/已有状态；**不允许发网络请求或跑 docker**（首页必须秒开）。拿不到就返回 `"点击进入"`。
* `rescue` 里的 handler 会由 router 用「参数覆盖」包装后调用，用于兜住全角斜杠/零宽字符（原 ClinePass 的 `_RESCUE_HANDLERS` 机制）。

## 3. 命令归属（合并后最终菜单）

| 命令 | 归属 | 说明 |
|---|---|---|
| `/start` `/home` `/menu` | router | 🏠 首页总览（`/menu` = 模块菜单，不再等于 LitePan 的 setMyCommands） |
| `/help` | router | 合并帮助，只渲染有权限的章节 |
| `/id` | router | 汇总各模块 `id_lines` |
| `/jobs` | router | 统一任务中心 |
| `/cancel` | router | 取消当前会话的等待态（清 pending 输入） |
| `/status` | router | **按当前模块消歧**：无模块→首页；docker→容器状态；litepan→规则/盘状态；cline→额度面板 |
| `/list` | router | **按当前模块消歧**：无模块→docker 列表；docker→项目列表；litepan→规则列表；cline→docker 列表 |
| `/d_status` `/d_list` | docker | 永久别名（显式指定模块） |
| `/p_status` `/p_list` | litepan | 同上（`/p_status` == 原 `/info`） |
| `/c_status` | cline | 同上（`/c_status` == 原 `/status`） |
| `/upgrade` `/prune` | docker | 原样保留 |
| `/refresh` `/refresh_<slug>` `/strm` `/run` `/ping` | litepan | 原样保留（`/refresh_<slug>` 由正则 MessageHandler 处理，不属于 CommandHandler） |
| `/p_menu` | litepan | 原 `/menu`：强制刷新 LitePan 规则菜单 |
| `/quota` `/addkey` `/delkey` `/keys` `/clear` | cline | 原样保留 |

**PTB 注册顺序要求**：同一 group 内先注册的 handler 先匹配。所以
`app.py` 先注册 router 的全局 `CommandHandler`（`start/help/id/jobs/home/menu/cancel/status/list`），
再让各模块 `register()`。模块**不得**注册 `status/list/start/help` 这四个名字。

> **集成修订（最终生效）**：`/d_status` `/d_list` `/p_status` `/p_list` `/c_status` 这五个永久别名
> 由 **router 统一注册**（它会先写入当前模块上下文再委派给 `show_status`/`show_list`），
> 模块**不要**再注册它们，否则会出现同名 handler 抢注。
> 模块仍然在 `commands()` 里把它们列进菜单、在 `rescue` 里保留同名的兜底项。
>
> **另一个坑（PTB 语义）**：`Application.process_update` 会遍历**每一个 group**，`break` 只跳出
> 当前 group 的 handler 列表。所以「用第二个 group 当兜底」是错的——第一个 group 已经处理过的
> 更新，第二个 group 的 handler 照样会执行（命令被执行两次）。
> MTBots 的做法是：兜底（未知命令 / 已下线模块的按钮）留在**同一个 group**，由 `mtbots/app.py`
> 在**所有模块 `register()` 之后**最后注册，靠「同组内首个匹配者执行后 break」保证只跑一次。
> 因此模块可以放心在自己的 `register()` 里加消息级 handler（例如 LitePan 的 `/refresh_<slug>`），
> 它会先于兜底匹配。

## 4. 面板与回调（`mtbots/panels.py`）

```python
class PanelManager:
    async def render(self, module_id: str, update: Update, text: str,
                     keyboard: InlineKeyboardMarkup | None = None, *,
                     force_new: bool = False, chat_id: int | None = None) -> None:
        """把 text 渲染成「本会话本模块的唯一面板消息」：自动加面包屑与时间戳、末尾补 [🏠 返回]、
        能编辑就原地编辑（BadRequest: Message is not modified 静默忽略），超长自动分片。"""

    async def send(self, chat_id: int, text: str, keyboard=None,
                   parse_mode: str = "HTML") -> Message: ...

    async def ask_confirm(self, module_id: str, update: Update, text: str,
                          confirm_data: str, *, cancel_data: str | None = None) -> None:
        """两步确认面板：[✅ 确认]=confirm_data、[❌ 取消]=cancel_data|'nav|home'；
        登记 (confirm_data -> (发起人 uid, 截止 monotonic))。"""

    def validate_confirm(self, query: CallbackQuery, data: str) -> tuple[bool, str]:
        """消费确认令牌并校验「本人 + 未过期」；返回 (ok, 失败提示文案)。"""

    def forget(self, chat_id: int, module_id: str) -> None: ...

def cb(prefix: str, action: str, payload: dict | None = None) -> str:
    """生成短回调数据 '<prefix>|<action>|<8位hex>'，payload 存内存表（超 800 条清旧）。
    payload=None 时返回 '<prefix>|<action>'。长度必须 < 64 字节。"""

def cb_parse(data: str) -> tuple[str, str, dict | None]:
    """('d','page_turn',{'page':2})；无法解析时返回 ('', data, None)。"""

def cb_args(data: str) -> tuple[str, str]:   # ('d','page_turn')
def nav_home() -> str:  return "nav|home"
def nav_open(module_id: str) -> str: return f"nav|open|{module_id}"
```

**命名空间**：`d|…` docker、`p|…` litepan、`c|…` cline、`nav|…` router、`job|…` 任务中心。
模块用 `CallbackQueryHandler(handler, pattern=r"^d\|")` 只吃自己的回调；router 吃 `^nav\|` 与 `^job\|`。
**过期回调**（内存表被清理/Bot 重启）必须给出「菜单已过期」提示并删/改消息，不允许静默。

**渲染规范**：正文由 `panels.render` 统一加
头部 `🏠 › 🐳 Docker 管理`、尾部 `🔄 HH:MM:SS`；模块正文里用 `mtbots.text` 的符号语义
（✅成功 ❌失败 ⚠️接近阈值 ⛔️超阈值 ⏳进行中）。

## 5. 任务中心（`mtbots/jobs.py`）

```python
@dataclass
class Job:
    id: str; module: str; title: str
    status: str = "running"          # running|done|failed|cancelled
    detail: str = ""; progress: int | None = None
    started_at: float; finished_at: float | None = None
    cancel: Callable[[], None] | None = None

class JobCenter:
    def add(self, module: str, title: str, *, detail: str = "", progress: int | None = None,
            cancel: Callable[[], None] | None = None) -> Job: ...
    def update(self, job: Job, *, detail: str | None = None, progress: int | None = None) -> None: ...
    def finish(self, job: Job, status: str = "done", detail: str = "") -> None: ...
    def running(self, module: str | None = None) -> list[Job]: ...
    def recent(self, limit: int = 5) -> list[Job]: ...
    def render(self, icons: dict[str, str]) -> str: ...           # /jobs 面板正文
    async def announce(self, bot: Bot, chat_id: int, job: Job, *,
                       actions: InlineKeyboardMarkup | None = None) -> None:
        """长任务跑完推一条带模块标签的卡片（含跨模块下一步按钮）。"""
```

长任务（docker 升级/清理、litepan 回执轮询）必须注册 Job；完成推送带 `🐳/🎬/🤖` 标签。

## 6. 命令菜单（`mtbots/menu.py`）

```python
class MenuManager:
    def __init__(self, core: Core, base: list[tuple[str, str]]): ...
    def set_module_commands(self, module_id: str, entries: list[tuple[str, str]],
                            *, scope_chats: Sequence[int] | None = None) -> None: ...
    def clear_module(self, module_id: str) -> None: ...
    def render_for(self, user_id: int) -> list[BotCommand]: ...   # 全局 + 有权限模块片段（去重、≤100）
    async def apply(self, bot: Bot, *, force: bool = False,
                    chats: Sequence[int] | None = None) -> bool: ...
```
* 菜单是**合并**出来的：任何模块都不得自己调用 `set_my_commands`。
* `apply()` 内容未变化时**不发请求**（保留原 LitePan 的去重），失败只 warning 返回 False。
* LitePan 动态 `refresh_<slug>` 有预算（默认 30 条，`LITEPAN_MENU_BUDGET`），超出的规则进内联键盘分页。

## 7. 各 feature 包的验收标准

### 7.1 docker（← LDMG `bot.py`）
* 保留：compose 探测（`docker compose` → `docker-compose`）、`docker compose ls -a` 扫描 + 15s TTL 缓存、
  `docker compose config --services` 服务列表、项目按「运行中优先 + 名称」排序（编号命令与面板同序）、
  执行锁（同时只跑一个 compose 任务）、`COMMAND_TIMEOUT`、pull 噪音行过滤、进度原地编辑 +
  `edit_html_safe` 降级、`task_cancel` 取消（杀进程组）、prune 候选扫描 + 两步确认、
  `/upgrade 01 | 01 emby | all` 三种用法、容器状态速览 `/d_status`。
* 改造：全局变量收进 `DockerState`（`core.data["docker"]`）；回调前缀 `d|`；主面板/详情/确认走 `core.panels`；
  升级/清理注册 `core.jobs`；`summary()` 用缓存给出「N 个项目可升级」。
* 权限：`core.acl.can(user_id, "docker")`。

### 7.2 litepan（← LitePan-TGBot `tgbot.py`）
* 保留（照搬逻辑，不重写）：`UserProfile` 全部字段与 `users.json` 字段名、单用户 `LITEPAN_*` env 兜底、
  60s 发现缓存 + 单飞锁、pypinyin slug（限长 24 / 重名加 `_2`）、`rule_by_slug` / `slugs`(账号) /
  `account_rules` / `by_account`、`_discovery_failed` 与「未开启」的区分、`/refresh`（无参=全量规则优先，
  否则所有规则；带盘名=精确触发）、`/refresh_<slug>`（按规则 ID 精确执行，回退事件触发）、`/run`、
  `/strm`、`/ping`、`/p_menu`、回执轮询（`max_run_id` 快照 + 终态判定 + 每个 run 只回执一次 + 超时提示）、
  `render_result` 步骤回显、管理员登录 401 重登、`_AUTH_LOCK`。
* 改造：**所有同步 HTTP 一律 `await asyncio.to_thread(...)`**；`threading.Thread` 回执轮询改为
  `asyncio.create_task` + `core.jobs`（`asyncio.Event` 做取消）；`say()` → `context.bot.send_message`
  或 `core.panels.send`；`setMyCommands` → `core.menu.set_module_commands("litepan", …)` + `core.menu.apply(bot)`；
  新增内联键盘规则分页（`p|rule_page|<n>`、`p|run_rule|<id>`），`open_panel` = `/p_status` 面板，
  `show_list` = 规则分页。
* 权限：`core.acl.can(user_id, "litepan")` + chat→profile 绑定（未绑定给明确提示）。

### 7.3 cline（← ClinePass `bot.py` + `core.py`）
* `core.py`：几乎原样移植（`Settings` 改为从 `mtbots.config.Settings` + env 构造；`ConfigStore` 落到
  `data/config.json`，原子写 + 0600；`redact`/`RedactingFilter` 改为从 `mtbots.logging_setup` 再导出，
  保证 `tests/` 里 `from mtbots.features.cline.core import redact` 仍可用）。
* `handlers.py`：保留 `/addkey`（撤回含 Key 的消息、不可见字符清洗、指纹）、`/delkey`、`/keys`、
  `/clear confirm`、`/quota`、`/c_status`；`_guard` 接 `core.acl`（默认拒绝，替换原来「留空=所有人可用」）；
  额度面板 `core.panels.render("cline", …)`；`STATUS_COOLDOWN` 保留；`_safe_args` 脱敏保留。
* 贡献 `/id` 的 `id_lines`（容器名、config 路径、存储自检、已绑定数量）与 `help_text`、`summary`（用上次
  快照，不主动请求；`DEMO_MODE` 保留）。
* 兜底救援：把原 `_RESCUE_HANDLERS` 交给 `rescue` 字段（不自己注册 MessageHandler）。

## 8. 兼容与迁移

* 三个旧 bot 的**环境变量名全部保留**（`BOT_TOKEN`/`TELEGRAM_BOT_TOKEN`/`TG_BOT_TOKEN` 归一处理，
  `ALLOWED_USER_IDS`/`TG_ALLOWED_IDS` 取并集），旧 `.env` 和 `users.json`/`config.json` 可以直接搬过来。
* 统一 token：`MTBOTS_BOT_TOKEN` > `TELEGRAM_BOT_TOKEN` > `BOT_TOKEN` > `TG_BOT_TOKEN`；只允许**一个**在轮询。
* 旧 `config.json`（ClinePass）与 `users.json`（LitePan）分别落在 `data/config.json`、`data/litepan-users.json`。
* 启动即校验配置：`python -m mtbots --check` 打印每个模块的可用状态与缺失项，不连 Telegram。
