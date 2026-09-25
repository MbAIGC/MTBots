# 三个 TG Bot 合并可行性 + 交互设计

> 对象：**LDMG**（`/root/workspace/LDMG`）、**LitePan-TGBot**（`/root/LitePan-TGBot`）、**ClinePass-TG-Bot**（`/root/ClinePass-TG-Bot`）
> 结论：**能合并。技术上没有硬阻塞，真正的取舍在"权限边界"而不在代码量。**
> 本文档是设计稿（未改动任何现有代码）。

---

## 1. 结论速览

| 问题 | 答案 |
|---|---|
| 能不能合成一个 bot？ | 能。三者都是 Python + Telegram，命令只有 **4 个**冲突，其余天然不冲突 |
| 最大的技术工作量 | LitePan 是**同步 stdlib 长轮询**，另两个是 **python-telegram-bot 异步**，必须统一运行时 |
| 最大的设计风险 | LDMG 挂 `docker.sock`（≈宿主机 root）× ClinePass 存多用户 API Key，**爆炸半径不该重叠** |
| 推荐落地方式 | 一个 Bot 进程做前端 + LDMG 特权操作放 sidecar/`docker-socket-proxy`；或先合 LitePan + ClinePass |
| 合并的真正收益 | 统一入口、**跨模块任务中心**、统一权限/脱敏，而不是"少一个会话窗口" |

---

## 2. 现状对比

| 维度 | 🐳 LDMG | 🎬 LitePan-TGBot | 🤖 ClinePass-TG-Bot |
|---|---|---|---|
| 定位 | 宿主机 Docker Compose 升级/清理 | 远程触发 LitePan 媒体自动化 | 多 Key 额度面板 |
| 代码 | `bot.py` 1464 行，单文件 | `tgbot.py` 1290 行，单文件 | `bot.py` 639 + `core.py` 1250 |
| 框架 | python-telegram-bot 21.6（async） | **纯 stdlib `urllib` + threading，自己 `getUpdates`** | python-telegram-bot（async） |
| 依赖 | ptb + python-dotenv | 仅 `pypinyin` | ptb + requests |
| 鉴权 | `ALLOWED_USER_IDS` 必填，**默认拒绝** | `TG_ALLOWED_IDS` / `users.json` 的 `chat_ids` | `ALLOWED_USER_IDS` **可留空 = 所有人可用** |
| 存储 | `logs/` 滚动审计日志 | `users.json`（含 LitePan 管理员密码） | `config.json`（含用户 API Key，600 权限） |
| 特权 | **挂 `docker.sock` + 宿主机 compose 目录** | 网络可达 LitePan 后台 | 出网到 `api.cline.bot` |
| 命令 | start, list, status, prune, upgrade, help | start, help, info, list, status, ping, menu, refresh, strm, run, `refresh_<slug>`（**运行时动态生成，上限 100 条**） | start, help, id, status/quota, addkey, delkey, keys, clear |
| 多用户 | 白名单内所有人共享宿主 Docker 权限 | 按 `chat_id` 绑定各自的 LitePan 实例（隔离好） | 按 user_id 隔离 Key（隔离好） |

**关键观察：权限模型是三种不同的东西。**
LDMG 是"要么全给、要么不给"的运维权限；LitePan 是"按用户绑定实例"；ClinePass 是"每个用户管自己的密钥"。
合并后必须有真正的角色系统，不能沿用任何一方的扁平白名单。

---

## 3. 冲突清单（这是"能不能合"的核心论据）

三边命令取并集后，**只有 4 个真冲突**：

| 命令 | LDMG | LitePan | ClinePass | 处置 |
|---|---|---|---|---|
| `/start` | 面板 | 帮助 | 帮助 | 合并成**唯一首页** |
| `/help` | 帮助 | 帮助 | 帮助 | 合并帮助，**按权限只渲染可见章节** |
| `/status` | 容器状态 | 状态/规则 | 额度面板 | 收归首页**总览卡片** |
| `/list` | 项目面板 | `info` 的同义词 | — | 归 Docker；LitePan 侧保留 `/info` |

