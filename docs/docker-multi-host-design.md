# Docker 多主机（远端容器升级）设计稿 —— **SSH 执行版**

> 状态：**计划中，未实现**（本文只写方案，代码未动）。
> 传输选型：**SSH 执行**（`ssh <目标> docker compose …`）。
> 决策依据：**不挂任何远端目录**。这条前提直接排除了 `DOCKER_HOST` 路线（原因见 §2，留档避免反复讨论）。

## 1. 目标与非目标

**要做的**

1. 一个 bot 同时管理本机 + 若干**远端主机**的 Compose 项目：列表、详情、升级（项目 / 单服务 / 批量）、镜像清理、容器状态、`--health`。
2. 远端体验与本机一致：同样的进度流式回显、🛑 中断、收尾面板、权限控制。
3. 无 `data/docker-hosts.json` 时**行为与今天完全一致**（单机），旧部署零改动。

**不做的**

* 不挂载、不同步远端任何文件（这正是选 SSH 的原因）。
* 不做远端构建 / git 操作 / 日志查看 / `exec`。
* 不支持「同 token 起第二个实例」（Telegram 只允许一个 `getUpdates` 消费者）。

## 2. 为什么不挂目录就必须用 SSH（留档）

`DOCKER_HOST` 路线的三条硬伤，都源自「compose 的 yml 由**本地 CLI** 解析」：

1. `pull` / `up -d` 要求容器内能打开那份 yml → 必须挂远端目录；
2. yml 里的相对路径（`./data`）被本地 CLI 解析成**绝对路径**再发给远端 daemon。实测：

   ```yaml
   # 同一份 yml，仅所在目录不同
   - source: /root/…/.pathdemo/a/data     # ./data → a/data
   - source: /root/…/.pathdemo/b/data     # 放到 b/ 就变成 b/data
   ```

   远端上没这个路径时，**dockerd 会自动建一个空目录顶上**，容器静默挂到空目录，数据看着「没了」且不报错；
3. 于是容器里那份副本必须与远端**实时一致**，否则就是「拿旧定义升级」，还得再加一层副本漂移检测。

SSH 执行把这些全部消掉：yml 留在远端，由**远端的 CLI** 解析，路径天然一致；没有副本、没有过期、没有漂移检测，远端 docker CLI 版本也天然匹配。

## 3. 形态总览

```
   ┌────────────────── bot 容器（非 root, 只读根）──────────────────┐
   │  mtbots                                                       │
   │   └ 🐳 docker 模块 ── DockerState ── DockerHost ──┬─ local: docker compose …（走 /var/run/docker.sock，今天的方式）
   │                                                   └─ ssh  : ssh -i … mtbots@host docker compose …（yml 在远端）
   └───────────────────────────────────────────────────────────────┘
             │ unix socket                    │ ssh（22，走内网/WireGuard 均可）
             ▼                                ▼
      本机 dockerd                    远端 dockerd（远端用户在 docker 组）
```

每个远端的**一次性准备**：建一个专用用户（加入 `docker` 组）→ 放公钥 →（可选但推荐）给这条 key 加**强制命令守卫**，只允许它跑 `docker compose`。

## 4. 远端准备（一次性，写进 README）

```bash
# 远端
sudo useradd -m -s /bin/bash mtbots
sudo usermod -aG docker mtbots
sudo install -d -m 700 -o mtbots -g mtbots /home/mtbots/.ssh
# 把 bot 主机生成的公钥放进去（见下）
```

bot 主机侧生成密钥（一次性），密钥放 `data/`（已 gitignore，容器内 `/app/data/ssh`）：

```bash
ssh-keygen -t ed25519 -N '' -C mtbots@bot -f ./data/ssh/id_ed25519
chmod 600 ./data/ssh/id_ed25519
ssh-keyscan -p 22 10.0.0.5 >> ./data/ssh/known_hosts    # 可选：预置 known_hosts
```

### 4.1 强烈建议：给这把 key 加「只能跑 compose」的守卫

万一 bot 被拿下，这把 key 就是远端 shell。用 `authorized_keys` 的强制命令把它收窄：

```sh
#!/bin/sh
# 远端 /usr/local/bin/mtbots-compose-guard
# 只放行：docker compose ls … / docker compose -f <path> pull|up -d|config --services|ps / docker image prune
case "$SSH_ORIGINAL_COMMAND" in
  "docker compose ls"*|"docker compose -f "*" pull"*|"docker compose -f "*" up -d"*|\
  "docker compose -f "*" config --services"*|"docker compose -f "*" ps"*|"docker image prune -f"*)
      exec sh -c "$SSH_ORIGINAL_COMMAND" ;;
  *)  echo "mtbots: command not allowed" >&2; exit 126 ;;
esac
```

