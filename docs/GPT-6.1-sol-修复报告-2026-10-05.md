# GPT-6.1-sol 审阅问题修复报告

- 修复日期：2026-10-05
- 修复基线：`6886ae2`（与审阅基线同一提交，审阅后未产生新提交）
- 对应审阅报告：[GPT-6.1-sol-审阅报告-2026-10-05.md](GPT-6.1-sol-审阅报告-2026-10-05.md)
- 本次范围：**R01–R12 + R14**。R13（拉取失败消息清理）按约定不修——它要改的是产品行为，且已被 `tests/test_docker_module.py` 固化为契约（详见 §4）。
- 验证：`make test` → **479 tests OK**（基线 469 条 + 新增 10 条回归测试，另同步更新 1 条镜像实现的旧断言）。未执行真实 Docker 操作、未联网、未接真实 Telegram。

## 1. 处置总览

| 编号 | 原定级 | 本次处置 | 主要改动文件 | 回归测试 |
|---|---|---|---|---|
| R01 | P1 | 已修 | `jobs.py`、`router.py`、docker/cline/litepan handlers | 4 条 |
| R02 | P1 | 已修 | `logging_setup.py` | 1 条 + 更新 1 条 |
| R03 | P1 | 已修（按加固对待） | docker/compose.py、docker/handlers.py | 复用既有输出测试 |
| R04 | P1 | 已修（改为 fail-fast） | `acl.py` | 既有配置测试 |
| R05 | P1 | 已修 | litepan/discovery.py | 1 条 |
| R06 | P2 | 已修 | litepan/handlers.py | 未补（见 §4） |
| R07 | P2 | 已修 | cline/handlers.py | 未补（见 §4） |
| R08 | P2 | 已修 | cline/handlers.py | 未补（见 §4） |
| R09 | P2 | 已修 | `router.py` | 1 条 |
| R10 | P2 | 已修 | docker/handlers.py | 未补（见 §4） |
| R11 | P2 | 已修（含报告中不成立的子结论） | litepan/config.py | 2 条 |
| R12 | P2 | 已修 | `store.py` | 1 条 |
| R13 | P2 | **未修** | — | — |
| R14 | 较低 | 已修 | scripts/setup-remote-host.sh | shell roundtrip 实测 |

合计：16 个文件，+472 / −87 行（含复核后的补充修复）。

## 2. 逐条修复

### R01 任务中心越权（任务归属 + 模块权限）

**改法**

- `jobs.py`：`Job` 新增 `user_id`（第 75 行），`JobCenter.add()` 接受 `user_id`（第 126 行）；`JobCenter.render()` 新增只读参数 `visible`（第 209 行），正文按谓词过滤——`running` 与 `recent` 都过滤，且**先过滤再取最近 5 条**，否则「最近 5 条不是我的」会让本人任务凭空消失。
- `router.py`：新增 `_job_visible()`（第 395 行）——先看 `core.acl.can(user_id, job.module)`，再看归属；owner/admin 看全部，普通用户只看自己发起的（无归属任务只对管理员可见）。`jobs_panel` 的按钮生成与面板正文共用同一谓词。
- `router.py` 取消分支：不再只比 `chat_id`，改为「模块权限 + 发起人」两步判定，管理员可跨会话取消。
- 7 处 `jobs.add` 调用点补齐归属：docker 的升级项目 / 升级服务 / 停止服务 / 批量升级 / 清理镜像（`_user_id(update)`）、cline 额度查询、litepan 规则执行。

**一个原报告没提的脆弱点**：旧判定写成 `if job.chat_id is not None and …`，而 `chat_id` 默认就是 `None`——一旦有调用不带 `chat_id`，取消校验完全失效。新判定不依赖 `chat_id`。

**验证**：`tests/test_core.py` 新增 `test_render_respects_visibility`、`test_jobs_cancel_denies_other_users`、`test_jobs_cancel_denies_module_without_permission`、`test_jobs_panel_hides_tasks_without_module_permission`。

### R02 日志脱敏在格式化前执行

**改法**（`logging_setup.py`）

