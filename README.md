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

## 管理多台服务器（多主机）

一个 MTBots 同时管理**本机 + 若干远端服务器**上的 Compose 项目：列表、详情、升级（整项目 / 单服务 / 批量）、镜像清理、`/d_status`、`--health` 全部覆盖。

> **不配这一节的文件时，行为与以前完全一致**：只有一台「本机」，面板上没有主机字样，也不会执行任何 ssh。
> 远端是**可选能力**，随时可以加、也可以删（删掉配置文件就回到单机）。

### 一键接入：bot 侧一条命令

```bash
cd /mbots && make add-host
```

就这一句，**参数全都由脚本在跑的过程中问**（每一问都有默认值，直接回车也行）。
`make add-host` 会用 `curl` 拉 `main` 上最新的向导（宿主机没 curl 才退回镜像里那份）——
所以**脚本改动不需要升级镜像**，重新跑一次拿到的就是最新的。



| 脚本会问 | 说明 |
|---|---|
| 远端 IP 或域名 | 例如 `10.0.0.5` |
| 远端准备方式 | `1` 复用已有账号（它已经能用 docker）；`2` 新建专用用户（需要一个能 sudo 的登录账号） |
| 账号 | 默认都是 **`mtbots`**（跟远端脚本、守卫脚本的示例一致）；方式 2 另问一个「登录做初始化」的账号（默认 `root`——那必须是远端**已经存在且能 sudo** 的账号，不是要新建的 mtbots） |
| 主机 id / 显示名 | 面板里用的短名（默认从地址推导），例如 `vps` / `Oracle 东京` |
| 路径白名单 | 可留空（= 不限） |

它随后自动做完这些：生成/复用 `data/ssh/id_ed25519`（属主交给容器用户 `10001`）→ 驱动远端准备 →
验证 `ssh → 守卫 → docker compose version` → 按 id **合并**写进 `data/docker-hosts.json` → 问你要不要重建容器。

没克隆仓库、想直接跑也行（**在项目根的宿主机上**跑；容器里没有 curl）：

```bash
cd /mbots
bash <(curl -fsSL https://raw.githubusercontent.com/MbAIGC/MTBots/main/scripts/setup-remote-host.sh)
```

这种模式下「项目根」= 当前目录；守卫与远端脚本不在本地时会**按 ref 自动下载**（`--ref` 默认取当前 MTBots 版本，
取不到用 `main` 并给出警告）。不是 bash 的 shell 用管道形式也一样：
`curl -fsSL <同一个 URL> | sh`——脚本的提问读 `/dev/tty`，管道不会把问题吃掉。

> URL 里用的是 `main`，所以不用跟着版本号改，**脚本升级也不牵扯镜像**（这两件事现在解耦了：
> 脚本在仓库里，镜像只是顺带带一份离线副本）。想锁死某个版本：
> 把 URL 里的 `main` 换成 `v1.3.4`，或加 `--ref v1.3.4`。

### 一键接入：远端侧一条命令

远端那台**没法让 bot 直接 ssh 进去**（要先用密码、或者得从跳板机进）时，在**远端主机**上以 root 跑这一条：

```bash
# 在远端主机上（root / sudo）——就这一句，没有参数
sudo bash <(curl -fsSL https://raw.githubusercontent.com/MbAIGC/MTBots/main/docs/examples/mtbots-remote-setup.sh)

# 不是 bash 的 shell（群晖等 /bin/sh）用管道形式，效果一样：
# curl -fsSL https://raw.githubusercontent.com/MbAIGC/MTBots/main/docs/examples/mtbots-remote-setup.sh | sudo sh
```

跑起来只有几问，全部有默认值（回车即可）：

```text
== MTBots 远端向导（直接回车 = 用默认值）==
要授权/创建的远端账号 [mtbots]:
把 MTBots 那台 ./data/ssh/id_ed25519.pub 的整行内容粘进来（也可以给文件路径或 http 地址）：
公钥: ssh-ed25519 AAAAC3Nza... mtbots@bot
装强制命令守卫（自动下载官方那份） (y/n) [y]:
守卫安装目录 [/usr/local/bin]:
```

