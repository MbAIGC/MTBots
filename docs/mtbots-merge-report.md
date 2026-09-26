# MTBots 合并施工报告

> 依据：[three-bots-merge-design.md](three-bots-merge-design.md)（可行性 + 交互设计）、
> [three-bots-merge-ux.md](three-bots-merge-ux.md)（单人版交互图）、[porting-contract.md](porting-contract.md)（施工契约）。
> 结果：三个 Bot 已合并为 **一个进程、一个 Python 包 `mtbots`**（Bot 名 MTBots，方案 A：单进程模块化），可运行、可自检、**314 个测试全绿**。

---

## 1. 合并了什么

| 原 Bot | 原形态 | 合并后 | 保留的行为 |
|---|---|---|---|
| **LDMG** `/root/workspace/LDMG/bot.py`（1464 行） | PTB 21.6 异步，单文件 | `mtbots/features/docker/` | compose 探测/扫描缓存、项目分页与编号升级、单任务执行锁、流式进度与 HTML 降级、进程组取消、镜像清理两步确认、容器状态速览 |
| **LitePan-TGBot** `/root/LitePan-TGBot/tgbot.py`（1290 行） | **纯 stdlib 同步长轮询**（自己 `getUpdates`） | `mtbots/features/litepan/` | 自动发现（60s 缓存 + 单飞）、pypinyin slug（限长 24/去重）、按规则 ID 精确执行、`/refresh <盘名>`、回执轮询与 `render_result`、401 重登、`users.json` 多用户绑定 |
| **ClinePass-TG-Bot** `/root/ClinePass-TG-Bot/core.py`+`bot.py`（1250+639 行） | PTB 异步 + 纯逻辑层 | `mtbots/features/cline/` | 原子写 0600 的 Key 存储（含脏 Key 自愈）、额度接口重试与宽容解析、掩码/指纹对账、含 Key 消息自动撤回、不可见字符清洗、命令兜底救援 |

**新建的公共层**（原来三家各写各的）：`core/ModuleSpec`、`panels`（单会话单面板 + 面包屑 + 两步确认）、
`jobs`（跨模块任务中心）、`menu`（合并命令菜单 + 按会话作用域）、`acl`（默认拒绝 + 角色）、
`store`（原子写 0600）、`logging_setup`（全局脱敏）、`text`（HTML 安全分片 / 进度条 / 符号语义）、`router`（首页 + 消歧）。

### 代码量

| 层 | 文件 | 行 |
|---|---|---|
| 公共核心 | `mtbots/*.py` | 2690 |
| 三个 feature | `mtbots/features/**` | 6349 |
| 测试 | `tests/**` | 3839 |
| 文档 + 打包 | `docs/*`、`README.md`、`Dockerfile`、`docker-compose.yml`、`Makefile`、`.env.example` | 1427 |

## 2. 关键改造点（为什么必须改）

| 改造 | 原因 |
|---|---|
| LitePan 的 `getUpdates`/offset/`threading.Thread` 全部删除，业务逻辑改为 PTB handler | 一个 Bot Token 只能有一个轮询者；且同步 HTTP 会冻住另两个模块的按钮 |
| LitePan 所有 HTTP 走 `asyncio.to_thread`，回执轮询由线程改为 `asyncio` 任务 + `Job` | 同上；同时让回执进入任务中心，可取消、可视化 |
| LitePan 不再调 `setMyCommands`，改为向 `MenuManager` 提交片段（预算 30 条） | Telegram 全局限 100 条命令，三个模块共享；原实现每次刷新都会擦掉另两个模块的菜单 |
| `/status` `/list` `/start` `/help` 由路由按「当前模块」解释，另给 `/d_*` `/p_*` `/c_*` 永久别名 | 4 个真冲突命令必须有确定性语义（设计稿 §5.4） |
| ClinePass 白名单「留空 = 所有人可用」→ 全局默认拒绝 | 合并后任一模块的鉴权缺口都会变成整机缺口 |
| 三个模块的长任务统一注册进 `JobCenter`，完成推送带模块标签与跨模块下一步按钮 | 合并才有的能力：一个任务中心 + 一次点击跨模块跳转 |
| LDMG 的模块级可变全局收进 `DockerState`（`core.data["docker"]`） | 同进程多模块 + 测试需要独立实例 |
| 兜底 handler 由 `app.py` 在所有模块注册完之后**最后**注册（同一个 group） | PTB 的 `process_update` 会遍历每一个 group，用第二个 group 当兜底会让已处理的命令再执行一次（实测踩到）；同组内「首个匹配者执行后 break」才是正确的兜底语义 |

