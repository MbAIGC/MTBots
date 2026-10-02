# Docker 多主机（远端容器升级）设计稿

> 状态：**计划中，未实现**（本文只写方案；代码一行未动）。
> 目标版本：1.1.0（多主机是加能力，不是修 bug）。
> 传输选型已定：**`DOCKER_HOST` + `docker-socket-proxy`（或 dockerd 原生 TLS 2376）**；主机清单放 `data/docker-hosts.json`。

## 1. 目标与非目标

**要做的**

1. 一个 bot 同时管理本机 + 若干**远端主机**上的 Compose 项目：列表、详情、升级（项目/单服务/批量）、镜像清理、容器状态、`--health`。
2. 远端能力与今天本机体验一致：同样的进度流式回显、同样的 🛑 中断、同样的收尾面板与权限控制。
3. 无 `data/docker-hosts.json` 时**行为与今天逐字节一致**（单机），旧部署零改动。

**不做的**

* 不做远端**部署/构建**（`build:` 服务仍由各主机自己负责；不代管 git、不做 CI）。
* 不做远端主机上的文件编辑、日志查看、`exec`。
* 不跨主机迁移容器/卷。
* 不为「同一 token 跑第二个实例」提供任何支持（Telegram 只允许一个 `getUpdates` 消费者，两个实例会互相抢更新、菜单互相覆盖）。

## 2. 设计的中心约束：compose 文件是**本地解析**的

Compose CLI 的工作方式是「**本地读 yml → 把 API 请求发给选定 endpoint**」：

| 动作 | 谁执行 | 是否需要容器内能读到 yml |
|---|---|---|
| `docker compose ls -a --format json` | endpoint（远端 daemon） | ❌ 不需要，项目列表来自 daemon 的容器 labels |
| `docker compose -f <file> pull` / `up -d` / `config` | CLI 本地解析 + daemon 执行 | ✅ **必须**：CLI 要打开 `<file>` |
| yml 里的 `volumes:` bind 路径、`.env` | 「bind 路径」由**远端 daemon** 解析；`.env`/`include`/相对路径由**本地 CLI** 解析 | 部分 |

所以走 `DOCKER_HOST` 这条路，**必须把每台远端的 compose 目录在容器里可读**（本方案默认要求「容器内路径 = 远端路径」的只读副本）。这也是它相对 SSH 执行路线唯一的额外成本；选它的理由见 §4。

**副作用（要在文档里讲清）**：`pull` 是**远端 daemon** 拉的镜像——registry 镜像源/加速器、代理、磁盘空间都按**远端**的配置算，与 bot 主机无关。

## 3. 形态总览

```
                 ┌──────────────────────────── bot 容器（非 root, 只读根）───────────────────────────┐
                 │  mtbots（单进程，一个 Application）                                                │
                 │   ├ 🐳 docker 模块 ── DockerState ── DockerHost 抽象 ──┬─ local  : unix:///var/run/docker.sock
                 │   │        scan / pull / up -d / prune / ps           │                                 │
                 │   └ …其他模块                                          └─ remote : tcp://vps:2375 (proxy) / https://nas:2376 (TLS)
                 │  /remote/vps/opt:/remote/vps/opt:ro   ← 远端 compose 目录的只读副本（同路径）           │
                 └──────────────────────────────────────────────────────────────────────────────────────┘
                          │ unix socket（今天就有）                    │ 内网 / WireGuard / TLS
                          ▼                                            ▼
                  本机 dockerd                                 远端主机 dockerd（或 socket-proxy）
```

## 4. 传输选型

| 方案 | 结论 | 理由 |
|---|---|---|
| **`DOCKER_HOST` + socket-proxy / TLS（选定）** | ✅ 采用 | 容器里已有 docker CLI，不需要新装 ssh 客户端；`docker compose ls` 天然支持远端；每台主机只是多一个 endpoint + 一份只读目录副本 |
| SSH 执行（`ssh host docker compose …`） | ❌ 不选（但保留为备选） | 语义最干净（路径就是远端路径，不用副本），代价是镜像加 `openssh-client`、管密钥/known_hosts、远端要能收 ssh；将来若要「副本都不挂」，这是唯一出路 |
| 远端装 agent（watchtower 等） | ❌ | 脱离权限/面板/审计，多一套东西要维护 |
| 同 token 起第二个实例 | ❌ 禁止 | 抢 update、菜单互相覆盖 |