它随后自己做完：建用户 → 加 `docker` 组 → 修家目录/.ssh 属主权限 → **下载并安装守卫** →
写 `authorized_keys`（`command="…",restrict`；幂等、改前备份、别人的 key 不动）→ 自检 docker 可用性。

不用记那行 curl？让脚本替你打印（连公钥一起给你，方便粘）：

```bash
cd /mbots && make remote-setup
```

远端跑完，回 bot 这边把主机写进清单（向导发现密钥已可用，就只写清单 + 验证）：

```bash
cd /mbots && make add-host          # 方式选 1「复用已有账号」，账号填 mtbots
```

> 远端脚本里的守卫默认从 `main` 拉（跟脚本同源），所以不用手写守卫 URL；要指定版本就加 `--ref v1.3.4`。

### 非交互（CI / 批量，可选）

两个脚本都支持把参数全写出来（`--yes`/`-y` 表示不再提问）：

```bash
# bot 侧
docker compose exec mtbots sh /app/scripts/setup-remote-host.sh \
  --mode create --host 10.0.0.5 --login-user root --user mtbots --id vps --yes

# 远端侧（等价于上面那几问的回答）
sudo bash <(curl -fsSL https://raw.githubusercontent.com/MbAIGC/MTBots/main/docs/examples/mtbots-remote-setup.sh) \
  --user mtbots --yes --pubkey-line 'ssh-ed25519 AAAAC3Nza... mtbots@bot'
```

不想用脚本：下面 §0–§10 是**完整手动步骤**，脚本做的与手动完全等价；脚本在你环境里跑不通时就照手动来。

### 0. 先讲清楚它到底怎么跑（为什么是 SSH）

Compose 的命令行工具是「**在本地读 yml，再把 API 请求发给目标 daemon**」。所以如果把远端 daemon 的端口暴露给容器（`DOCKER_HOST` 那套），容器里就必须**能读到远端那份 yml**；而且 yml 里的相对路径（`./data:/data`）会被本地 CLI 解析成**绝对路径**再发给远端 daemon——路径对不上时 dockerd 会**自动建一个空目录顶上**，容器静默挂到空目录，数据看着「没了」还不报错。

所以这里选了另一条路：**ssh 到远端，让远端的 CLI 去解析**。一次「升级项目」实际执行的就是这一条命令（你在面板上看到的进度就是它的输出）：

```bash
ssh -p 22 -i /app/data/ssh/id_ed25519 \
    -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 \
    -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/app/data/ssh/known_hosts \
    mtbots@10.0.0.5 -- 'docker compose -f /opt/blog/docker-compose.yml pull'
```

由此得到三个好处，和一个代价：

| | |
|---|---|
| ✅ **零挂载** | 远端目录一个都不用挂进容器，也不用 rsync 副本 |
| ✅ **零漂移** | 用的永远是远端那份 yml 本身，不存在「拿旧定义升级」 |
| ✅ **版本天然匹配** | 用的是远端自己的 `docker compose`（远端只有老版 `docker-compose` 也能用，会自动探测回退） |
| ⚠️ 代价 | 镜像里多了 `openssh-client`（约 10MB）+ 一把只读私钥；要在远端建一个 `docker` 组用户 |

### 1. 目录与权限约定（先看这个，最容易踩）

假设你的 MTBots 项目根目录是 **`/mbots`**（里面有 `docker-compose.yml`、`.env`、`data/`）。多主机用到的东西**全部生成在项目根的 `data/` 里**，不需要新增任何 volume（`docker-compose.yml` 已经有 `./data:/app/data`）：

| 宿主机（项目根下） | 容器内 | 作用 | 权限要求 |
|---|---|---|---|
| `/mbots/data/docker-hosts.json` | `/app/data/docker-hosts.json` | 主机清单 | 容器用户（uid **10001**）**可读** |
| `/mbots/data/ssh/id_ed25519` | `/app/data/ssh/id_ed25519` | 远端私钥 | **0600，且属主必须是 10001** |
| `/mbots/data/ssh/id_ed25519.pub` | 同路径 | 公钥（贴到远端） | 无所谓 |
| `/mbots/data/ssh/known_hosts` | `/app/data/ssh/known_hosts` | 远端主机指纹 | 0600；`strict=accept-new` 时还要**可写** |
| `/mbots/data/config.json`、`logs/` | `/app/data/…` | 原有内容（不受影响） | 已有约定 |