**天然不冲突、可原样保留的**：
`/upgrade`、`/prune`（Docker）、`/refresh`、`/info`、`/strm`、`/run`、`/ping`、`/menu`（LitePan）、
`/addkey`、`/delkey`、`/keys`、`/clear`、`/id`、`/quota`（ClinePass）。

> 这意味着合并的**迁移成本主要是"消除歧义"而不是"重命名一切"**，老用户肌肉记忆基本能保住。

---

## 4. 三种合并方案

### 方案 A：单进程单 Bot，模块化（真合并）
一个 `Application`，三个 feature 包：`features/docker/`、`features/litepan/`、`features/cline/`。

* ✅ 一个 token、一份部署、一个会话、可跨模块联动
* ❌ `docker.sock` 与多用户密钥同进程 = 权限耦合
* ❌ LitePan 同步代码要异步化
* 工作量：**3–6 人日**

### 方案 B：一个前端 Bot + 三个内部 worker（网关）
网关持有 token，负责路由/权限/菜单；三个 worker 通过 unix socket / docker 内网 HTTP 暴露 `handle(update) -> reply`。

* ✅ 保留特权隔离（Docker worker 自己挂 socket），故障隔离，可逐个迁移
* ❌ 多一层 IPC，多一个容器
* 工作量：**2–3 人日**

### 方案 C：只统一入口，不合并进程
保留三个 bot，新人 Bot 做 deep-link 跳转（`t.me/other_bot?start=...`）。

* ✅ 几乎零成本（0.5 人日）
* ❌ 不是"一个 bot"，跳会话体验割裂，跨模块状态拿不到
* 只适合"我就想少点置顶"

### 推荐
* **单人自己用** → 方案 A 全合并；LDMG 走 `docker-socket-proxy`（只放行 `pull/up/ps/images` 等必要端点）或特权 sidecar。
* **ClinePass 要给多用户用** → 先合 **LitePan + ClinePass**（两者都不碰宿主机，受众也接近），LDMG 作为特权模块以 sidecar 接入或暂时独立、从首页 deep-link 进入。

---

## 5. 交互设计

### 5.1 一句话原则
> **模块 = 命名空间；命名空间由"当前所处的位置"决定，而不是由命令前缀决定。**

### 5.2 首页（Home）—— 合并后新增的价值
`/start` 不再是某一个 bot 的帮助页，而是一张**聚合卡片**（按权限渲染，无权限的模块直接不显示）：

```
🏠 控制台 · v1.0
───────────────
🐳 Docker    2 个项目有新镜像
🎬 LitePan   最近任务 ✅ 3 分钟前
🤖 Cline     本周额度 57%
───────────────
[🐳 Docker 管理]  [🎬 LitePan]
[🤖 Cline 额度]   [🧰 任务中心]
───────────────────────────────
🔄 18:20:11
```

### 5.3 模块内：面包屑 + 原地编辑
点按钮后**编辑同一条消息**，不新发：

```
🏠 › 🐳 Docker 管理
───────────────
01  emby       ⬆️ 可升级
02  openlist   ✅ 最新
───────────────
[⬆️ 全部升级] [🧹 清理镜像]
[◀️ 上一页]  [下一页 ▶️]  [🏠 返回]
```

* 面包屑 `🏠 › 模块名` 永久可见，`🏠 返回` 永远在右下角。
* 面板消息**每个模块每条会话只保留一条**，用 `edit_message_text` 更新；超 4096 字或编辑失败才新发 + 自动分片。
* 群里用「回复某条面板消息」来定位上下文，避免多用户互相刷屏。

### 5.4 命令分层与消歧