> 备注：socket-proxy 官方明确「**不含 TLS**，就是明文 HTTP 转发到宿主 socket，靠网络隔离」；要 TLS 就用 dockerd 原生 2376（`--tlsverify` + 客户端证书）。两条都支持，配置里用 `kind` 区分。

## 5. 远端接入：两种 endpoint

### 5.1 A 型：socket-proxy（明文 → 必须网络隔离）

远端主机上：

```yaml
# 远端 /opt/mtbots-proxy/docker-compose.yml
services:
  dockerproxy:
    image: tecnativa/docker-socket-proxy:latest
    privileged: true                     # 官方要求（SELinux/AppArmor 下访问 socket）
    environment:
      # —— 写操作：升级必需 ——
      POST: "1"                          # 默认 0：关掉就只剩只读
      CONTAINERS: "1"                    # ls / inspect / create / rm（compose up -d 需要）
      NETWORKS: "1"                      # compose 网络
      VOLUMES: "1"                       # 项目声明了命名卷时需要（能创建/删除卷 = 有数据风险，见 §11）
      IMAGES: "1"                        # pull / inspect / prune
      ALLOW_START: "1"
      ALLOW_STOP: "1"
      ALLOW_RESTARTS: "1"
      # —— 明确关掉 ——
      EXEC: "0"
      BUILD: "0"
      SWARM: "0"
      SYSTEM: "0"
      SECRETS: "0"
      CONFIGS: "0"
      AUTH: "0"
      NODES: "0"
      PLUGINS: "0"
      SERVICES: "0"
      TASKS: "0"
      SESSION: "0"
      COMMIT: "0"
      LOG_LEVEL: notice
    ports:
      - "10.0.0.5:2375:2375"             # 只绑内网 IP，绝不要 0.0.0.0、绝不要公网
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
```

放行清单的取舍：`compose up -d` 会创建/重建/启动/停止容器，并按需建网络、建卷，因此 `CONTAINERS/NETWORKS/VOLUMES/POST/ALLOW_START/STOP/RESTARTS` 是必需的；`PULL` 需要 `IMAGES`。若 pull 时报 `403 Forbidden`，按此顺序补开再试：`DISTRIBUTION` → `INFO`（这两个只暴露元数据，风险低）。

**网络要求（红线）**：proxy 端口只允许 **bot 主机** 访问。可行做法：WireGuard/Tailscale 网段内互访、或内网 VLAN + 防火墙白名单。**绝不允许**把 2375 暴露到公网。

### 5.2 B 型：dockerd 原生 TLS 2376

远端 `daemon.json` / 启动参数：`--tlsverify --tlscacert --tlscert --tlskey -H=0.0.0.0:2376`，CA 签发**只给 bot 主机**的客户端证书；容器内配置：

```json
{ "id": "nas", "kind": "tls",
  "endpoint": "tcp://nas.lan:2376",
  "ca": "/app/data/docker-certs/nas/ca.pem",
  "cert": "/app/data/docker-certs/nas/cert.pem",
  "key": "/app/data/docker-certs/nas/key.pem" }
```

容器里对应注入 `DOCKER_HOST` / `DOCKER_TLS_VERIFY=1` / `DOCKER_CERT_PATH=/app/data/docker-certs/nas`。证书目录只读挂载（`./data/docker-certs:/app/data/docker-certs:ro`），`data/` 已在 `.gitignore` 里。

## 6. 配置：`data/docker-hosts.json`