`data/` 本来就在 `.gitignore` 里，**密钥不会被提交**。

> ⚠️ **最容易踩的一条**：容器不是 root，而是 **uid 10001**（`Dockerfile` 里 `useradd --uid 10001`）。
> 已实测：宿主机上 root 生成的 `root:root 600` 密钥，在容器里 `head -c1` 都读不到（`NOT_READABLE`）；`chown -R 10001:10001` 之后才 `READABLE`。
> 读不到私钥时 ssh 只会含糊地回一句 `Permission denied (publickey)`（前面可能带一句 `Identity file ... not accessible`），很容易误以为「远端公钥没装对」。
> 所以下面第 3 步的 `chown -R 10001:10001 ./data/ssh` 不能省。

### 2. 远端准备（每台远端做一次）

```bash
# 远端执行：建一个专用用户（别用你的登录账号，更别用 root），并加入 docker 组
sudo useradd -m -s /bin/bash mtbots
sudo usermod -aG docker mtbots

# 取这个用户**真正的**家目录：NAS 上常常不在 /home（群晖是 /var/services/homes/mtbots）
HOME_DIR=$(getent passwd mtbots | cut -d: -f6)
echo "$HOME_DIR"

# 家目录和 .ssh 的属主都必须是 mtbots，否则它连自己的家都进不去（下一步会 Permission denied）
sudo mkdir -p "$HOME_DIR/.ssh"
sudo chown mtbots:mtbots "$HOME_DIR" "$HOME_DIR/.ssh"
sudo chmod 700 "$HOME_DIR/.ssh"

# 确认这个用户真的能用 docker / compose（很多「连不上」其实是这一步没过）
sudo -u mtbots docker compose version
```

两个常见的坑：

* `docker compose version` 报 `permission denied while trying to connect to the Docker daemon socket` → docker 组没生效，重新登录（或 `newgrp docker`）后再试；
* 后面写 `authorized_keys` 时若报 `Permission denied` → 家目录或 `.ssh` 属主不是 `mtbots`（`useradd` 忘了 `-m`、家目录早先被 root 建过、或家目录在 NAS 的非标准路径）。先用 `sudo ls -ld "$HOME_DIR" "$HOME_DIR/.ssh"` 看一眼，属主不对就 `sudo chown mtbots:mtbots …`。

### 3. 在 MTBots 这边生成密钥

```bash
cd /mbots
mkdir -p ./data/ssh && chmod 700 ./data/ssh

# 生成一对 ed25519 密钥（-N '' = 不要口令，容器里没法交互输入）
ssh-keygen -t ed25519 -N '' -C mtbots@bot -f ./data/ssh/id_ed25519
chmod 600 ./data/ssh/id_ed25519

# 关键：把属主交给容器用户（uid 10001），否则容器里的 ssh 读不到私钥
sudo chown -R 10001:10001 ./data/ssh
ls -l ./data/ssh        # 期望：-rw------- 1 10001 10001 id_ed25519

cat ./data/ssh/id_ed25519.pub      # 下一步要贴到远端
```

### 4. 装公钥 + **强制命令守卫**（强烈建议）

把守卫脚本放到远端（它把这条 key 能跑的命令限定成「MTBots 会用到的那 13 种形态」）：

```bash
# 把 docs/examples/mtbots-compose-guard.sh 传上去（或直接粘贴创建）
sudo install -m 755 mtbots-compose-guard.sh /usr/local/bin/mtbots-compose-guard
```

然后把公钥写进远端的 `authorized_keys`，**并加上 `command=` 与 `restrict`**：