- `RedactingFilter`：先 `record.getMessage()` 拿到 %-格式化后的完整文本，整串 `redact()`，写回 `record.msg` 并清空 `record.args`（第 84–85 行）。数字参数、异常对象参数、嵌套参数都不再是旁路。
- `TokenMaskFilter` 同口径处理（第 111 行），不再单独依赖字符串参数的 `replace`。
- 异常栈的 `exc_text` 脱敏逻辑保持不变；两个过滤器在多个 handler 上重复执行是幂等的（`redact()` 的输出不会再匹配模式）。

**验证**：新增 `test_redacting_filter_covers_non_string_args`（整数 ID + 异常对象参数）；`test_token_mask_filter` 的断言从「`record.args[0]` 含占位符」改为「最终文本含占位符且不含原 Token」——原断言正是报告 §3.4 批评的「只测实现细节、不测出口」。

### R03 Docker 输出未脱敏

**改法**

- `docker/compose.py`：进度预览 `preview`（第 1176 行）、最终输出 `safe_full_output`（第 1208 行）、失败提示 `explain_exit(...)` 的结果（第 1230 行）统一先 `redact()` 再 `esc()`。
- `docker/handlers.py`：`_failure_block()`（第 327 行）与批量升级的失败尾部输出（第 1297 行）同样先脱敏。

**未脱敏的出口**：`out=` 列表本身保留原始内容（供内部判断使用），只在进入聊天/任务详情的路径上脱敏。

**定级说明**：报告中该条为 P1，但复核未找到把凭据带进 Docker 输出的稳定通道（`compose pull/up/stop` + `ps`/`image ls` 的 `--format` 都不回显凭据）。本次按「顺手加固」处理，不按已证实的信息泄露对待。

### R04 非法角色回退 owner

**改法**（`acl.py`）：非法 `default_role` 与非法用户角色一律 `raise ValueError`（带原始 key 与可选值清单），不再 `warning + 回退 owner`；无法解析的用户 ID 同样报错。角色值统一 `strip().lower()` 后再校验。

**验证**：`make test` 全绿——既有的 `ConfigTests` / `ACLTests` 没有依赖「非法回退」的旧行为，说明这是一处纯粹的安全收紧。

### R05 LitePan 按盘刷新忽略 parse_ok

**改法**（`litepan/discovery.py`）：新增 `safe_by_account: dict[int, set[int]]`（第 89 行），在解析成功处与 `by_account` 一起写入（第 172 行）；`account_rules()` 改为只读这份索引（第 216 行），不再回头重扫 `self.rules` 的 `accounts` 字段。

**验证**：新增 `test_account_rules_skip_partially_parsed_rules`——复现报告场景（一条规则同时含有效 STRM 与不存在的 organize task），断言 `account_rules("GY01") == [103]`（只剩 `parse_ok` 的那条），且 `account_events()` 列表同步收窄。

### R06 LitePan 回执去重缺实例与接收会话

**改法**（`litepan/handlers.py`）：去重键从 `(rule_id, run_id)` 扩为 `(profile.lite_url, chat_id, rule_id, run_id)`（第 615 行）；`receipted_runs` 由 `set` 改为 `dict`（有插入序），新增容量上限 `RECEIPTED_MAX = 500`（第 61 行）并在超限时淘汰最旧记录（第 616 行），避免长跑进程无界增长。

### R07 Cline 多词别名删除只取第一个参数

**改法**（`cline/handlers.py`）：删除别名改为 `sanitize_alias(" ".join(...))`，与 `/addkey` 侧 `split_alias_and_key` 的规范化对齐；返回 `None`（别名不合法）时给出提示并中止，不再把 `None` 传进存储层。

### R08 Cline 换 Key 后旧查询回写旧额度

**改法**（`cline/handlers.py`）：`ClineState` 新增 `key_versions` 与 `snapshot_version()` / `invalidate_snapshot()`（第 80、84 行）。所有改 Key 的路径（addkey / delkey / clear）统一走 `invalidate_snapshot()`——它同时「作废快照」和「版本 +1」。查询侧（第 241、262 行）与首页刷新侧（第 723、726 行）在发起时记下版本，回写前比对；不等则丢弃结果（面板查询会额外发一条「Key 已变更，本次结果作废」）。

### R09 首页渲染失败后刷新槽位不释放

**改法**（`router.py` 第 298–307 行）：`render` 包进 try/except，失败时 `_release_refreshes()` 再 `raise`。这是 claim 之后、后台任务启动之前唯一的漏网路径。