```json
{
  "hosts": [
    {
      "id": "nas",
      "label": "本机 NAS",
      "kind": "local",
      "compose_roots": ["/mnt/data2/docker"]
    },
    {
      "id": "vps",
      "label": "Oracle 东京",
      "kind": "proxy",
      "endpoint": "tcp://10.0.0.5:2375",
      "readonly_mirror": "/remote/vps",
      "path_map": [{ "remote": "/opt", "local": "/remote/vps/opt" }]
    }
  ]
}
```

字段规则：

| 字段 | 适用 | 说明 |
|---|---|---|
| `id` | 全部 | 唯一、`[a-z0-9_-]{1,16}`，**回调里只认这个 id**（不接受任意字符串拼命令） |
| `label` | 全部 | 面板显示名 |
| `kind` | 全部 | `local` / `proxy`（明文 tcp）/ `tls`（2376 双向证书） |
| `endpoint` | proxy/tls | `tcp://…` / `https://…`；`kind=local` 时忽略 |
| `ca/cert/key` | tls | 容器内路径，必须存在且可读，否则该主机标记为「配置错误」而不是静默跳过 |
| `readonly_mirror` / `path_map` | proxy/tls | 见 §7 路径解析 |
| `enabled` | 全部 | 默认 true，便于临时下线一台主机 |

校验与容错（沿用现有风格：非法值回退 + 明确提示，不崩）：

* 文件不存在 → 单机模式（等价于今天，`local` 主机自动生成）；
* `id` 重复 / `kind` 未知 / tls 缺证书 → 该主机进入「配置错误」状态，面板与 `--health` 直接说明原因；
* 一台主机探测失败**不影响**其它主机（per-host `last_scan_error`）。

## 7. 路径解析：容器内必须能读到 yml

远端 `compose ls --format json` 给出的 `ConfigFiles` 是**远端绝对路径**（如 `/opt/blog/docker-compose.yml`）。容器里要有对应可读文件，两种挂载方式：

* **方案 A（推荐）：同路径只读挂载。** 用 NFS/SMB/sshfs 把远端 `/opt` 挂到容器内 `/opt`（`:ro`）。`path_map` 留空。好处：`-f` 路径、相对 `include`、`.env`、相对 bind 的解析都与远端完全一致。
* **方案 B：前缀映射。** 远端 `/opt` 挂到容器内 `/remote/vps/opt`，配置 `path_map: [{"remote": "/opt", "local": "/remote/vps/opt"}]`。扫描后把 `config_files` 与 `dir` 映射成容器内路径再交给 CLI；**yml 内部的相对路径与 `.env` 会按容器内布局解析**，因此只适合「单文件 yml、无 `include`、无相对 bind/env_file」的项目。

两种方式都要求副本**实时**（网络挂载优于定期 rsync）：本地副本的内容就是 `pull/up` 用的配置，副本过期 = 拿旧定义升级（§9 的漂移检测就是为了兜住这一点）。

## 8. 代码改造清单

| 文件 | 改动 |
|---|---|
| `mtbots/features/docker/hosts.py`（新） | `DockerHost` 数据类 + `load_hosts(path)` + 校验；`host.env()` 生成子进程环境；`host.display`（`vps/blog`） |
| `mtbots/features/docker/config.py` | `DockerSettings` 增 `hosts_file: Path`（默认 `data/docker-hosts.json`，可 `DOCKER_HOSTS_FILE` 覆盖） |
| `mtbots/features/docker/compose.py` | `scan_projects_sync()` 逐主机扫描，project 增 `host`/`host_label`/`local_dir`；`seen_keys` 与缓存键改 `(host_id, name)`；`last_scan_error`/`hidden_dirs` 改 per-host；`build_compose_cmd()` 接受 `host`；`run_command_with_feedback()` 支持 `env=`；`get_project_services`/`dump_container_status`/`scan_prune_candidates`/`format_prune_snapshot` 全部带主机；`scan_hint()` 按主机分支；`socket_group_hint()`/`common_mount_root()` 只对 `local` 生效 |
| `mtbots/features/docker/handlers.py` | 列表按主机分组（单主机时不显示主机头 → 与今天一致）；项目标签 `vps/blog`；回调载荷加 `host`（`up_p_do`/`up_svc_do`/`p_sel`/`prune_*`）；`/upgrade NN` 的编号仍是跨主机连续的扁平序号；`/d_status`、镜像清理按主机执行；未知 host id → `⚠️ 未知主机` |
| `mtbots/features/docker/__init__.py` | `summary()` / `id_lines()` 标注主机数与每台主机项目数 |
| `docker-compose.yml` / `.env.example` | 挂载只读副本目录与证书目录（`./data/docker-certs:/app/data/docker-certs:ro`、远端的 `readonly_mirror`）；`data/docker-hosts.json` 已在 `./data` 挂载内 |
| `Dockerfile` | **不用改**（这路线的优点：不需要 ssh 客户端，`ca-certificates` 已装） |
| 测试 | 见 §12 |
| 文档 | 本文 + README「多主机」章节（实现后再写）+ 设计稿 §5.10 + 合并报告 §10 |