```bash
HOME_DIR=$(getent passwd mtbots | cut -d: -f6)

# 用 root 写（避免 sudo -u mtbots 在家目录属主不对时 Permission denied），写完再把属主交回去
echo 'command="/usr/local/bin/mtbots-compose-guard",restrict ssh-ed25519 AAAAC3Nza... mtbots@bot' \
  | sudo tee -a "$HOME_DIR/.ssh/authorized_keys" >/dev/null
sudo chown mtbots:mtbots "$HOME_DIR/.ssh/authorized_keys"
sudo chmod 600 "$HOME_DIR/.ssh/authorized_keys"

# 确认内容进去了、且 mtbots 自己能读
sudo -u mtbots tail -n 2 "$HOME_DIR/.ssh/authorized_keys"
```

> 如果你手边报的是 `tee: /home/mtbots/.ssh/authorized_keys: Permission denied`：那是**家目录或 `.ssh` 的属主不是 `mtbots`**，跟公钥内容无关。
> 先 `sudo ls -ld "$HOME_DIR" "$HOME_DIR/.ssh"` 看属主，`sudo chown mtbots:mtbots "$HOME_DIR" "$HOME_DIR/.ssh"` 修好即可（或者就一直用上面的 `sudo tee` + `chown` 写法，`sudo -u` 这步可以完全不用）。

（更严一点可以再加来源限制：`from="10.0.0.9",command="…",restrict ssh-ed25519 …`，样例见 [`docs/examples/authorized_keys.sample`](docs/examples/authorized_keys.sample)。）

**为什么值得加**：万一 MTBots 主机被拿下，攻击者拿到的也只是「能对这几个 compose 项目做 pull / up / 只读查询」，**拿不到远端 shell**（`bash -i`、`docker run`、`docker exec`、`curl` 都会被 `exit 126` 顶回去）。守卫脚本内容与逐条验证结果见 [`docs/examples/mtbots-compose-guard.sh`](docs/examples/mtbots-compose-guard.sh)。

在容器里验证这条链路（**这一步过了，多主机基本就成了**）：

```bash
docker compose exec mtbots \
  ssh -p 22 -i /app/data/ssh/id_ed25519 -o BatchMode=yes \
      -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/app/data/ssh/known_hosts \
      mtbots@10.0.0.5 docker compose version
```

### 5. known_hosts：两种模式，选一个

| 模式 | 清单里写 | 做法 | 适用 |
|---|---|---|---|
| **accept-new**（示例默认，省事） | `"strict": "accept-new"` | 首次连接自动把指纹写进 `/app/data/ssh/known_hosts`；**该目录必须可写**（`chmod 700` + 属主 10001） | 内网、图省事 |
| **yes**（更严） | `"strict": "yes"` | 先在**宿主机**预置指纹：`ssh-keyscan -p 22 10.0.0.5 >> /mbots/data/ssh/known_hosts && sudo chown 10001:10001 /mbots/data/ssh/known_hosts`；文件之后可以只读 | 生产、想防中间人 |

远端换过主机密钥时，`yes` 会**直接拒连**并提示 host key 变化——这是你想要的行为；确认没问题后删掉 `known_hosts` 里那一行重新 `ssh-keyscan` 即可。

### 6. 写主机清单 `/mbots/data/docker-hosts.json`

```bash
cp docs/examples/docker-hosts.json /mbots/data/docker-hosts.json   # 然后按下面改
```

