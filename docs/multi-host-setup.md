# 多主机接入：手动步骤与实现细节

README 的 [多主机](../README.md#多主机) 一节是主推路径（`make add-host`、远端一条命令）。这份文档是它的**等价手动步骤**，加上面板样例、提速数据与原理，脚本跑不通时照这里做，想搞清楚为什么这么设计也看这里。

| 章节 | 内容 |
|---|---|
| [1. 手动步骤](#1-手动步骤) | 七步：权限、远端准备、密钥、守卫、known_hosts、清单、生效 |
| [2. SSH 路线](#2-ssh-路线不暴露-docker-端口) | 为什么不把远端 docker 端口暴露给容器 |
| [3. 面板与按钮](#3-面板与按钮) | 多主机下的面板实际长什么样 |
| [4. 面板提速](#4-面板提速) | 实测数字与五条优化 |
| [5. 安全与回滚](#5-安全与回滚) | 常态化建议与一键回退单机 |
| [6. 相关文件](#6-相关文件) | 脚本与样例清单 |

主机清单的字段表、生效命令在 README 里（两条路径共用），本文只给指针，不重复。

## 1. 手动步骤

假设项目根是 `/mbots`（里面有 `docker-compose.yml`、`.env`、`data/`）。多主机用到的东西全生成在 `data/` 里，不用新增 volume（`docker-compose.yml` 已有 `./data:/app/data`）。

### 1.1 目录与权限

| 宿主机（项目根下） | 容器内 | 作用 | 权限 |
|---|---|---|---|
| `data/docker-hosts.json` | `/app/data/docker-hosts.json` | 主机清单 | 容器用户（uid **10001**）**可读** |
| `data/ssh/id_ed25519` | `/app/data/ssh/id_ed25519` | 远端私钥 | **0600，属主必须是 10001** |
| `data/ssh/id_ed25519.pub` | 同路径 | 公钥，贴到远端 | 无所谓 |
| `data/ssh/known_hosts` | `/app/data/ssh/known_hosts` | 远端主机指纹 | 0600；`strict=accept-new` 时还要**可写** |
| `data/config.json`、`data/logs/` | `/app/data/…` | 原有内容 | 已有约定 |

`data/` 在 `.gitignore` 里，密钥不会被提交。

**最容易踩的一条**：容器不是 root，是 **uid 10001**（Dockerfile 里 `useradd --uid 10001`）。宿主机上 root 生成的 `root:root 600` 密钥，容器里连 `head -c1` 都读不到（`NOT_READABLE`），`chown -R 10001:10001` 之后才 `READABLE`。读不到私钥时 ssh 只回一句含糊的 `Permission denied (publickey)`（前面可能带 `Identity file ... not accessible`），很容易误判成「远端公钥没装对」。所以 [1.3](#13-在-mtbots-这边生成密钥) 的 `chown` 不能省。

### 1.2 远端准备

每台远端做一次：

```bash
sudo useradd -m -s /bin/bash mtbots      # 专用用户，别用登录账号，更别用 root
sudo usermod -aG docker mtbots

# 真正的家目录：NAS 上常常不在 /home（群晖是 /var/services/homes/mtbots）
HOME_DIR=$(getent passwd mtbots | cut -d: -f6)
sudo mkdir -p "$HOME_DIR/.ssh"            # 家目录和 .ssh 的属主必须是 mtbots，
sudo chown mtbots:mtbots "$HOME_DIR" "$HOME_DIR/.ssh"   # 否则它连自己的家都进不去
sudo chmod 700 "$HOME_DIR/.ssh"

sudo -u mtbots docker compose version     # 确认这个用户真能用 docker
```

报 `permission denied while trying to connect to the Docker daemon socket` = docker 组没生效，重新登录或 `newgrp docker` 再试。写 `authorized_keys` 报 `Permission denied` = 家目录或 `.ssh` 属主不对（`useradd` 忘了 `-m`、家目录早先被 root 建过、或家目录在 NAS 的非标准路径），`sudo ls -ld "$HOME_DIR" "$HOME_DIR/.ssh"` 看一眼后 `chown`。

### 1.3 在 MTBots 这边生成密钥

```bash
cd /mbots
mkdir -p ./data/ssh && chmod 700 ./data/ssh
ssh-keygen -t ed25519 -N '' -C mtbots@bot -f ./data/ssh/id_ed25519   # 不要口令：容器里没法交互输入
chmod 600 ./data/ssh/id_ed25519
sudo chown -R 10001:10001 ./data/ssh     # 关键：属主交给容器用户
ls -l ./data/ssh                         # 期望 -rw------- 1 10001 10001 id_ed25519
cat ./data/ssh/id_ed25519.pub            # 下一步贴到远端
```

### 1.4 装公钥 + 强制命令守卫

守卫把这条 key 能跑的命令限定成 **16 条白名单形态**（`docker compose` 与 `docker-compose` 各 6 条 + 只读查询 3 条 + 镜像清理 1 条），并**整条拒绝含 shell 元字符的命令**（分号、`&`、`|`、`$`、反引号、`\`、`>`、`<`）。

```bash
# 远端：把 docs/examples/mtbots-compose-guard.sh 传上去，或直接粘贴创建
sudo install -m 755 mtbots-compose-guard.sh /usr/local/bin/mtbots-compose-guard

# 远端：写公钥，必须带 command= 与 restrict
HOME_DIR=$(getent passwd mtbots | cut -d: -f6)
echo 'command="/usr/local/bin/mtbots-compose-guard",restrict ssh-ed25519 AAAAC3Nza... mtbots@bot' \
  | sudo tee -a "$HOME_DIR/.ssh/authorized_keys" >/dev/null
sudo chown mtbots:mtbots "$HOME_DIR/.ssh/authorized_keys"
sudo chmod 600 "$HOME_DIR/.ssh/authorized_keys"
sudo -u mtbots tail -n 2 "$HOME_DIR/.ssh/authorized_keys"    # 确认进去了、mtbots 能读
```

用 root 写、写完把属主交回去，就不用跟 `sudo -u mtbots` 的家目录属主问题纠缠。更严一点可以再加来源限制：`from="10.0.0.9",command="…",restrict ssh-ed25519 …`，样例见 [authorized_keys.sample](examples/authorized_keys.sample)。

加了守卫，万一 MTBots 主机被拿下，攻击者拿到的也只是「能对这几个 compose 项目做 pull / up -d / stop / 只读查询」，拿不到远端 shell（`bash -i`、`docker run`、`docker exec`、`curl` 都会被 `exit 126` 顶回去）。

在容器里验证这条链路，**这一步过了多主机基本就成了**：

```bash
docker compose exec mtbots \
  ssh -p 22 -i /app/data/ssh/id_ed25519 -o BatchMode=yes \
      -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/app/data/ssh/known_hosts \
      -- mtbots@10.0.0.5 docker compose version
```

### 1.5 known_hosts：两种模式选一个

| 模式 | 清单里写 | 做法 | 适用 |
|---|---|---|---|
| `accept-new` | `"strict": "accept-new"` | 首次连接自动把指纹写进 `/app/data/ssh/known_hosts`，该目录必须可写（`chmod 700` + 属主 10001） | 内网、图省事 |
| `yes` | `"strict": "yes"` | 先在宿主机预置：`ssh-keyscan -p 22 10.0.0.5 >> /mbots/data/ssh/known_hosts && sudo chown 10001:10001 /mbots/data/ssh/known_hosts`，之后文件可以只读 | 生产、防中间人 |

远端换过主机密钥时 `yes` 会直接拒连并提示 host key 变化；确认没问题后删掉 `known_hosts` 里那行重新 `ssh-keyscan`。

### 1.6 写主机清单

字段含义与出错行为见 README 的 [主机清单](../README.md#主机清单)，样例文件 [examples/docker-hosts.json](examples/docker-hosts.json)：

```bash
cp docs/examples/docker-hosts.json /mbots/data/docker-hosts.json   # 然后按字段表改
```

### 1.7 生效与验证

见 README 的 [生效与验证](../README.md#生效与验证)：重建容器 → 日志里确认清单被读进去 → `--health` 逐主机自检，然后回 Telegram 发 `/d_list`。

## 2. SSH 路线（不暴露 docker 端口）

`docker compose` 是「在本地读 yml，再把 API 请求发给目标 daemon」。把远端 daemon 的端口暴露给容器（`DOCKER_HOST` 那套）就得让容器读到远端那份 yml，而 yml 里的相对路径（`./data:/data`）会被本地 CLI 解析成绝对路径再发给远端 daemon——路径对不上时 dockerd 会**自动建一个空目录顶上**，容器静默挂到空目录，数据看着没了还不报错。

所以 ssh 过去让远端的 CLI 自己解析。一次「升级项目」实际执行的就是这一条：

```bash
ssh -p 22 -i /app/data/ssh/id_ed25519 \
    -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 \
    -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/app/data/ssh/known_hosts \
    -- mtbots@10.0.0.5 'docker compose -f /opt/blog/docker-compose.yml pull'
```

`--` 放在目标**之前**结束选项解析，否则以 `-` 开头的 target 会被 ssh 当成选项（见 README 安全红线 8）。

好处是零挂载（远端目录都不用挂进容器）、零漂移（用的永远是远端那份 yml）、版本天然匹配（用远端自己的 `docker compose`，远端只有老版 `docker-compose` 也能用，会自动探测回退）；代价是镜像里多了 `openssh-client`（约 10MB）与 `curl`、一把只读私钥，远端要建一个 `docker` 组用户。

## 3. 面板与按钮

多主机时 `🐳 Docker` 进来先是选主机：

```text
📊 共 2 个项目（🟢 2 运行中），分布在 2 台主机
🖥 主机：NAS 1 / VPS 1

请选择要管理的主机：
   [🖥 NAS（1 个项目）]
   [🖥 VPS（1 个项目）]
   [📚 全部主机（2 个项目）]
   [🧹 镜像清理菜单]  [🏠 返回]
```

选完只看那一台，批量升级也只作用于那台：

```text
📊 统计：共 2 个项目 | 🟢 2 运行中 | 🟡 0 停止
🖥 主机：VPS（1 个项目）
📖 页码：1 / 1

01. 192-168-1-5/docker 🟢 [running(4)]
     路径：/mnt/data1/docker
     容器：a
   [🚀 01. 192-168-1-5/docker]
   [📄 1/1]
   [🧹 镜像清理菜单]  [⬆️ 升级这台全部项目]
   [🔄 刷新状态]  [🖥 换主机]  [🏠 返回]
```

首页直接把每台主机摆出来（少点一次）：`🐳 docker（NAS）` `🐳 docker（VPS）`，有每台主机的入口时不再重复一个笼统的「Docker 管理」按钮。要进「选主机 / 全部主机 / 镜像清理」，从任意一台的列表里点 `🖥 换主机`。

跟单机的差别：

* 第一屏选主机；「📚 全部主机」里每台一段，0 个项目的、连不上的、配置写错的都会写明原因（以前只画有项目的主机，远端空空如也就完全看不见）；
* 项目编号全库连续（与 `/upgrade NN` 同序），换主机查看不会改变编号；标签带主机前缀 `vps/blog`；
* 镜像清理按主机执行：多主机时先选主机、再选清理范围；`/d_status`、`--health` 也逐主机出结果；
* 主机配置错（id 非法 / target 非法 / 私钥缺失）时不给它按钮，只写在正文里说明原因——既避免点了只报「未知主机」，也避免 callback_data 超 Telegram 的 64 字节上限；
* 远端 compose 探测（同步 ssh）一律挪到线程里，一台连不上的主机不会冻住所有人的 update；
* 日志里能看清扫描结果：`扫描完成（0.4s）：local 15、192-168-1-5 10，共 25 个项目`，失败也有 `主机 X 扫描失败：…`（结果没变化时降到 DEBUG，不刷屏）；
* 单主机时以上全都不出现，面板文案、编号、`/jobs` 标题与 1.0.x 一字不差（有回归用例锁死）。

## 4. 面板提速

慢的原因只有一条：为了画列表多跑了很多子进程。实测（本机回环）：

| 命令 | 单次耗时 |
|---|---|
| `docker compose ls -a --format json` | 0.10s / 主机（真正必需的只有这条） |
| `docker compose config --services` | 0.14s / 项目 |
| `ssh` 握手（纯回环） | 0.36s / 次 |

15 个本机 + 10 个远端项目按老做法 = 15×0.14 + 10×(0.36+…) ≈ **8–10 秒**，每次都要跑，还串行。现在：

1. **服务列表（`容器：…`）按需 + 缓存**：扫描只跑 `compose ls`；`config --services` 只对当前这一页（`PAGE_SIZE`）和详情页取，结果缓存 `SERVICES_CACHE_TTL` = 300s，翻页 / 换主机 / 重新扫描直接复用（升级或清理后作废）。取失败**不写缓存**，但会进入 `SERVICES_FAIL_TTL` = 60s 的退避窗口：窗口内不重跑命令、面板继续显示上次的原因，窗口过后自动重试；🔄 强制刷新或升级 / 清理会立刻清掉退避。
   解析失败时（典型：项目目录的 `.env` 归 root、容器用户 uid 10001 读不到，`${VAR:?}` 插值直接失败）自动退回**容器 label** 读服务名（`docker ps -a --filter label=com.docker.compose.project=…`，不读任何文件），面板会标「来自容器」；两级都失败才显示 `⚠️ 未获取：<原因>`。**不需要为了能检测到服务去改 `.env` 的属主或权限。**
2. **多主机并行扫描**：每台一条线程，最多 4 台并发。
3. **SSH 连接复用**：同一台主机的第 2..N 条命令走 `ControlMaster` 复用同一条 TCP（`ControlPath=/tmp/mtbots-ssh-%C`、`ControlPersist=60`），省掉每次 0.36s 的握手。老 sshd 或中间设备不接受复用时 `.env` 里设 `SSH_MULTIPLEX=0`（目录用 `SSH_CONTROL_DIR` 换；目录不可写、或值里带空白引号时自动退回每次握手，不会把远端命令全打挂）。
4. **远端探测快速失败**：`docker compose version` 返回 255（连不上 / 认证失败 / 守卫拒绝）就不再试 `docker-compose`，死主机上限从 20s 降到 10s。
5. **首页刷新不阻塞**：首帧永远是缓存（见 README 交互约定第 7 条）。

主面板首屏因此从 ~8s 降到**亚秒级**（每台主机只剩一条 `compose ls`，并行），服务列表随翻页补齐。

## 5. 安全与回滚

* 远端用**专用用户 + `docker` 组**，不要 root、不要复用登录账号；一定要加 `command=` 守卫（[1.4](#14-装公钥--强制命令守卫)），这是 SSH 路线相对「暴露 docker 端口」最大的优势；
* 私钥只放 `data/`（已 gitignore），`0600` + 属主 `10001`；生产建议 `strict=yes` 配预置 `known_hosts`；
* 权限归 ACL：`docker` 模块默认只给 owner/admin，`upgrade` / `prune` / `stop` 都走两步确认；
* **回滚**：删掉 `/mbots/data/docker-hosts.json` → `docker compose up -d --force-recreate`，立刻回到单机。密钥留着不影响，以后想再加不用重新生成。

## 6. 相关文件

* [scripts/setup-remote-host.sh](../scripts/setup-remote-host.sh)——bot 侧接入向导，`make add-host`
* [docs/examples/mtbots-remote-setup.sh](examples/mtbots-remote-setup.sh)——远端一次性准备
* [docs/examples/mtbots-compose-guard.sh](examples/mtbots-compose-guard.sh)——守卫脚本
* [docs/examples/docker-hosts.json](examples/docker-hosts.json)——主机清单样例
* [docs/examples/authorized_keys.sample](examples/authorized_keys.sample)——authorized_keys 样例
* [docs/docker-multi-host-design.md](docker-multi-host-design.md)——设计与落地清单