**第一层：全局命令（唯一，无歧义）**
`/start`（首页）、`/help`、`/id`、`/menu`、`/cancel`、`/jobs`、`/status`（首页总览）

**第二层：模块命令（不冲突的原样保留）**
* Docker：`/upgrade`、`/prune`、`/list`
* LitePan：`/refresh`、`/info`、`/strm`、`/run`
* Cline：`/addkey`、`/delkey`、`/keys`、`/clear`、`/quota`

**第三层：消歧规则（4 个冲突命令）**
`/status` 和 `/list` 按**当前模块**解释，没有模块上下文时按全局解释：

| 输入 | 无活动模块 | Docker 中 | LitePan 中 | Cline 中 |
|---|---|---|---|---|
| `/status` | 首页总览 | 容器状态 | 规则/盘状态 | 额度面板 |
| `/list` | Docker 项目列表 | 项目列表 | 规则列表 | Docker 项目列表 |

需要显式指定的场景，提供带前缀的**永久别名**：`/d_status`、`/p_status`、`/c_status`、`/d_list`。

### 5.5 LitePan 动态规则命令的处置（重要）
现状：LitePan 把每条规则生成一个 `refresh_<slug>` 命令，上限 100 条，且会调 `setMyCommands`。
合并后 Telegram 的 100 条命令额度是**三个模块共享**的，必须重新分配：

1. **命令区**只保留常用规则（建议 ≤ 30 条），用「最近使用 / 标记置顶」筛选；
2. 其余规则进入**内联键盘分页**（复用 LDMG 已有的分页实现）：

```
🎬 LitePan · 规则（2/5）
[光鸭-A 全量]  [123-A 全量]
[剧集刮削]     [电影刮削]
[◀️] [2/5] [▶️] [🏠 返回]
```
3. 命令菜单按**用户作用域**下发（`BotCommandScopeChat`）：管理员看到 `upgrade/prune`，普通用户看不到——这是 Telegram 原生能力，正好用来隐藏无权限模块。

### 5.6 角色与 ACL（必须新建，不能沿用扁平白名单）

```
owner   全部 + 用户/角色管理
admin   docker 全部 + litepan 触发 + 只读查看所有 Cline 额度
user    仅自己的 Cline Key + 被授权的 LitePan 触发
```

```json
{
  "roles": { "123456789": "owner", "987654321": "user" },
  "acl": { "docker": ["owner", "admin"], "litepan": ["owner", "admin", "user"], "cline": ["owner", "admin", "user"] }
}
```

* **默认拒绝**，白名单语义三方统一（ClinePass 现在"留空 = 所有人可用"必须改掉）。
* 限流按模块独立：Docker 操作锁（已有）、`STATUS_COOLDOWN`（Cline 已有）、LitePan 回执轮询各自配额。
* 破坏性操作统一**两步确认**，按钮绑定发起人 + 过期时间（LDMG 已把"取消按钮与任务绑定"，把它提升为公共组件）。

### 5.7 统一任务中心 `/jobs`（合并才有的能力）

```
🧰 任务中心
───────────────
🐳 emby 升级中 ▓▓▓▓▓░░░ 62%   [取消]
🎬 LitePan 刮削中 ⏳ 1 分 20 秒  [取消]
✅ openlist 升级完成 · 2 分钟前
```
* 统一 `Job(id, feature, title, task, cancel_event)`，任意模块的长任务都注册进来；
* 完成推送带模块标签：`🐳 / 🎬 / 🤖`，一眼看出是哪条线的通知；
* LitePan 的"完成回执轮询"改成向任务中心 push，而不是直接发消息。

### 5.8 统一渲染规范
* 头部：`🏠 › 模块名`；底部：`🔄 HH:MM:SS`（LDMG 与 ClinePass 已有，统一格式即可）。
* 状态符号语义固定：`✅` 成功 / `❌` 失败 / `⚠️` 接近阈值 / `⛔️` 超阈值 / `⏳` 进行中。
* 进度条、时间人性化（`humanize_delta`）、消息分片（`split_message`）各抽一份公共实现。
* **脱敏统一**：ClinePass 的 `redact()` + `RedactingFilter` 覆盖全局，LitePan 管理员密码、Cline API Key、Docker 命令输出全部走同一层（消息层 + 日志层双重）。