```json
{
  "hosts": [
    { "id": "nas", "label": "本机 NAS", "kind": "local" },

    { "id": "vps", "label": "Oracle 东京", "kind": "ssh",
      "target": "mtbots@10.0.0.5",
      "port": 22,
      "identity": "/app/data/ssh/id_ed25519",
      "known_hosts": "/app/data/ssh/known_hosts",
      "strict": "accept-new",
      "roots": ["/opt"] }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 主机标识，`[a-z0-9_-]{1,16}`。面板/回调里**只认这个 id**（伪造的会被拒并记日志，绝不会拿去拼命令） |
| `label` | | 面板显示名（缺省=用 `id`） |
| `kind` | ✅ | `local`（本机，走挂进容器的 docker.sock）或 `ssh`（远端） |
| `target` | ssh ✅ | `user@host`。格式非法（带空格、分号、`-o` 之类）直接判为配置错误 |
| `port` | | 默认 22 |
| `identity` | | 私钥路径，默认 `/app/data/ssh/id_ed25519` |
| `known_hosts` | | 指纹文件，默认 `/app/data/ssh/known_hosts` |
| `strict` | | `accept-new`（默认）或 `yes`，见上一节 |
| `roots` | | **可选**的路径白名单：只管理这些前缀下的项目（例如只让 bot 管 `/opt` 下的东西），纵深防御 |
| `enabled` | | 默认 true；置 false 可临时下线一台主机而不删配置 |

清单**怎么坏都能看出原因**，不会静默：

* 没有这个文件（或路径指向不存在的文件）→ 单机模式，什么都不提示（这是默认形态）；
* 整份 JSON 解析失败 / 没有 `hosts` 列表 → 面板提示「⚠️ 读取失败，已按单机模式运行：<原因>」；
* 只有某一条主机非法（`target` 写错、`kind` 拼错、私钥不存在、`roots` 不是绝对路径…）→ **那台**带错误说明，其余主机照常工作；
* `id` 重复 → 保留第一条 + 面板提示。

改路径：清单默认读 `data/docker-hosts.json`（容器内 = `/app/data/docker-hosts.json`），要换位置就设 `DOCKER_HOSTS_FILE=/app/data/xxx.json`。

### 7. 生效与验证

```bash
cd /mbots
docker compose up -d --force-recreate        # 清单是启动时读的，改完要重建容器

# 日志里确认主机清单被读进去了
docker compose logs --tail=50 mtbots | grep -E "主机|docker 模块已注册"

# 逐主机自检（私钥、连通性、项目数；不连 Telegram）
docker compose exec mtbots python -m mtbots --health | grep 🐳
```

然后回到 Telegram 发 `/d_list`。

### 8. 生效后的面板长这样（真代码渲染）

```
📊 统计：共 2 个项目 | 🟢 1 运行中 | 🟡 1 停止
🖥 主机：nas 1 / vps 1
📖 页码：1 / 1

🖥 本机 NAS
01. nas/media 🟢 [running(1)]
     主机：本机 NAS
     路径：/mnt/data2/docker/media
     容器：emby

🖥 Oracle 东京
02. vps/blog 🟡 [exited(2)]
     主机：Oracle 东京
     路径：/opt/blog
     容器：web, db
   [🚀 01. nas/media]  [⚙️ 02. vps/blog (多服务)]