## 3. 命令归属（最终效果）

* **全局**：`/start` `/home` `/menu` `/help` `/status` `/list` `/jobs` `/id` `/cancel`
* **🐳 Docker**：`/d_list` `/d_status` `/upgrade` `/prune`
* **🎬 LitePan**：`/refresh` `/refresh_<规则>` `/refresh <盘名>` `/strm` `/run` `/info` `/ping` `/p_list` `/p_status` `/p_menu`
* **🤖 Cline**：`/c_status` `/quota` `/addkey` `/delkey` `/keys` `/clear`
* **消歧**：`/status` 与 `/list` 按当前模块解释（无模块 → 首页总览 / Docker 列表）；`/d_*`、`/p_*`、`/c_*` 是永久别名
* **别名归属**：`/d_status` `/d_list` `/p_status` `/p_list` `/c_status` 统一由 `router.ALIASES` 注册（先切模块上下文再委派给模块的
  `show_status`/`show_list`）。模块的 `register()` 不再注册同名命令；docker 额外做了「router 没注册才注册」的自适应，
  这样单模块直连也能用（见 §6 偏差 1）。

## 4. 验证

```bash
cd /root/DSH/MTBots
PYTHONPATH=./.vendor:. python3 -m unittest discover -s tests -t .   # 314 tests OK
PYTHONPATH=./.vendor:. python3 -m mtbots --check                      # exit 0，离线
PYTHONPATH=./.vendor:. python3 -m mtbots --health                     # 真实探测（compose / LitePan / Cline 存储）
```

| 测试文件 | 用例 | 结果 |
|---|---|---|
| `tests/test_core.py` | 56 | OK（含 `safe_html` / `SafeBot` 降级的出口兜底用例） |
| `tests/test_docker_module.py` | 41 | OK（含「把 `subprocess` 全换成抛异常的桩」反证 + 扫描失败诊断） |
| `tests/test_litepan_module.py` | 58 | OK |
| `tests/test_cline_module.py` | 140 | OK |
| `tests/test_integration.py` | 19 | OK（5 个装配 + 12 个真 Update 端到端 + 2 个 HTML 守卫用例） |
| **合计** | **314** | **OK（约 5.1s，无网络）** |

端到端用例（`DispatcherTests`）用**真正的 `telegram.Update` + 记录型假 Bot** 跑 PTB 自己的
`Application.process_update`，因此能抓到装配级事故：

* `/start` 只渲染一次（兜底没有再执行一遍）；
* 点按钮触发的渲染**原地编辑同一条面板**（home / docker / litepan / cline 共用一条），命令触发时新发到最底部并删掉旧面板；
* 全角 `／start`、代码块里粘出来的命令能被兜底救援；
* 未启用模块留下的按钮会弹「该功能当前不可用」，而不是一直转圈；
* 每个面板文案都要过一次 Telegram HTML 合法性校验（`tests/htmlcheck.py`）。

装配期硬约束（`tests/test_integration.py` 自动断言）：

1. 三个真实模块在同一个 `Application` 里共存，`/start /help /status /list` **各只有一个** handler，且都归路由（注册序号最小）；
2. 三个模块的 handler 不得抢注这四个命令，也不得重复注册别名（`d_list`/`c_status` 曾在此处被抓出来）；
3. 合并菜单包含基础命令 + 每个模块贡献的片段（`MenuManager` 内容未变时**不重复请求** Telegram）；
4. 首页只渲染有权限的模块（普通用户看不到 Docker 运维入口）；
5. `python -m mtbots --check` 在没有 Telegram 连接的情况下退出码为 0。

实测 `--check` 关键行（本机，未配 LitePan）：

```
✅ 基础配置完整
🐳 Docker 管理（docker） 每页 6 个项目｜单命令超时 300s｜扫描缓存 15s
🎬 LitePan 联动（litepan） 用户配置文件：/…/litepan-users.json（不存在（将使用 .env 单用户模式））
🤖 Cline 额度（cline） 存储：✅ 可读写（… 权限 0o600）
```

## 5. 与原三个 Bot 的行为差异