## 9. 副本漂移检测（本方案的关键安全网）

升级前用 compose 自己的哈希对一遍「容器里的副本」与「远端实际部署的配置」：

```bash
# 容器内（本地副本）：
docker compose -f /opt/blog/docker-compose.yml config --hash web     # → web 1441334a754f…
# 远端（实际部署）：容器 label 就是同一个哈希
docker -H tcp://10.0.0.5:2375 inspect blog-web-1 \
  --format '{{index .Config.Labels "com.docker.compose.config-hash"}}'
```

* 已实测：`docker compose config --hash <service>` 在镜像内的 compose **v2.35.1** 可用（输出 `<service> <sha256>`）。
* 一致 → 正常升级。
* 不一致 → 收尾/确认面板打黄字：`⚠️ 远端实际部署的配置与容器内副本不一致（副本可能过期），升级会用副本里的定义`，并把该提示写进 job detail；用户仍可继续（确认面板本来就是两步）。
* 取不到 label（老项目、非 compose 管理的容器）→ 不误报，只在详情里注明「无法比对」。

## 10. 数据流（改造后）

```
打开面板 → 逐主机 scan（可缓存 15s；单台失败只标记该主机）
        → 面板：🖥 本机 NAS（2） / 🖥 Oracle 东京（3）
             nas/media   running(1)   vps/blog   exited(2)
点项目 → 详情（带主机标签）→ 升级确认（两步、绑定发起人、60s 过期）
        → 漂移比对（§9）→ run_command_with_feedback(env=DOCKER_HOST…, cmd=[docker,compose,-f,<容器内路径>,pull])
        → 面板进度（🛑 中断执行可用；取消 = 断开与远端 daemon 的连接/终止本地 CLI）
        → 收尾面板：✅ 🐳 升级项目 vps/blog · …（同一套 card_text）
镜像清理 → 按主机分别执行（`docker image prune` 影响的是那台主机）
--health → 逐主机：docker version / compose ls 条数 / 证书可读性 / 副本可读性 / 漂移比对
```

并发的取舍：保持现有「同一时刻只有一个 docker 长任务」的全局锁（`DockerState.begin_task`），跨主机**不做并行**——面板只有一个进度面，并行会把进度与取消语义搞乱。

## 11. 安全清单（红线）

1. **绝不裸 2375 到公网**；proxy 端口只绑内网 IP，只允许 bot 主机访问（WireGuard/Tailscale/防火墙白名单）。
2. proxy 放行**最小集**（§5.1）；`EXEC`/`BUILD`/`SWARM`/`SECRETS`/`CONFIGS`/`AUTH` 一律保持 0。
   注意：**开了 `VOLUMES` 就等于允许删除远端卷**（数据丢失风险），这是「允许升级」的固有代价，要么接受，要么对不声明命名卷的项目才开。
