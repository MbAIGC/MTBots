# MTBots —— 三个 Telegram Bot 合并成一个

[![docker](https://github.com/MbAIGC/MTBots/actions/workflows/docker.yml/badge.svg)](https://github.com/MbAIGC/MTBots/actions/workflows/docker.yml)
[![image](https://img.shields.io/badge/ghcr.io-mbaigc%2Fmtbots-2496ED?logo=docker&logoColor=white)](https://github.com/MbAIGC/MTBots/pkgs/container/mtbots)
![platform](https://img.shields.io/badge/platform-linux%2Famd64%20%7C%20linux%2Farm64-informational)

> 把 **LDMG**（宿主机 Docker Compose 升级/清理）、**LitePan-TGBot**（远程触发 LitePan 媒体自动化）、
> **ClinePass-TG-Bot**（多 Key 额度面板）合并为**一个进程、一个 token、一套权限、一个任务中心**的 Bot：`MTBots`。
>
> 合并方案来自本仓库 `docs/` 下的设计稿（方案 A：单进程模块化），施工契约见
> [docs/porting-contract.md](docs/porting-contract.md)，交互图见 [docs/three-bots-merge-ux.md](docs/three-bots-merge-ux.md)，
> 施工与验证记录见 [docs/mtbots-merge-report.md](docs/mtbots-merge-report.md)。

```
              ┌───────────────────────────────────────────────┐
   /start ───►│  🏠 控制台（首页总览，按权限渲染）             │
              │  🐳 Docker   🎬 LitePan   🤖 Cline            │
              │  🧰 任务中心（跨模块长任务）                   │
              └───────────────┬───────────────────────────────┘
                              │ 模块 = 命名空间，由「当前位置」决定
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
   🐳 Docker 管理        🎬 LitePan 联动        🤖 Cline 额度
   /upgrade /prune       /refresh /run /info    /addkey /keys /quota
   /d_status /d_list     /p_status /p_list      /c_status
```

## 一句话原则

**模块 = 命名空间；命名空间由「当前所处的位置」决定，而不是靠命令前缀。**

所以三边命令取并集后**只有 4 个真冲突**（`/start` `/help` `/status` `/list`），其余全部原样保留，
老用户肌肉记忆基本不用改；冲突的四个按「你现在在哪个模块」解释，并都有永久别名兜底。

## 快速开始

```bash
cp .env.example .env
# 必填两项：
#   MTBOTS_BOT_TOKEN=123456:ABC...      （旧名 TELEGRAM_BOT_TOKEN / BOT_TOKEN / TG_BOT_TOKEN 也认）
#   ALLOWED_USER_IDS=你的用户ID        （默认拒绝：留空 = 谁都不能用；用 /id 查询）
python3 -m mtbots --check          # 配置自检，不连 Telegram
python3 -m mtbots                  # 启动
```

Docker 部署（含 docker CLI，Docker 模块靠它升级宿主机 compose 项目）。
镜像由 GitHub Actions 在每次 push 到 `main` / 打 `v*` tag 时构建，推 `ghcr.io/mbaigc/mtbots`，
**公开、可匿名拉取**，同时带 `linux/amd64` 与 `linux/arm64`：

```bash
cp .env.example .env                 # 填 MTBOTS_BOT_TOKEN 与 ALLOWED_USER_IDS
mkdir -p data && sudo chown 10001:10001 data
export DOCKER_GID=$(getent group docker | cut -d: -f3)

docker compose up -d                 # ① 用 GHCR 上的现成镜像（最快）
docker compose up -d --build         # ② 或本地构建（compose 里同时写着 build: .）
docker compose logs -f
```

不想用 compose 就直接 docker run：

```bash
docker run -d --name mtbots --restart unless-stopped \
  --env-file .env \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /docker:/docker \
  -v "$PWD/data:/app/data" \
  --group-add "$DOCKER_GID" \
  ghcr.io/mbaigc/mtbots:latest
```

> 切换上线时建议**换一个新 token**：旧 bot 还在轮询同一个 token 会导致 409 冲突。

## 命令速查

### 🌐 全局（唯一，无歧义）

| 命令 | 作用 |
|---|---|
| `/start` `/home` `/menu` | 🏠 首页总览（模块与任务一屏看全；顶部显示当前版本号 `MTBots vX.Y.Z`） |
| `/help` | 合并帮助，**只渲染你有权限的章节** |
| `/status` | 当前模块状态；无模块上下文时 = 首页总览 |
| `/list` | 当前模块列表；无模块/在 Cline 中 = Docker 项目列表 |
| `/jobs` | 🧰 任务中心（跨模块长任务，可取消） |
| `/id` | 用户 ID、会话 ID、角色、容器、各模块自检 |
| `/cancel` | 取消当前等待中的操作与输入 |

### 🐳 Docker（原 LDMG）

`/d_list`（项目面板）、`/d_status`（容器实时状态）、`/upgrade 01`、`/upgrade 01 emby`、`/upgrade all`、`/prune`

### 🎬 LitePan（原 LitePan-TGBot）

`/p_status`（＝原 `/info`：连接状态/规则/盘名）、`/p_list`（规则分页面板，点按钮触发）、
`/refresh`（全量规则优先，否则全部规则）、`/refresh <盘名>`、`/refresh_<规则>`（按规则 ID 精确执行）、
`/strm`（＝`/refresh`）、`/run <事件> [path]`、`/ping`、`/p_menu`（强制刷新命令菜单）

### 🤖 Cline 额度（原 ClinePass-TG-Bot）

`/c_status` `/quota`（额度面板）、`/addkey <别名> <KEY>`、`/delkey <别名>`、`/keys`、`/clear confirm`

### 消歧规则（4 个冲突命令）

| 输入 | 无活动模块 | 🐳 Docker 中 | 🎬 LitePan 中 | 🤖 Cline 中 |
|---|---|---|---|---|
| `/status` | 首页总览 | 容器实时状态 | 规则 / 盘状态 | 额度面板 |
| `/list` | Docker 项目列表 | 项目列表 | 规则列表 | Docker 项目列表 |

需要显式指定：`/d_status`、`/d_list`、`/p_status`、`/p_list`、`/c_status`。

## 交互约定：一次操作一条消息

三个 Bot 合进一个进程后，最容易乱的不是命令而是**消息**：同一次操作东一条西一条，用户根本追不上。
现在的规则统一成六条：

1. **一个会话只有一条面板**。返回、切模块、翻页都改**用户正看着的那条**；命令触发的渲染会新发到最底部并顺手删掉旧面板（不然编辑了旧面板用户也看不见）。
2. **长任务收尾只留一条**。交互式任务（有面板可改）把结果**画在面板上**，不再另发「✅ 🐳 升级项目 mt」卡片；每一步的执行消息（`✅ 拉取新镜像 - mt 完成`、`✅ 重建与启动 - mt 完成`）在成功后自动删除，**失败/取消/超时一律保留**——那几条输出就是排错依据。镜像清理同理，但会把 `Total reclaimed space` 抄进面板，信息不丢。
3. **只有真后台任务才推卡片**。例如 LitePan 的规则回执（触发几分钟后才出结果，没有面板可改），用同一份 `jobs.card_text()` 文案单发一条。
4. **收尾键盘给「下一步」**。一行只放其他**已启用且有权限**的模块入口（当前模块不重复给，也不放任务中心——首页里就有）；`🔙 返回列表` 会回到**你刚才那页**，不是永远回第 1 页。模块入口超过 3 个就整体不显示，只留 `🏠 返回`。
5. **跑的时候面板上能中断**。进度面板的键盘是 `[🛑 中断执行] [🧰 任务中心]`——用户视线就在这条消息上，取消不必再去翻那条随时会消失的执行消息。
6. **失败时把命令尾巴抄进面板**。收尾面板除了 `❌` 结论，还会带 `🔻 最后输出` 的最后 2–3 行（批量升级最多 6 行），不用上滑去找那条执行消息。取消的项目单独记成 `⚠️ 已中断：`，不算「失败」。

## 目录结构

```
mtbots/
├── app.py            # 组装：Core + 路由 + 三个模块 → 一个 Application
├── config.py         # 全局配置（兼容三家全部旧环境变量名）
├── acl.py            # 角色 + 模块权限（默认拒绝）
├── logging_setup.py  # 统一日志与全局脱敏（Token / API Key / 邮箱 / 密码）
├── text.py           # 转义 / HTML 安全分片 / 进度条 / 时间人性化 / 状态符号
├── store.py          # 原子写 + 0600 的统一 JSON 存储
├── panels.py         # 单会话单面板 + 面包屑 + 🏠 返回 + 两步确认 + 回调载荷表
├── jobs.py           # 🧰 任务中心
├── menu.py           # 合并命令菜单（模块只提交片段，禁止各自 setMyCommands）
├── core.py           # Core 容器 + ModuleSpec（模块唯一对接口）
├── router.py         # 首页 / 面包屑路由 / 冲突命令消歧 / 帮助 / 兜底救援
└── features/
    ├── docker/       # 🐳 原 LDMG：compose 扫描 / 升级 / 清理 / 进度流
    ├── litepan/      # 🎬 原 LitePan：发现 / 规则 / 触发 / 回执轮询（同步 HTTP → to_thread）
    └── cline/        # 🤖 原 ClinePass：Key 存储 / 额度接口 / 面板渲染

仓库根：
├── Dockerfile / docker-compose.yml / .dockerignore   # 镜像与部署（非 root、只读根、自带 docker CLI）
├── .github/workflows/docker.yml                      # CI：跑测试 + 构建 amd64/arm64 镜像推 GHCR
├── Makefile                                          # make check / health / test / run / list
├── docs/                                             # 设计稿、施工契约（porting-contract）、合并报告
└── tests/                                            # 343 个 stdlib unittest 用例
```

## 配置

一份 `.env` 管三个模块，**三家旧变量名全部兼容**：

| 类别 | 变量 | 说明 |
|---|---|---|
| Telegram | `MTBOTS_BOT_TOKEN` / `TELEGRAM_BOT_TOKEN` / `BOT_TOKEN` / `TG_BOT_TOKEN` | 四选一，**只允许一个实例在轮询** |
| 白名单 | `ALLOWED_USER_IDS`、`TG_ALLOWED_IDS` | 取并集；**默认拒绝**（留空 = 谁都不能用） |
| 模块 | `MTBOTS_MODULES` | 默认 `docker,litepan,cline`，可单独下线某个模块 |
| 角色 | `MTBOTS_ROLES=123:owner,456:user` | 默认白名单内全部 `owner`；`docker` 默认只给 owner/admin |
| 数据 | `DATA_DIR`、`CONFIG_FILE`、`LITEPAN_USERS_FILE`、`LOG_DIR` | 默认 `data/`（`config.json` + `litepan-users.json` + `logs/`，均 0600/原子写） |
| 🐳 | `PAGE_SIZE`、`COMMAND_TIMEOUT`、`PROJECTS_CACHE_TTL` | 面板分页、单命令超时、扫描缓存 |
| 🎬 | `LITEPAN_URL`、`LITEPAN_API_KEY`、`DRIVES`、`LITEPAN_ADMIN_USER/PASSWORD`、`LITEPAN_MENU_BUDGET` | 单用户模式；多用户请用 `data/litepan-users.json`（字段名与旧版一致） |
| 🤖 | `CLINEPASS_API_BASE`、`MAX_KEYS_PER_USER`、`STATUS_COOLDOWN`、`DEMO_MODE`、`SHOW_IDENTITY` | 与旧版一致 |

自检：

```bash
python3 -m mtbots --check     # 配置 + 三个模块的静态自检
python3 -m mtbots --health    # 额外探测 docker compose / LitePan 连通性 / Cline 存储可写
python3 -m mtbots --list      # 列出已启用模块
```

## 从旧三个 Bot 迁移

1. 旧 `.env` 可以基本原样搬（变量名全部兼容），白名单取并集；
2. `ClinePass-TG-Bot/config.json` → `data/config.json`（Key 直接可用，原子写 + 0600 不变）；
3. `LitePan-TGBot/users.json` → `data/litepan-users.json`（`chat_ids`/`litepan_url`/`api_key`/`drives`/admin 字段名不变）；
4. `docker compose up -d --build`，用**测试 token** 并行验证 `--check` / `/start` / 三个模块各一条命令；
5. 确认无误后换正式 token，停掉旧三个容器（避免 409 抢占）。

回滚：把旧容器和旧 token 起回来即可，`data/` 两边互不影响。

## 安全红线（合并后的默认姿态）

1. **默认拒绝**：白名单为空时谁都不能用（旧的"ClinePass 白名单留空 = 所有人可用"已被移除）。
2. **统一脱敏**：`redact()` + `RedactingFilter` 覆盖所有 handler 出口（Bot Token、`sk_`/`lpk_` Key、邮箱、管理员密码）；
   API Key 只在 `data/config.json`（0600、原子写），并且 `addkey` 会先撤回含明文 Key 的消息。
3. **Docker 特权集中在一个模块**：`docker.sock` 只被 `features/docker` 使用并受 ACL 限制（`docker` 默认只给 owner/admin）；
   想进一步收窄可换 `docker-socket-proxy`（主进程只发 HTTP，见设计稿 §6）。
4. **密钥渲染带 user_id**：Cline 面板只渲染调用者自己的 Key；LitePan 按 `chat_id` 绑定实例，不串台。
5. **破坏性操作两步确认**：确认按钮绑定发起人 + 60 秒过期（`PanelManager.ask_confirm/validate_confirm`）。
6. **非 root + 只读根文件系统**（compose 已配置 `read_only` / `no-new-privileges`），只有 `data/` 与挂载的 compose 目录可写。

## 测试

```bash
# 需要 python-telegram-bot。仓库约定的本地依赖目录是 .vendor/（已 gitignore）；
# 如果机器上没有 pip，可以这样引导一份：
#   curl -fsSL -o /tmp/pip.pyz https://bootstrap.pypa.io/pip/pip.pyz
#   python3 /tmp/pip.pyz install --target ./.vendor -r requirements.txt

make test          # = PYTHONPATH=./.vendor:. python3 -m unittest discover -s tests -t . -v
make check
```

测试全部是 stdlib `unittest`、不联网也不碰真实 Telegram/Docker（Docker 用例还会把
`subprocess` / `create_subprocess_exec` 换成抛异常的桩做反证）。当前 **343 个用例全绿**：

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `tests/test_core.py` | 66 | 文本分片（HTML 标签闭合）、`safe_html` 出口转义、`SafeBot` 解析失败降级、ACL 默认拒绝、存储原子写/0600/损坏分类、任务中心（运行中显示最后一行输出、终态不再翻转）与收尾卡片文案、跨模块入口按钮的取舍（启用/权限/排不下）、面板唯一与两步确认、菜单去重与作用域、配置兼容、日志脱敏（含 exc_info 的 traceback）、路由消歧与兜底救援 |
| `tests/test_docker_module.py` | 52 | 项目排序/分页、pull 噪音过滤、清理候选、回调载荷、模块装配、**扫描失败诊断（实测 socket GID、未挂载目录的公共挂载点、缺命令）**、执行消息收尾（成功即删、失败必留、结果回传）、失败尾部输出与进度键盘的中断入口 |
| `tests/test_litepan_module.py` | 59 | slug 构建（拼音/限长/去重）、users.json 校验、发现解析与缓存、菜单预算、触发与回执 |
| `tests/test_cline_module.py` | 140 | 额度解析/渲染、Key 掩码与指纹、别名校验、存储读写与自愈、默认拒绝 |
| `tests/test_integration.py` | 26 | 三个真实模块一起装配、命令不重复、菜单合并、`--check` 离线可跑，**真 `telegram.Update` 走 PTB dispatcher 的端到端用例**（不重复执行、全角命令可救援、下线模块的按钮有反馈、点按钮原地改同一条面板、**所有面板文案都过一遍 Telegram HTML 合法性校验**），以及**收尾只留一条消息**（批量升级不再推卡片、执行消息带 `delete_on_success`、收尾面板带跨模块入口、`🔙 返回列表` 回原页、失败抄尾部输出、进度面板可中断、最后一步取消判为取消） |

## 与原三个 Bot 的差异（有意为之）

| 变化 | 原因 |
|---|---|
| LitePan 的 `getUpdates` 轮询、offset 落盘、`threading.Thread` 全部删除，改为 PTB handler + `asyncio.to_thread` | 一个进程只能有一个轮询者；同步 HTTP 不能阻塞 asyncio 事件循环 |
| LitePan 不再自己调 `setMyCommands`，改为向 `MenuManager` 提交片段（预算 30 条，其余进内联键盘分页） | Telegram 全局限 100 条命令，三个模块共享；否则每刷新一次会擦掉另两个模块的菜单 |
| `/menu` 的含义从「刷新 LitePan 菜单」变为「打开模块菜单」，强制刷新菜单改到 `/p_menu` | `/menu` 属全局层，语义必须唯一 |
| 旧的 `/status` `/list` `/start` `/help` 分别由路由统一解释 | 4 个冲突命令需要消歧，见上表 |
| ClinePass 的白名单「留空 = 所有人可用」被移除 | 合并后任一模块的鉴权缺口都会变成整机缺口 |
| 三个模块的长任务统一进 `/jobs`，完成推送带模块标签与跨模块下一步按钮 | 合并才有的能力：一个任务中心 + 一次点击跨模块跳转 |
| `/d_status` `/d_list` `/p_status` `/p_list` `/c_status` 由路由注册并转发给模块 | 别名要先进模块上下文再渲染面板；同 group 内先注册者优先，模块再注册同名命令会变成死代码 |

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| 按钮点了没反应、「🏠 返回」看着无效 | 已修：一条会话只保留**一条**面板消息（home / docker / litepan / cline 共用同一处），点按钮就地改这条，`/start` 这类命令新发到聊天最底部并删掉旧面板。若仍无反应，`docker compose logs -f` 里搜 `HTML 解析失败`：那说明文案里有 Telegram 不认的标签，出口已经自动降级为纯文本，把日志贴出来即可定位。 |
| LitePan 报 `Can't parse entities: unsupported start tag "盘名"` | 已修：`SafeBot`（`mtbots/bot.py`）在出站口把白名单外的 `<` 全部转义，真解析失败时再降级纯文本重发；`tests/htmlcheck.py` 会把**每个面板文案**离线校验一遍，这类事故进不了 CI。 |
| Docker 面板「⚠️ 暂未检测到任何 Docker Compose 项目」 | 面板会直接给出原因；`permission denied` 还会实测 socket 属组并告诉你填哪个 GID，见下面「Docker 读不到项目」。 |
| 点旧按钮提示「菜单已过期」 | 回调里的长载荷（规则名、项目名）存在内存，Bot 重启后失效；重发一次命令即可。 |

### Docker 读不到项目

面板把三种原因分开说。`permission denied` 时会**实测** socket 的属组和容器进程的附加组，直接给出要填的数字：

```
⚠️ 读不到 Docker：permission denied —— 容器里的 mtbots 用户没有 /var/run/docker.sock 的权限。
   实测：容器里 /var/run/docker.sock 属组 gid=996，本进程附加组是 10001，不含它。
   在 .env 写 DOCKER_GID=996，再用 docker compose up -d --force-recreate 重建（restart 不生效）。
```

照做即可；手工核对用宿主机上的 `stat -c '%g' /var/run/docker.sock`——NAS（busybox）上常常没有 `docker` 组条目，
`getent group docker` 会返回空，只能靠猜（线上就有人先猜 998、再猜 0，两次都无效）。两个坑：

* 改完 `.env` **必须** `--force-recreate`：`docker compose restart` 不会重新套用 `group_add`。
* 查出来是 `0`（socket 属 `root:root`，群晖等 NAS 上常见）时加组救不了：要么让容器用 root 跑（compose 里加 `user: "0:0"`），要么上 `docker-socket-proxy`（更安全，见「安全红线」）。

**情况二：项目扫到了，但目录在容器里不存在。** 这是权限修好之后紧接着会撞上的第二个坑：

```
ℹ️ 有 16 个 compose 项目扫到了，但它们的目录在容器里不存在：/mnt/data2/docker/clinepass-tg-bot、/mnt/data2/docker/cpa 等 16 个
   修：compose 命令按宿主机的原路径执行，所以要按相同路径挂进来——在 compose 的 volumes 里加 -v /mnt/data2/docker:/mnt/data2/docker，再重建容器。
```

原因：`docker compose ls` 给的是**宿主机路径**，而 mtbots 要用 `docker compose -f <那个路径>` 去执行，所以容器里必须存在同一个路径。
按提示把公共父目录挂进来即可（面板会自动算出这条 `-v`）：

```yaml
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /mnt/data2/docker:/mnt/data2/docker      # ← 你的 compose 项目根目录，路径左右必须一样
      - ./data:/app/data
```

改完 `docker compose up -d`（加 `--force-recreate` 更保险）。项目散在不同根下时面板不给公共 `-v`，逐个挂即可。

## 已知限制

* 群里「回复某条面板消息定位上下文」仍未实现；面板按会话唯一（跨模块共用），命令触发时新发到最底部、旧面板删除。
* Docker 模块是**进程内**模块（不是 sidecar + `docker-socket-proxy`）。单人自用可接受；多人场景建议按设计稿 §4 方案 B 拆出去。
* Docker 模块只能看到「挂进容器的那些 compose 目录」，且容器内路径必须与宿主机一致（探针靠 `docker compose ls` 的宿主机路径定位工作目录）。
* LitePan 命令菜单按会话差异化下发受 `MenuManager` 限制：目前是所有已授权会话共用一份片段（含 `refresh_<slug>`）。
* LitePan 的「自动发现」与「回执」还没拆成两个开关（旧版就是耦合的，行为未退化）。