| 变化 | 影响/说明 |
|---|---|
| `/menu` 从「刷新 LitePan 菜单」变成「打开模块菜单」 | 强制刷新菜单改为 `/p_menu`；`/menu` 在合并语义下必须唯一 |
| LitePan 规则命令进菜单的数量有预算（`LITEPAN_MENU_BUDGET`，默认 30） | 超出部分在 `/p_list` 内联键盘里分页触发，能力不减 |
| LitePan 规则命令按会话作用域下发（`BotCommandScopeChat`） | 顺手修掉原实现「多人使用时菜单互相覆盖、规则名互相可见」的问题；当前受 `MenuManager` 限制，会话间共用一份片段 |
| ClinePass 的 `/status` 变为 `/c_status`（`/status` 现按模块解释） | `/quota` 仍可直接使用；在 Cline 模块内 `/status` 等价 |
| LitePan 发现/回执未拆成独立开关（仍以「配了管理员账号」为整体条件） | 行为与旧版一致，未退化；见后续工作 |
| LDMG 以**进程内模块**接入，而非设计稿建议的 sidecar + `docker-socket-proxy` | 单人自用可接受；已用 ACL 把 `docker` 限制在 owner/admin，并提供只读根 + 非 root + `no-new-privileges`。多用户建议补 socket-proxy |
| 群里「回复某条面板消息定位上下文」未实现 | 单人私聊为主；面板按会话唯一（home/docker/litepan/cline 共用一条） |

## 6. 有意偏差（施工中为了集成做的调整）

1. **别名归属上移**：`/d_list` `/d_status` `/p_status` `/p_list` `/c_status` 从模块搬到了 router。
   原因：同 group 内先注册者优先，模块再注册就是死代码，且集成测试要求「同一命令不得注册两次」。
   docker 保留了「router 未注册时才注册」的自适应分支（单模块直连场景可用），litepan/cline 直接在注释里写明由 router 持有。
2. **`RedactingFilter` 补齐 `exc_info` 分支**：原实现只在 `record.exc_text` 已存在时脱敏，
   `log.exception(...)`（PTB 的 `InvalidToken` 会把明文 Token 放进 traceback）会在到达 handler 前漏掉。
   现在会先 `formatException` → `redact` → 写回 `exc_text` 并清空 `exc_info`，`tests/test_core.py` 有用例锁死。
3. **`StoreError.kind`**（`corrupt` / `shape` / `io`）、`PanelManager.render(..., limit=)`、`Core.state(module_id)`：
   都是模块在移植时提出、由集成层补的公共 API（见 §7）。
4. LitePan `LitePanConfig` **不抛异常**（配置坏了只 `enabled=False` + 明确文案），避免单模块配置拖垮整个 bot。
5. docker 扫描/`ps` 走 `asyncio.to_thread(subprocess.run)` + 超时预算；升级仍是 `create_subprocess_exec` + `killpg`（可取消）。
6. 所有面板渲染前统一 `query.answer()`，并对「模块已下线/菜单过期」的孤儿回调给了明确弹窗（router 按**未启用模块的前缀**注册精确兜底）。
7. **兜底 handler 从「group=1」改回「同组最后注册」**：PTB 的 `process_update` 会遍历所有 group
   （`break` 只跳出当前 group），group=1 的兜底会在 group=0 已经处理过之后**再执行一次**——
   端到端测试里表现为「`/start` 执行两次、每个命令都多渲染一次」。现在 `router.register()` 只注册
   路由命令，`router.register_fallback()` 由 `app.py` 在模块之后调用；`tests/test_integration.py::DispatcherTests`
   用真 `telegram.Update` + 假 Bot 跑 PTB 自己的 dispatcher 把这条锁死（全角 `/／start` 仍能被救援）。

## 7. 模块提出、由集成层落地的 core API

| 请求 | 处理 |
|---|---|
| `RedactingFilter` 漏 `exc_info` 的 traceback | ✅ 已修（安全项），含回归用例 |
| `StoreError` 要能区分「JSON 坏」与「顶层不是对象」 | ✅ 已加 `kind`（`corrupt`/`shape`/`io`） |
| `PanelManager.send/render` 缺 `limit=` | ✅ 已加（keyword-only，默认 `MESSAGE_LIMIT=3800`） |
| `Core` 缺模块私有状态 helper | ✅ 已加 `Core.state(id)`，且不会覆盖 `data[id]` 里的自定义对象（如 `DockerState`） |
| `ModuleSpec.commands_owned`（命令归属声明） | ❌ 未加：router 的 `ALIASES` 就是归属表，重复注册已由集成测试拦住；加字段会带来第二份真相 |
| `JobCenter.drop()`（快任务不进 `/jobs`） | ❌ 未加：`JobCenter` 会自动剪枝（保留最近 50 条），加 `drop` 只会让「查询到底跑没跑」变得不可追溯 |
| 菜单片段按会话差异化 | ❌ 未做：与「内容不变不发 API」的去重签名冲突，收益低于复杂度；已写进 README 的已知限制 |
| 把 `parse_command_tokens` 从 cline 提到 `mtbots.text` | ❌ 未做：router 侧只做了**惰性可选导入**（纯兜底解析），不构成硬耦合；移动会让 cline 的 140 个用例一起改 |