```text
# 远端 /home/mtbots/.ssh/authorized_keys
command="/usr/local/bin/mtbots-compose-guard",restrict ssh-ed25519 AAAA… mtbots@bot
```

加上守卫后，即使 bot 主机沦陷，攻击面也只是「升级这些 compose 项目」，拿不到远端 shell。

## 5. 配置：`data/docker-hosts.json`

```json
{
  "hosts": [
    { "id": "nas", "label": "本机 NAS", "kind": "local" },
    {
      "id": "vps", "label": "Oracle 东京", "kind": "ssh",
      "target": "mtbots@10.0.0.5", "port": 22,
      "identity": "/app/data/ssh/id_ed25519",
      "known_hosts": "/app/data/ssh/known_hosts",
      "strict": "accept-new",
      "roots": ["/opt"]
    }
  ]
}
```

| 字段 | 适用 | 说明 |
|---|---|---|
| `id` | 全部 | 唯一，`[a-z0-9_-]{1,16}`；**回调里只认这个 id**（绝不接受任意字符串拼命令） |
| `label` | 全部 | 面板显示名 |
| `kind` | 全部 | `local` / `ssh` |
| `target` | ssh | `user@host`；校验格式，拒绝空格/分号/`-o` 这类可注入内容 |
| `port` | ssh | 默认 22 |
| `identity` | ssh | 私钥路径（默认 `/app/data/ssh/id_ed25519`）；不可读 → 该主机「配置错误」而不是静默跳过 |
| `known_hosts` / `strict` | ssh | `strict` 取 `yes`（默认，配预置 known_hosts）或 `accept-new`（首次自动记录，需要该文件可写） |
| `roots` | ssh | 可选的路径白名单：只允许管理这些前缀下的项目（纵深防御） |
| `enabled` | 全部 | 默认 true，便于临时下线一台主机 |

容错（沿用现在「非法值回退 + 明确提示，不崩」的风格）：文件不存在 → 单机模式；`id` 重复 / `kind` 未知 / 缺私钥 / `target` 格式非法 → 该主机标记「配置错误」；**一台主机探测失败不影响其它主机**（per-host `last_scan_error`）。

## 6. 命令层设计

```python
@dataclass(frozen=True)
class DockerHost:
    id: str
    label: str
    kind: str                      # local | ssh
    target: str = ""
    port: int = 22
    identity: str = "/app/data/ssh/id_ed25519"
    known_hosts: str = "/app/data/ssh/known_hosts"
    strict: str = "yes"
    roots: tuple[str, ...] = ()

    def wrap(self, cmd: Sequence[str]) -> list[str]:
        """把远端的 docker 命令包成一条 ssh 调用（local 主机原样返回）。"""
```

* 包装规则：`["ssh", "-p", str(port), "-i", identity, "-o", "BatchMode=yes",
  "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
  "-o", "StrictHostKeyChecking=%s" % strict,
  "-o", "UserKnownHostsFile=%s" % known_hosts, target, "--", shlex.join(cmd)]`
* **引号**：远端命令必须是一个字符串，一律 `shlex.join(cmd)`（项目路径带空格也不会散）。
* `BatchMode=yes`：绝不弹交互式密码提示（拿不到 key 就直接失败，日志里给提示）。
* `cwd` 对 ssh 主机**不传**（远端路径自带在 `-f` 里）。
* 退出码 → 面板提示（不能只有「失败」）：

  | 码 | 含义 | 提示 |
  |---|---|---|
  | 255 | ssh 自己失败 | 「连不上 / 认证失败：检查网络、端口、密钥、known_hosts」 |
  | 127 | 远端没有该命令 | 「远端未安装 docker compose」 |
  | 126 | 被守卫脚本拒绝 | 「远端授权只允许 compose 操作」 |
  | 1 | docker/compose 报错 | 走现有逻辑：把命令尾部输出抄进面板 |

* 环境：ssh 主机不需要 `DOCKER_HOST` 等变量；仍然按调用传 `env=`（不污染 `os.environ`），保留给 local 主机与将来的用途。

## 7. 数据流