### 5.9 跨模块联动（值得做的三个）
1. LitePan 面板检测到 LitePan 容器有新镜像 → 直接给 `[⬆️ 升级 LitePan 容器]` 按钮（跳 Docker 模块并预选该服务）。
2. Docker 升级完某个媒体服务 → 提示"是否重跑 LitePan 刷新/刮削规则"。
3. 首页总览一处看全：容器异常数 + 最近任务结果 + 额度告警。

---

## 6. 安全红线

1. **`docker.sock` 不进合并后的主进程**（除非确定只有你一个人用）。
   首选 `docker-socket-proxy` 只读+白名单端点；主进程只发 HTTP。
2. **多用户密钥隔离必须在合并后重新证明**：所有渲染函数强制带 `user_id` 并做归属断言，
   防止 `/status` 串台把别人的 Key 或管理员输出带出来。
3. **默认拒绝**：合并后任一模块的鉴权缺口都会变成"整机"的缺口，白名单必须统一为默认拒绝。
4. **根文件系统只读 + 非 root 运行**：沿用 LitePan / ClinePass 的做法；
   只有 LDMG sidecar 允许接触宿主 Docker，且单独设限。
5. `setMyCommands` 按作用域下发，避免普通用户从菜单发现运维命令。

---

## 7. 迁移路线（可回滚）

| 阶段 | 内容 | 产出 |
|---|---|---|
| P0 | 抽公共库 `tgbot_common`：config / redact / panel / pagination / jobs / acl / split | 三个仓库先各自引用，**进程仍分开**，测试全绿 |
| P1 | LitePan 改造成 PTB 模块（1290 行同步 `urllib` → async，或用 `run_in_executor` 包装） | 最大的一块工作量 |
| P2 | ClinePass 并入（框架已一致） | 半天～1 天 |
| P3 | 首页 / 面包屑路由 / 权限角色 / 命令作用域 / `/jobs` | 交互成型 |
| P4 | LDMG 接入（sidecar + socket-proxy），**换 token 上线** | 旧三 bot 下线 |

* 过渡期用**测试 token** 并行验证；三边 token 各自独立，正式切换只是一次 token/配置替换。
* 回滚 = 把旧镜像和旧 token 起回来，风险可控。
* 工作量：方案 A **3–6 人日**（含 LitePan 异步化 1–2 天、权限与存储统一 1 天、面板路由 1 天、联调回归 1–2 天）。

---

## 8. 不建议合并的部分

* **不建议**把 `docker.sock` 直接搬进主进程（多用户场景）。
* **不建议**为了"少一个 bot"而强行合并：如果诉求只是减会话，方案 C 半天就够，别动 4600 行代码。
* 三个仓库可以先合并成**一个 monorepo、一个镜像、一个 compose 服务 + 一个特权 sidecar**，但模块目录保持独立，方便按需单独下线。

---

## 9. 附录：LitePan 的「自动发现 + 自动生成菜单」怎么保留

**结论：能保留，而且应该保留 —— 这是 LitePan 模块最值钱的部分**（不用手工维护盘名/事件映射）。
但**不能原样搬**，因为它现在**独占 `setMyCommands`**：合并后 LitePan 每刷新一次菜单，
就会把 Docker、ClinePass 的命令**整条擦掉**。

### 9.1 现在它怎么工作（`tgbot.py`）