## 8. 后续工作（按价值排序）

1. **多用户硬化**：把 `docker` 拆到 sidecar / `docker-socket-proxy`，主进程只发 HTTP（设计稿 §4 方案 B 的收益在这里）；
2. **LitePan 发现/回执解耦**：`discovery.enabled` 与 `receipt.enabled` 分开配置（设计稿 §9.2 ④）；
3. **角色管理界面**：`/id` 已能看角色，可加 owner 专用的角色增删命令，替代手写 `MTBOTS_ROLES`；
4. **真实环境联调**：用测试 token 跑通「首页 → Docker 升级 → LitePan 重跑 → 回执」这条主线（设计稿图 4）；
5. `docker-compose.yml` 接入可选的 `docker-socket-proxy` 服务。

## 9. 启动

```bash
cp .env.example .env      # 填 MTBOTS_BOT_TOKEN 与 ALLOWED_USER_IDS
python -m mtbots --check    # 先自检
python -m mtbots            # 启动
# 或（镜像由 GitHub Actions 构建推 GHCR，公开可匿名拉，amd64 + arm64）
DOCKER_GID=$(getent group docker | cut -d: -f3) docker compose up -d
# 想就地构建就加 --build
DOCKER_GID=$(getent group docker | cut -d: -f3) docker compose up -d --build
```

## 10. 修复记录（v1.0.1，线上反馈驱动）

首轮上线后收到三条反馈，全部已修并补了回归测试（314 个用例）：

| 反馈 | 根因 | 修法 |
|---|---|---|
| 「第一次 `/start` 有效，后面再点就没反应」「🏠 返回 很多地方无效」 | 面板按 `(会话, 模块)` 各存一条：点「返回」改的是**另一条**消息（home 面板），而用户视线停在模块面板上；命令触发时也只编辑旧面板，旧面板却停在刚发出的命令**上方**，编辑了看不见 | ① `PanelManager` 改为**一会话一条消息**（`_panels: chat_id -> message_id`），返回/切模块都改用户正看的那条；② 命令触发的渲染新发到最底部并删掉旧面板；③ `global_error_handler` 补 `query.answer`，异常时按钮不再一直转圈 |
| LitePan 报 `Can't parse entities: unsupported start tag "盘名"` | 文案里写了字面量 `<盘名>`、`<规则>`、`<事件>`，HTML 模式下被当成标签，整条消息被 Telegram 拒绝（`/id` 里的 `<DATA_DIR>`、Cline 的 `<{n}字符>` 同理） | ① 新增 `mtbots/bot.py::SafeBot`（`ExtBot` 子类，用 `Application.builder().bot(...)` 注入）：出站 HTML 一律先把白名单外的 `<` 转义，真解析失败再降级纯文本重发——模块漏 `esc()` 也不会整条挂掉；② 文案占位符改成全角 `〈盘名〉`；③ `tests/htmlcheck.py` + 集成用例把**每个面板文案**离线校验一遍 |
| Docker「⚠️ 暂未检测到任何 Docker Compose 项目」，怀疑权限不够 | `scan_projects_sync` 里 `docker compose ls` 非 0 退出、以及「项目目录没挂进容器」两种情况都是**静默**返回 `[]` | `DockerState` 记录 `last_scan_error` / `hidden_dirs`，新增 `scan_hint()`：区分 permission denied、连不上守护进程（挂 `/var/run/docker.sock`）、目录未挂载（按相同路径挂载），并在 `d_list` 面板与 `--health` 里显示。permission denied 时再由 `socket_group_hint()` **实测** socket 属组与进程附加组，直接给出要填的 `DOCKER_GID` 数字，并点明「`restart` 不生效、要 `--force-recreate`」以及 socket 属 `root:root`（NAS 常见）时 `group_add` 无效的两条出路 |