```
打开面板 → 逐主机 scan：local 跑 docker compose ls；ssh 跑 ssh <host> docker compose ls -a --format json
        → 项目身份 = (host_id, name)，标签显示 vps/blog；单主机时不显示主机头（与今天一致）
点项目 → 详情 → 升级确认（两步、绑定发起人、60s 过期）
        → run_command_with_feedback(cmd=[ssh, …, 'docker compose -f /opt/blog/docker-compose.yml pull'])
        → 进度照旧（读的是 ssh 的 stdout）；🛑 中断 = 终止本地 ssh（见 §9）
        → 收尾面板：✅ 🐳 升级项目 vps/blog · …（同一套 card_text）
镜像清理 → 按主机分别跑 docker image prune（影响的是那台主机）
--health → 逐主机：ssh 连通性 → 远端 docker version → compose ls 条数 → 私钥/known_hosts 可读性
```

并发取舍：保持「同一时刻只有一个 docker 长任务」的全局锁，跨主机**不并行**（面板只有一个进度面，并行会让进度与取消语义变乱）。

## 8. 代码改造清单

| 文件 | 改动 |
|---|---|
| `mtbots/features/docker/hosts.py`（新） | `DockerHost`（`wrap()` / `env()` / `display`）+ `load_hosts()` 校验；`shlex.join` 包装 |
| `mtbots/features/docker/config.py` | `DockerSettings` 增 `hosts_file`（默认 `data/docker-hosts.json`，`DOCKER_HOSTS_FILE` 可覆盖） |
| `mtbots/features/docker/compose.py` | `scan_projects_sync()` 逐主机扫描、project 增 `host`/`host_label`；`seen_keys` 与缓存键改 `(host_id, name)`；`last_scan_error`/`hidden_dirs` 改 per-host（`hidden_dirs` 只对 local 有意义）；`build_compose_cmd()` → 由 host 包装；`run_command_with_feedback()` 支持 `host=`（包装命令、跳过 `cwd`、退出码提示）；`get_project_services`/`dump_container_status`/`scan_prune_candidates`/`format_prune_snapshot` 全部带主机；`scan_hint()` 按主机分支（ssh 主机不显示 socket GID / 未挂载目录那套本机提示） |
| `mtbots/features/docker/handlers.py` | 面板按主机分组；项目标签 `vps/blog`；回调载荷加 `host`（`p_sel`/`up_s_ask`/`up_svc_ask`/`up_p_do`/`up_svc_do`/`prune_*`）；`/upgrade NN` 仍是跨主机连续编号；未知 host id → `⚠️ 未知主机`；`/d_status`、清理按主机 |
| `mtbots/features/docker/__init__.py` | `summary()` / `id_lines()` 显示主机数与每台项目数 |
| `Dockerfile` | `apt-get install openssh-client`（约 +10MB）；**不需要**额外挂载目录 |
| `docker-compose.yml` / `.env.example` | 无需新卷（密钥与 known_hosts 放已有的 `./data:/app/data` 里，即 `/app/data/ssh/*`）；`read_only: true` 下若选 `strict=accept-new`，known_hosts 必须落在 `/app/data` 这种可写卷内 —— 配置里给的就是这个路径 |
| 测试 | 见 §11 |
| 文档 | 本文 + README「多主机」章节（实现后写）+ 设计稿 §5.10 + 合并报告 §10 |

## 9. 取消语义（要说清楚）

`🛑 中断执行` 杀的是**本地 ssh 进程**：连接断开后，远端 sshd 通常会向该会话的进程组发 SIGHUP，`docker compose` 随之退出——**但这不保证**。

因为 `pull` / `up -d` 都是**幂等**且通常较短的操作，实际做法是：面板照旧显示「已取消」，若远端那条命令仍跑完了，重跑一次升级即可，不会造成不一致状态。文案里写「已发送中断信号」，不承诺「已终止远端」。

## 10. 安全清单

1. **专用用户 + `docker` 组**，不要 root、不要复用登录账号。
2. **强制命令守卫**（§4.1）：这把 key 只能跑 compose 相关命令——这是 SSH 路线相对 proxy 路线最大的安全优势，务必用上。
3. 私钥 `0600`，只放 `data/`（gitignore）；容器只读根、不额外挂载。
4. `BatchMode=yes`（不交互）、`ConnectTimeout`、`ServerAliveInterval`；生产建议 `strict=yes` + `ssh-keyscan` 预置的 `known_hosts`。
5. `target` 与 `identity` 只来自配置，**回调只接受配置内的 host id**；远端路径来自远端 `compose ls` 的 labels，不拼用户输入，可再用 `roots` 白名单夹一层。
6. 权限仍在 `core.acl`：`docker` 模块只给 owner/admin；`upgrade`/`prune` 走两步确认。
7. 审计：每条远端命令 INFO 记 `host=<id> cmd=<前 200 字符>`；失败原因进 job detail，可 `/jobs` 回溯。