```

行为上的变化（与单机对比）：

* 项目标签带主机前缀 `vps/blog`；`/upgrade 02` 的编号与面板**同序**（跨主机连续编号，面板与命令行共用同一套排序）；
* **镜像清理按主机执行**：多主机时先选主机，再选清理范围；
* `/d_status` 每台主机一段；`--health` 逐主机报项目数；
* **单主机时以上全都不出现**——面板文案、编号、`/jobs` 标题与 1.0.x 一字不差（有回归用例锁死）；
* 失败不静默：某台主机连不上、或远端没装 compose、或被守卫拒绝，面板会在列表下方给出原因**和一条能直接抄的自测命令**（v1.1.1 起列表非空时也会提示）。

### 9. 排错

| 现象 | 处理 |
|---|---|
| 面板：「主机 vps：SSH 连不上或认证失败」 | 按面板给的自测命令在容器里跑（见第 4 节最后一段）。它其实已经把原因写在输出里：`Connection refused`=端口/网络、`Permission denied (publickey)`=公钥没装对、`not accessible: Permission denied`=私钥权限/属主不对（见第 1 节 ⚠️） |
| 远端 `sudo -u mtbots tee …/authorized_keys` 报 `Permission denied` | 家目录或 `.ssh` 的属主不是 `mtbots`（`useradd` 没带 `-m` / 家目录早先被 root 建过 / NAS 家目录不在 `/home`）。用 `getent passwd mtbots` 取真实家目录并 `chown mtbots:mtbots`，或者按第 4 节用 `sudo tee` + `chown` 写文件（不用 `sudo -u`） |
| 「私钥不存在：/app/data/ssh/id_ed25519」 | 密钥没生成或没放进 `/mbots/data/ssh/`；确认 `ls -l /mbots/data/ssh` 里属主是 `10001`。若报的是 `Permission denied (publickey)`，先查权限再看远端公钥——容器读不到私钥时也是这个表现 |
| 「远端未安装 docker compose / docker」 | 远端 `sudo -u mtbots docker compose version` 不过；装 CLI 或修 PATH |
| 「远端授权只允许 compose 操作」 | 守卫脚本拦下了这条命令：要么命令不在白名单（`docs/examples/mtbots-compose-guard.sh` 里补齐），要么远端没走守卫但命令拼错了 |
| 远端项目一个都看不到 | 检查 `roots` 白名单；在容器里手跑 `ssh … docker compose ls -a --format json` 看远端到底返回什么 |
| 中断了但远端还在跑 | 取消 = 断开 ssh（远端通常被 SIGHUP 带走，但**不保证**）；`pull`/`up -d` 幂等，重跑一次即可 |
| 想临时关掉某台 | 清单里给它加 `"enabled": false`，重建容器 |

### 10. 安全建议与回滚

* 远端用**专用用户 + `docker` 组**，不要 root、不要复用登录账号；
* **一定要加 `command=` 守卫**（第 4 节），这是 SSH 路线相对「暴露 docker 端口」最大的优势；
* 私钥只放 `data/`（已 gitignore）、`0600` + 属主 `10001`；生产建议 `strict=yes` 配预置 `known_hosts`；
* 权限仍然归 ACL：`docker` 模块默认只给 owner/admin，`upgrade`/`prune` 走两步确认；
* **回滚**：删掉 `/mbots/data/docker-hosts.json` → `docker compose up -d --force-recreate`，立刻回到单机；密钥留着不影响（以后想再加不用重新生成）。

相关文件：[`scripts/setup-remote-host.sh`](scripts/setup-remote-host.sh)（bot 侧向导，`make add-host`）、[`docs/examples/mtbots-remote-setup.sh`](docs/examples/mtbots-remote-setup.sh)（远端一次性准备）、[`docs/examples/mtbots-compose-guard.sh`](docs/examples/mtbots-compose-guard.sh)（守卫脚本）、[`docs/examples/docker-hosts.json`](docs/examples/docker-hosts.json)（清单样例）、[`docs/examples/authorized_keys.sample`](docs/examples/authorized_keys.sample)（authorized_keys 样例）、[`docs/docker-multi-host-design.md`](docs/docker-multi-host-design.md)（设计与落地清单）。

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
├── scripts/                                          # setup-remote-host.sh：一键接入远端主机（交互向导）
├── docs/                                             # 设计稿、施工契约（porting-contract）、合并报告
│   └── examples/                                     # 多主机：主机清单样例、远端守卫脚本、authorized_keys 样例
└── tests/                                            # 405 个 stdlib unittest 用例
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
7. **远端主机不给 bot 任何端口或 socket**：只放一把被 `authorized_keys` 强制命令收窄的 ssh key——
   守卫把这条 key 限定在「MTBots 会用到的那几条 docker 命令」上，即使 bot 主机被拿下也拿不到远端 shell（见「管理多台服务器」）。
8. **多主机回调只认配置里的 host id**：面板里的主机名来自 `data/docker-hosts.json`，伪造的 id 会被拒并记日志，绝不会拿去拼命令。

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
`subprocess` / `create_subprocess_exec` 换成抛异常的桩做反证）。当前 **405 个用例全绿**：

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `tests/test_core.py` | 70 | 文本分片（HTML 标签闭合）、`safe_html` 出口转义、`SafeBot` 解析失败降级、ACL 默认拒绝、存储原子写/0600/损坏分类、任务中心（运行中显示最后一行输出、终态不再翻转）与收尾卡片文案、跨模块入口按钮的取舍（启用/权限/排不下）、**命令菜单的作用域规则（私聊按权限裁剪 / 群取全量 / 空片段不下发）**、面板唯一与两步确认、菜单去重与作用域、配置兼容、日志脱敏（含 exc_info 的 traceback）、路由消歧与兜底救援 |
| `tests/test_docker_module.py` | 76 | 项目排序/分页、pull 噪音过滤、清理候选、回调载荷、模块装配、**扫描失败诊断（实测 socket GID、未挂载目录的公共挂载点、缺命令）**、执行消息收尾（成功即删、失败必留、结果回传）、失败尾部输出与进度键盘的中断入口、**多主机（主机清单校验 / ssh 包装与引号 / 逐主机扫描与提示 / 退出码映射 / 只认配置内的 host id）** |
| `tests/test_litepan_module.py` | 59 | slug 构建（拼音/限长/去重）、users.json 校验、发现解析与缓存、菜单预算、触发与回执 |
| `tests/test_setup_script.py` | 26 | 两个接入脚本：ssh/scp 打桩跑完整向导流程（主机清单幂等合并 / `command=` 守卫行 / create 模式驱动远端脚本 / dry-run 不落地 / 非法 id 被拒），远端准备脚本（dry-run、参数校验、真跑时守卫 0755 + authorized_keys 0600 + 幂等 + 别人的 key 不动）、curl 模式（项目根取当前目录、不读 stdin、按 `--ref` 下载配套脚本）、远端侧交互向导（账号/公钥/守卫三问 + `--pubkey-line`/`--guard-url`）、`bash <(curl …)` 形式（$0=/dev/fd/* 时项目根取当前目录）、公钥被截断时早报错、老 sshd（<7.2）自动改用长格式选项、公钥已装+守卫生效时不被误判成「没装公钥」（先用守卫放行的 `docker compose version` 探连通，再用 `printf $HOME` 探「是不是守卫态」——compose 能跑不等于 key 没被 command= 限制，两者都要判对，且守卫态绝不写远端） |
| `tests/test_cline_module.py` | 140 | 额度解析/渲染、Key 掩码与指纹、别名校验、存储读写与自愈、默认拒绝 |
| `tests/test_integration.py` | 34 | 三个真实模块一起装配、命令不重复、菜单合并、`--check` 离线可跑，**真 `telegram.Update` 走 PTB dispatcher 的端到端用例**（不重复执行、全角命令可救援、下线模块的按钮有反馈、点按钮原地改同一条面板、**所有面板文案都过一遍 Telegram HTML 合法性校验**），多主机装配用例（按主机分组、单主机无主机标题、伪造 host id 被拒且不执行、`/upgrade` 编号与面板一致、状态与清理按主机），以及**收尾只留一条消息**（批量升级不再推卡片、执行消息带 `delete_on_success`、收尾面板带跨模块入口、`🔙 返回列表` 回原页、失败抄尾部输出、进度面板可中断、最后一步取消判为取消） |

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
| **命令菜单丢了 / 群里只剩 `/start`、`/help`** | 已修：菜单往**会话作用域**（`BotCommandScopeChat`）下发时曾把 `chat_id` 当 `user_id` 查权限，群/频道的负 id 永远查不到，于是被下发成「只剩基础命令」；而 Telegram 一有会话作用域就**覆盖**默认作用域，群里看着就像菜单没了。现在私聊按本人权限裁剪、群/频道取该会话全量命令（谁点谁被 handler 拦）、空片段一律不下发。升到 **≥ 1.0.7** 后发一次 `/p_menu` 或重启即可重刷；若某个会话仍不对，把日志里 `命令菜单已更新：scope=...` 那几行贴出来。 |

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
* 远端主机只支持 **SSH 执行**（见「管理多台服务器」）；不做远端构建 / git 操作 / 日志查看 / `exec`，也不做跨主机迁移。
* 多主机**不并行**执行：全局仍是一把任务锁，一次只跑一个升级（面板只有一个进度面）。
* LitePan 命令菜单按会话差异化下发受 `MenuManager` 限制：目前是所有已授权会话共用一份片段（含 `refresh_<slug>`）。
* LitePan 的「自动发现」与「回执」还没拆成两个开关（旧版就是耦合的，行为未退化）。