**验证**：新增 `test_render_failure_releases_refresh_slot`——让 `panels.render` 抛异常，断言 `home_panel` 抛出且 busy 已复位。

### R10 多主机全量升级预检错误

**改法**（`docker/handlers.py`）：预检改为**逐台**探测并记入 `compose_ok`（第 1200–1208 行，单台探测异常只标记该台不可用）；全部目标主机都缺 compose 时才整批判失败（第 1212 行）；循环内对缺 compose 的主机记失败并 `continue`（第 1237 行），不再让 `build_compose_cmd` 抛 `RuntimeError` 被外层 except 收成「执行异常」而丢掉成功/失败清单。

### R11 LitePan 配置形状异常中断加载

**改法**（`litepan/config.py`）：

- `from_dict()` 校验条目本身是 Mapping（第 208 行）、`drives` 是 Mapping（第 230 行），否则抛 `ConfigError`。
- `load()` 的逐条捕获扩为 `ConfigError` + `(AttributeError, TypeError)` 兜底（第 351 行），保证「逐条跳过」承诺；错误信息带异常类型。

**对审阅报告的修正**：报告中「布尔型 `chat_ids` 会产生 AttributeError」这一子结论**不成立**——`int(str(True))` 会抛 `ValueError` 并被既有的 `ConfigError` 路径捕获。真正会逃逸的是 `null` / 字符串 / 数组条目，以及非 Mapping 的 `drives`。

**验证**：新增 `test_non_mapping_entry_and_drives_are_config_errors`、`test_bad_entries_are_skipped_and_rest_still_loads`（`users.json` 里第一个条目是 `null`、第二个 `drives` 非法，断言后续合法条目仍加载、`cfg.enabled` 为真）。

### R12 JsonStore 写盘失败不回滚内存

**改法**（`store.py` 第 122–141 行）：`mutate()` 改为在 `copy.deepcopy` 的副本上跑 mutator → 直接落盘 → 成功后才提交 `self._data`。写盘失败（`OSError` → `StoreError`）或 mutator 中途抛异常时，内存保持原值，也不会出现「用户被告知失败、内存却已生效」。

### R14 远端接入脚本目标路径未安全传递

**改法**（`scripts/setup-remote-host.sh`）：新增 POSIX 兼容的 `shell_quote_arg()`（第 267 行，单引号包裹 + 内部 `'` 转义为 `'\''`；本脚本是 `#!/bin/sh`，不能用 bash 的 `printf %q`），三处拼接远端命令的位置改用它：`SETUP_ARGS`（第 473 行）、守卫安装（第 570 行）、`authorized_keys` 兜底路径（第 573 行）。

**验证**：`sh -n` 语法通过；对 `plain` / 含空格 / 含单引号 / `; rm -rf /` / `$HOME` / `"` / 反斜杠 / 空串共 8 个样例做「远端 shell 再解析一次」roundtrip，全部原样还原。

### 2.1 复核后的补充修复

改动完成后由子代理对 diff 做了只读复核，据此补了以下几处（都属于已修条目的同类残留，不是新范围）：

- **R14 漏了第四处拼接**：`scripts/setup-remote-host.sh` 里 `apply-ak.sh` 的 `$REMOTE_HOME` 仍是手写单引号（与已改的三处同类），已改用 `shell_quote_arg()`。现全脚本 5 处调用该函数，`sh -n` 通过。
- **R03 补齐剩余输出出口**：`format_prune_snapshot()`（`compose.py:163`）内部脱敏（覆盖镜像扫描异常快照）、镜像清理的 `reclaimed` 行、容器状态速览的 `explain_exit` 结果与 `docker ps -a` 输出、`run_command_with_feedback` 的异常分支。
- **R02 堵住非 `str` 消息旁路**：条件由 `isinstance(record.msg, str)` 改为 `record.msg is not None`，非字符串 `msg` 也走 `getMessage()`。
- **R01 补首页摘要的归属过滤**：`litepan/handlers.py` 的 `summary()` 原来直接用 `core.jobs.running("litepan")` 计数，任何有 litepan 权限的人都能看到「执行中 N 条 / 最近任务」；现按「管理员全量 / 普通用户只看自己」过滤（与 `/jobs` 同口径）。
- **R04 补配置入口的 fail-fast**：`config.py` 的 `_env_roles()` 原来静默丢弃「无冒号 / uid 非数字」的条目——写错的人根本进不到 ACL 那层，仍会落到 `MTBOTS_DEFAULT_ROLE`（默认 owner）。现直接报错。
- **R10 修正跳过文案**：配置错误 / ssh 探测失败 / 真没装 compose 三种原因原来统一写成「没有 docker compose 命令」，改为「该主机不可用（未装 compose 或探测失败）」。
- **清理死分支**：`cline/core.py` 的 `_replace` 里 `if current is data: return` 在 `mutate` 改用副本后恒为假，已删除。