## 11. 测试点（约 20 条）

* 配置：无文件 = 单机；重复 `id`；未知 `kind`；`target` 含空格/分号被拒；私钥不可读 → 「配置错误」（不是静默跳过）。
* 命令包装：`shlex.join` 对带空格路径的引用正确；`-p/-i/-o` 参数齐全；路径含 `'` 时不破引号；local 主机**不包装**。
* 退出码 → 提示映射：255/127/126/1 四类文案。
* 环境：`env=` 只作用于该子进程，不污染 `os.environ`。
* 扫描：两主机合并；A 主机失败不影响 B；per-host `last_scan_error`；同名项目不互相覆盖。
* 面板：单主机不出现主机头（与 v1.0.7 文案逐字节一致，回归保护）；多主机分组；`/upgrade 03` 指向跨主机扁平编号的正确项目。
* 回调：载荷带 `host`；伪造 `host=evil` → `⚠️ 未知主机` 且**不执行任何命令**。
* 取消：中断时终止的是本地 ssh 进程（断言 `stop_process_tree` 被调用）。
* 其它：prune/status/`--health` 按主机；`summary()` 主机数正确。
* 回归：现有 347 条用例在「无 hosts 文件」下全绿。

## 12. 分步落地（每步独立可验证、可回滚）

1. **只读接入**：`hosts.py` + 配置 + ssh 包装 + 扫描 + 面板分组 + `--health`。验收：面板列出远端项目、连不上/缺密钥的提示准确；远端项目先只读（升级按钮对远端回「暂不支持」）。回滚 = 删 `docker-hosts.json`。
2. **升级/清理**：确认与执行、退出码提示、按主机 prune/status。验收：远端 `pull` + `up -d` 全流程，含中断与失败提示。
3. **硬化**：README 的远端准备步骤（用户、公钥、**守卫脚本**、known_hosts）+ `--health` 补充检查项。
4. **文档与示例**：README「多主机」章节 + `docs/examples/`（守卫脚本、`authorized_keys` 片段）+ 合并报告 §10。

## 13. 风险

| 风险 | 缓解 |
|---|---|
| 密钥泄露 = 远端 shell | §4.1 强制命令守卫 + `docker` 组权限收敛 + 私钥只放 `data/` |
| 中断不保证终止远端命令 | 幂等 + 文案不承诺 + 重跑即可（§9） |
| 远端没装 compose / 版本太老 | 扫描阶段即失败，退出码 127 明确提示 |
| 网络抖动 | 现有按退出码判定 + 收尾面板给尾部输出；重跑幂等 |
| 一台主机拖慢面板 | per-host 缓存与超时（沿用 `SCAN_TIMEOUT`），失败只标记该主机 |
| 面板一次显示太多项目 | 沿用现有分页；多主机先按主机分组再分页 |

## 14. 与 `DOCKER_HOST` 版本相比省掉了什么

| 项 | DOCKER_HOST 版 | SSH 版 |
|---|---|---|
| 远端目录挂载 | 必需（同路径只读） | **零** |
| 副本过期 / 漂移检测（`config --hash`） | 必需 | **不需要** |
| 远端要装的东西 | socket-proxy 容器（或 TLS 配置） | 只要 sshd + `docker` 组用户 |
| 镜像体积 | 不变 | +`openssh-client`（约 10MB） |
| 新增的运维面 | proxy 放行清单、网络隔离、证书 | 密钥 + known_hosts + 守卫脚本 |
| 远端 CLI 版本 | bot 镜像里的版本（可能与远端不匹配） | 远端的版本（天然匹配） |
| 攻击面收窄手段 | proxy endpoint 白名单 | **`authorized_keys` 强制命令**（更硬） |

## 15. 实现前待确认

1. 远端主机的地址与 ssh 用户（给个真实例子即可，我写进示例配置）？
2. 远端是否已装 `docker compose`（`docker compose version`）？
3. 守卫脚本要不要一起上（推荐上，见 §4.1）？