| 机制 | 位置 | 说明 |
|---|---|---|
| 自动发现 | `Discovery.fetch()` `tgbot.py:499` | 用管理员账号读 `/api/admin/accounts`、`/api/admin/automation/options`、规则列表 → 得到账号名、STRM/整理任务、Webhook 规则 |
| 生成命令 | `_build_rule_slugs()` `tgbot.py:589` | 每条规则 → `/refresh_<slug>`；slug 限长 24，重名自动加 `_2/_3` |
| 下发菜单 | `refresh_menu()` `tgbot.py:1011` | 拼 `start/refresh/info/menu/run` + 所有规则命令，调 `setMyCommands`（**无 scope = 全局**） |
| 刷新时机 | `tgbot.py:674`（启动）、`:678`（每 30 分钟）、`:777`（`/info`）、`:779`（`/menu` 强制） | 命令列表没变就不发请求 |
| 上限 | `MAX_MENU_COMMANDS = 100` `tgbot.py:177` | 超出直接截断 |
| 降级 | `:1046` `log.warning` 返回 False | 菜单失败**不影响触发**，命令本身照用 |
| 依赖 | `_discovery()` `tgbot.py:926` | **没有管理员账号（`receipt_enabled=False`）时直接返回 None** → 没配管理员密码 = 没有发现、没有动态菜单 |

### 9.2 合并后的三处必修（+ 一处可选）

**① 菜单片段化（最关键）**
LitePan 不再自己调 `setMyCommands`，改成向统一的 `MenuManager` 提交"片段"，由它合成后下发：

```python
class MenuProvider(Protocol):
    def entries(self, user_id: int) -> list[BotCommand]: ...   # 本模块贡献的命令
    def scope(self, user_id: int) -> BotCommandScope: ...      # 作用域

# MenuManager.merge(user_id) -> 全局命令 + 各模块片段 -> set_my_commands(scope=...)
```

模块只回答"我有哪些命令"，**不再碰 Telegram API**；仍保留"内容没变就不请求"的去重逻辑。

**② 100 条额度要分配**
现在是 LitePan 独占 100 条。合并后建议给动态规则命令**预算 30–40 条**：
常用的（最近使用 / 标记置顶）做成命令，其余进**内联键盘分页**（复用 LDMG 的分页实现），
`refresh_<slug>` 的精确执行逻辑（按规则 ID 而不是事件名，避免同名事件误触发）完全保留。

**③ 作用域按会话下发（顺带修掉一个现存问题）**
`profile=None` 时它把**所有用户**的规则合并进一个全局菜单，而 `/info` 时又用**单个用户**的规则覆盖全局菜单 ——
多人使用时菜单会互相覆盖、规则名互相可见。合并后改用 `BotCommandScopeChat(chat_id)` 按会话下发：
全局 scope 只放通用命令，LitePan 规则只出现在你自己的会话里。单人使用天然没这问题，但顺手修掉。

**④（可选）发现与回执解耦**
现在"自动发现"被绑在"回执模式"上（没配管理员密码就什么都没有）。
合并后可以拆成两件事：`discovery.enabled`（要规则菜单）与 `receipt.enabled`（要完成回执），各配各的。

### 9.3 原样保留清单（照搬，不要重写）

- 60 秒发现缓存 + `_discovery_fetch_lock` 单飞，避免打爆 LitePan；
- slug 限长 24 / 重名加后缀 / 中文名转拼音 `pypinyin`；
- `/menu` 强制刷新、30 分钟定时刷新、命令未变化则不调接口；
- 菜单刷新失败只 warning、不影响 `/refresh` 触发；
- `/refresh_<slug>` 走规则 ID 精确执行；
- `_discovery_failed` 与"未开启发现"的区分（用于提示"发现失败，可 /menu 重试"）。

### 9.4 异步化的唯一注意点

发现是**同步 HTTP**（登录 + 拉三个接口），合并进 `python-telegram-bot` 的 asyncio 事件循环后，
必须用 `asyncio.to_thread(...)` / `run_in_executor` 包装，否则一次 LitePan 卡顿会**冻住 Docker 和 Cline 两个模块的按钮**。