## 3. 新增/更新的测试

`tests/test_core.py`（+7）：`test_redacting_filter_covers_non_string_args`、`test_mutate_save_failure_keeps_memory`、`test_render_respects_visibility`、`test_jobs_cancel_denies_other_users`、`test_jobs_cancel_denies_module_without_permission`、`test_jobs_panel_hides_tasks_without_module_permission`、`test_render_failure_releases_refresh_slot`（共 7 条，其中 `test_jobs_cancel_callback` 保留原样）。

`tests/test_litepan_module.py`（+3）：`test_non_mapping_entry_and_drives_are_config_errors`、`test_bad_entries_are_skipped_and_rest_still_loads`、`test_account_rules_skip_partially_parsed_rules`。

`tests/test_core.py` 更新 1 条：`test_token_mask_filter`（断言从 `record.args[0]` 改为最终文本）。

## 4. 未修与未覆盖

- **R13（Docker 失败消息清理）未修**：`compose.py` 明确把「失败一律保留」写成了设计意图，`tests/test_docker_module.py` 用断言把它固定为契约。要修就得先定「新任务前删旧失败消息」这个产品行为，本次不在约定范围内。
- **R06/R07/R08/R10 未补 handler 级自动化测试**：这四条需要假客户端 / 假 DockerState / 事件循环编排的重骨架，本次只做了代码修复与 `make test` 全量回归，没有新增针对触发条件的用例。R07 的基础路径已有 `test_delkey_and_clear` 覆盖，但「短别名与多词别名并存」这条触发条件没有自动化断言。
- **审阅报告第 3 节（操作关联字段、日志文件权限 0700/0600、结果持久化、脱敏测试覆盖出口）与第 4 节（优化项）不在本次范围**内，未改。
- 未做的验证：未在真实 Telegram 上点过按钮，未跑真实 Docker 命令，未验证 LitePan/Cline 真实接口，未验多实例 LitePan 的回执去重（R06）在真实运行 ID 分配下的表现。

### 已知遗留（复核发现，本次有意未改）

- `docker/compose.py` 的 `remote_compose` 负缓存不会被 `invalidate_cache()` 清空：一次 ssh 抖动会让该主机在本进程后续批处理里持续被判为「不可用」，直到重启。属既有缓存策略，改动面超出本次范围。
- `scripts/setup-remote-host.sh` 的 `warn` 提示里仍有 `'$HOSTS_FILE'`（那行是打印给操作者照抄的 sudo 示例文本，不是远端命令参数，不涉及转义）。
- Cline `ConfigStore` 仍有路径直接修改 `store.load()` 返回的内存对象再 `save()`，所以 R12 的「写盘失败内存不变」在 `config.json` 上并不成立；不过 `keys()` / `summary()` 每次都重新读盘，用户看不到脏数据。

## 5. 风险提示

- **R04 是行为收紧**：配置里出现非法角色（或角色 ID 写成字符串用户名）现在会导致进程起不来。这是刻意的 fail-fast，升级前请先跑 `make check` 确认 `roles` / `MTBOTS_DEFAULT_ROLE` 合法。
- **R01 改变了可见性语义**：无 `docker` 权限的用户在 `/jobs` 里连 docker 任务标题都看不到；非 owner/admin 只能看到自己发起的任务。如果部署里依赖「大家互相看得到在跑什么」，需要显式给管理员角色。
- **R08 的过期分支会多发一条提示消息**（面板查询路径），这是为了让用户知道结果作废而不是静默无输出。