3. TLS 模式：只给 bot 主机签发客户端证书，证书只读挂载，`DOCKER_TLS_VERIFY=1`（不要 `DOCKER_TLS_VERIFY=0` 图省事）。
4. **回调只接受配置内的 host id**；host 字段不许来自用户输入的任何其它来源。远端路径来自 daemon 自己的 labels，不拼用户字符串。
5. 权限仍在 `core.acl`：`docker` 模块只给 owner/admin；`upgrade`/`prune` 走两步确认。
6. 审计：每条远端命令 INFO 记 `host=<id> cmd=<前 200 字符>`，失败原因进 job detail，可在 `/jobs` 回溯。
7. 密钥/证书目录权限 0600、只读挂载；`data/` 已 gitignore。

## 12. 测试点（约 20 条）

* 配置：无文件 = 单机；重复 `id`；未知 `kind`；tls 缺证书 → 主机标记「配置错误」而不是静默。
* 环境注入：`env=` 只作用于该子进程（不污染 `os.environ`）；TLS 三个变量齐全。
* 扫描：两主机合并；A 主机失败不影响 B；per-host `last_scan_error`；同名项目不互相覆盖（键是 `(host, name)`）。
* 路径：方案 A 同路径直通；方案 B `path_map` 前缀替换；映射后文件不存在 → 该主机给「副本未挂载」提示。
* 漂移：哈希一致无警告；不一致出现 `⚠️` 文案并进 job detail；拿不到 label 不误报。
* 回调：载荷带 `host`；伪造 `host=evil` 被拒（`⚠️ 未知主机`），且**不会**执行任何命令。
* 面板：单主机时**不出现主机头**（与 v1.0.7 文案逐字节一致，回归保护）；多主机分组；`/upgrade 03` 指向跨主机扁平编号的正确项目。
* 其它：prune/status/`--health` 按主机；`summary()` 主机数正确。
* 回归：现有 347 条用例在「无 hosts 文件」下全绿。

## 13. 分步落地（每步独立可验证、可回滚）

1. **只读接入**：`hosts.py` + 配置 + 扫描 + 面板分组 + `--health`。验收：面板能列出远端项目、错误提示准确；升级按钮先禁用（或对远端项目回「暂不支持」）。回滚 = 删 `docker-hosts.json`。
2. **升级/清理**：命令注入 `env`、确认与回执、漂移警告、per-host 提示。验收：远端 pull/up 全流程 + 在真机上验证 §9 的 label 名。
3. **排错面完善**：403/证书/副本/版本不匹配（远端 daemon 太老 → 允许按主机设 `DOCKER_API_VERSION`）四类提示。
4. **文档与示例**：README 多主机章节 + `docs/examples/docker-socket-proxy.yml` + 合并报告 §10；可选把 proxy 写进 `docker-compose.yml` 的 profile。

## 14. 风险

| 风险 | 缓解 |
|---|---|
| 副本过期 → 按旧 yml 升级 | 只读实时挂载（NFS/SMB）+ §9 漂移检测 + 面板黄字警告 |
| proxy 权限过大 | §5.1 最小放行 + 网络隔离 + `docker` 模块只给 owner/admin |
| 远端 daemon / API 版本不匹配 | per-host `DOCKER_API_VERSION` 配置 + `--health` 探测并提示 |
| 网络抖动导致升级「半途而废」 | 现有 `run_command_with_feedback` 已按退出码判定 + 收尾面板给尾部输出；compose 的 pull/up 本身幂等，重跑即可 |
| 一台主机拖慢面板 | per-host 缓存 + 单主机超时（沿用 `SCAN_TIMEOUT`），失败只标记该主机 |

## 15. 实现前待确认

1. 远端机器是什么？是否有内网互通（WireGuard/Tailscale/同 VLAN）？——决定用 A 型还是 B 型。
2. 远端 compose 目录能否**实时只读挂载**到容器（NFS/SMB/sshfs）？不能的话只能走方案 B（前缀映射，限制见 §7）或改用 SSH 路线。
3. 要不要顺便把 `docker-socket-proxy` 也用于**本机**（合并报告 §8 第 5 条），让主进程完全不碰 socket？——可以一起做，也可以留到下一轮。
