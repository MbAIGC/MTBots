#!/bin/sh
# MTBots 远端接入向导（交互式）：一条命令把「远端主机」接进来。
#
# 远端那侧要做的所有事都收在一个脚本里（docs/examples/mtbots-remote-setup.sh）：
# 建专用用户 + 加 docker 组 + 修家目录/.ssh 权限 + 装守卫 + 写 authorized_keys。
# 本脚本负责驱动它，并完成 bot 这侧的事：
#
#   · 生成/复用 ssh 密钥（放项目根的 data/ssh/，权限与属主一次到位）
#   · 让远端的 mtbots-remote-setup.sh 以 sudo 跑完上面那串（有 TTY 时 sudo 密码就地输入）
#   · 合并写入 data/docker-hosts.json（按 id 幂等；已有清单不会被覆盖）
#   · 最后用同一套参数验证「ssh → 守卫 → docker compose version」整条链路
#
# 推荐在容器里跑（uid 10001，文件属主天然正确、ssh/scp 都在）：
#   docker compose exec mtbots sh /app/scripts/setup-remote-host.sh
# 也可以在项目根（宿主机）跑：
#   sh scripts/setup-remote-host.sh
#
# 非交互/可重复执行：
#   sh scripts/setup-remote-host.sh --mode create --host 10.0.0.5 --login-user root --user mtbots --id vps
#   sh scripts/setup-remote-host.sh --mode existing --host 10.0.0.5 --user admin --id nas2
#   sh scripts/setup-remote-host.sh --help
#
# 不想用脚本也行：README「管理多台服务器」章节保留了完整手动步骤，
# docs/examples/mtbots-remote-setup.sh 也可以单独在远端 sudo 跑。

# 也能直接 curl 下来跑（stdin 是脚本时，项目根 = 当前目录）：
#   cd /mbots && curl -fsSL https://raw.githubusercontent.com/MbAIGC/MTBots/v1.2.1/scripts/setup-remote-host.sh \
#     | sh -s -- --mode create --host 10.0.0.5 --login-user root --user mtbots --id vps
# 这种情况下守卫/远端脚本不在本地，脚本会按 --ref（默认取当前 MTBots 版本）从 GitHub 拉。

set -eu

CONTAINER_DATA=${MTBOTS_CONTAINER_DATA:-/app/data}
REPO=${MTBOTS_REPO:-MbAIGC/MTBots}
REF=""
PROJECT_ROOT_OPT=""

TARGET_HOST=""
SSH_PORT="22"
SSH_USER=""
LOGIN_USER=""
LOGIN_KEY=""
MODE=""
HOST_ID=""
HOST_LABEL=""
ROOTS=""
STRICT="accept-new"
USE_GUARD=1
ROOTS_SET=0
LABEL_SET=0
DRY_RUN=0
ASSUME_YES=0
DO_RESTART="ask"
WITH_LOCAL=1
GUARD_SRC=""
REMOTE_SETUP_SRC=""
GUARD_DEST=/usr/local/bin

usage() {
    cat <<'EOF'
用法: sh scripts/setup-remote-host.sh [选项]

不带选项 = 交互式向导，依次问：远端地址、端口、准备方式、账号、主机 id、显示名、路径白名单。

远端准备方式（--mode）:
  create    新建专用用户并加入 docker 组（需要能 sudo 的登录账号，推荐）
  existing  复用远端已有账号（需要它已经能用 docker；不改用户，只装公钥/守卫）

选项:
  --host HOST          远端 IP 或域名
  --port PORT          SSH 端口（默认 22）
  --user USER          最终要授权的远端账号（create=要创建的专用用户，existing=已有账号）
  --login-user USER    仅 create 模式：用于登录并 sudo 的账号（默认 root）
  --login-key FILE     仅 create 模式：登录账号的私钥（默认走你 ~/.ssh/agent）
  --id ID              主机 id（小写字母/数字/_/-，≤16；默认从 --host 推导）
  --label LABEL        面板显示名（默认 = id）
  --roots PATHS        只允许管理的路径前缀，逗号分隔（给空串表示不限，且不再提问）
  --strict MODE        known_hosts 策略：accept-new（默认）或 yes
  --guard-dest DIR     守卫安装目录（默认 /usr/local/bin）
  --no-guard           不装守卫（不推荐：等于给了一把能登远端 shell 的 key）
  --no-local           主机清单里不自动带上「本机」
  --restart            写完清单直接 docker compose up -d --force-recreate
  --no-restart         写完只打印重启命令
  --project-root DIR   项目根目录（默认取脚本所在仓库的上一级；curl|sh 时取当前目录）
  --repo OWNER/REPO    脚本来源仓库（默认 MbAIGC/MTBots，仅当需要下载配套脚本时用）
  --ref REF            下载用的 git ref（默认取当前 MTBots 版本，如 v1.2.1；取不到就 main）
  --data-dir DIR       项目根的 data 目录（默认 <项目根>/data）
  --guard FILE         守卫脚本路径（默认 <项目根>/docs/examples/mtbots-compose-guard.sh）
  --remote-setup FILE  远端准备脚本路径（默认 <项目根>/docs/examples/mtbots-remote-setup.sh）
  --yes                不再问「继续？/是否重启」（自动化用；重启仍需 --restart）
  --dry-run            只打印会做什么，不动任何文件、不连远端
  -h, --help           显示本帮助
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --host) TARGET_HOST=${2:-}; shift 2 ;;
        --port) SSH_PORT=${2:-}; shift 2 ;;
        --user) SSH_USER=${2:-}; shift 2 ;;
        --login-user) LOGIN_USER=${2:-}; shift 2 ;;
        --login-key) LOGIN_KEY=${2:-}; shift 2 ;;
        --mode) MODE=${2:-}; shift 2 ;;
        --id) HOST_ID=${2:-}; shift 2 ;;
        --label) HOST_LABEL=${2:-}; LABEL_SET=1; shift 2 ;;
        --roots) ROOTS=${2:-}; ROOTS_SET=1; shift 2 ;;
        --strict) STRICT=${2:-}; shift 2 ;;
        --guard-dest) GUARD_DEST=${2:-}; shift 2 ;;
        --guard) GUARD_SRC=${2:-}; shift 2 ;;
        --remote-setup) REMOTE_SETUP_SRC=${2:-}; shift 2 ;;
        --no-guard) USE_GUARD=0; shift ;;
        --no-local) WITH_LOCAL=0; shift ;;
        --restart) DO_RESTART=1; shift ;;
        --no-restart) DO_RESTART=0; shift ;;
        --project-root) PROJECT_ROOT_OPT=${2:-}; shift 2 ;;
        --repo) REPO=${2:-}; shift 2 ;;
        --ref) REF=${2:-}; shift 2 ;;
        --data-dir) DATA_DIR=${2:-}; shift 2 ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
    esac
done

# 自己是怎么被执行的：仓库里的文件，还是 `curl … | sh`（此时 stdin 是脚本本身）
case "$0" in
    ""|-|sh|dash|ash|bash|*/sh|*/dash|*/ash|*/bash) PIPED=1 ;;
    *) PIPED=0 ;;
esac
if [ "$PIPED" = 1 ] || [ ! -f "$0" ]; then
    PIPED=1
    SCRIPT_DIR=""
    DEFAULT_ROOT=$PWD
else
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    DEFAULT_ROOT=$(dirname -- "$SCRIPT_DIR")
fi
PROJECT_ROOT=${PROJECT_ROOT_OPT:-$DEFAULT_ROOT}

DATA_DIR=${DATA_DIR:-$PROJECT_ROOT/data}
SSH_DIR=$DATA_DIR/ssh
KEY=$SSH_DIR/id_ed25519
PUB=$KEY.pub
KNOWN_HOSTS=$SSH_DIR/known_hosts
HOSTS_FILE=$DATA_DIR/docker-hosts.json
STAGE_DIR=${TMPDIR:-/tmp}/mtbots-setup.$$

say()  { printf '%s\n' "$*"; }
warn() { printf '⚠️  %s\n' "$*" >&2; }
die()  { printf '❌ %s\n' "$*" >&2; exit 1; }

cleanup() { rm -rf "$STAGE_DIR"; }
trap cleanup EXIT INT TERM

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"; }

# 交互输入优先读 /dev/tty：`curl … | sh` 时 stdin 是脚本正文，读 stdin 会把脚本吃光。
# 注意不能在这里 `exec 9</dev/tty`——没有控制终端时 exec 的重定向失败会让脚本直接退出。
read_answer() {
    if [ -r /dev/tty ] && ( : < /dev/tty ) 2>/dev/null; then
        IFS= read -r _answer < /dev/tty || _answer=""
    else
        IFS= read -r _answer || _answer=""
    fi
    printf '%s' "$_answer"
}

ask() {
    _prompt=$1
    _default=${2:-}
    if [ -n "$_default" ]; then
        printf '%s [%s]: ' "$_prompt" "$_default" >&2
    else
        printf '%s: ' "$_prompt" >&2
    fi
    _answer=$(read_answer)
    [ -n "$_answer" ] || _answer=$_default
    printf '%s' "$_answer"
}

ask_yes() {
    _prompt=$1
    _default=${2:-y}
    printf '%s (y/n) [%s]: ' "$_prompt" "$_default" >&2
    _answer=$(read_answer)
    [ -n "$_answer" ] || _answer=$_default
    case "$_answer" in
        [Yy]*) return 0 ;;
        *) return 1 ;;
    esac
}

# 取「当前 MTBots 版本」当默认 ref：优先本地可导入的包（容器里就是它），否则退回 main
detect_ref() {
    if [ -n "$REF" ]; then
        printf '%s' "$REF"
        return
    fi
    if [ -n "${MTBOTS_REF:-}" ]; then
        printf '%s' "$MTBOTS_REF"
        return
    fi
    _v=$(python3 -c 'import mtbots; print(mtbots.__version__)' 2>/dev/null || true)
    if [ -n "$_v" ]; then
        printf 'v%s' "$_v"
        return
    fi
    printf 'main'
}

fetch_file() {
    _url=https://raw.githubusercontent.com/$REPO/$REF/$1
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$_url" -o "$2" || die "下载失败：$_url"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$2" "$_url" || die "下载失败：$_url"
    else
        die "没有 curl/wget，拉不到配套脚本 $1；请用 --guard / --remote-setup 指到本地文件"
    fi
    say "  已下载 $1（$REF）" >&2
}

# 配套脚本：优先用参数指定的本地文件；其次用脚本旁边的仓库文件；最后按 ref 下载
resolve_companion() {
    if [ -n "$2" ] && [ -f "$2" ]; then
        printf '%s' "$2"
        return
    fi
    if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/../$1" ]; then
        printf '%s' "$SCRIPT_DIR/../$1"
        return
    fi
    # 下到 fetched/ 子目录：create 模式还要把同名文件 cp 进 $STAGE_DIR 打包，
    # 直接下到 $STAGE_DIR 会撞名（cp 报 "are the same file"）
    mkdir -p "$STAGE_DIR/fetched"
    _dest=$STAGE_DIR/fetched/$(basename "$1")
    fetch_file "$1" "$_dest"
    printf '%s' "$_dest"
}

valid_id() { printf '%s' "$1" | grep -Eq '^[a-z0-9_-]{1,16}$'; }
valid_target() { printf '%s' "$1" | grep -Eq '^[A-Za-z0-9._-]+@[A-Za-z0-9._:-]+$'; }

# 用我们自己的密钥连远端（BatchMode：绝不交互）
ssh_run() {
    ssh -p "$SSH_PORT" -i "$KEY" \
        -o BatchMode=yes -o ConnectTimeout=8 \
        -o StrictHostKeyChecking="$STRICT" -o UserKnownHostsFile="$KNOWN_HOSTS" \
        "$TARGET" "$@"
}

# 用「登录账号」连远端做初始化：允许交互（sudo 密码、密码登录）
ssh_admin() {
    _remote_cmd=$1
    if [ -n "$LOGIN_KEY" ]; then
        set -- -i "$LOGIN_KEY"
    else
        set --
    fi
    if [ -t 0 ] && [ -t 1 ]; then
        ssh -t -p "$SSH_PORT" -o ConnectTimeout=10 \
            -o StrictHostKeyChecking="$STRICT" -o UserKnownHostsFile="$KNOWN_HOSTS" \
            "$@" "$LOGIN_TARGET" "$_remote_cmd"
    else
        ssh -p "$SSH_PORT" -o ConnectTimeout=10 -o BatchMode=yes \
            -o StrictHostKeyChecking="$STRICT" -o UserKnownHostsFile="$KNOWN_HOSTS" \
            "$@" "$LOGIN_TARGET" "$_remote_cmd"
    fi
}

# 同 ssh_admin，但本机 stdin 作为远端命令的 stdin（用于 tar 传输）
ssh_admin_pipe() {
    _remote_cmd=$1
    if [ -n "$LOGIN_KEY" ]; then
        set -- -i "$LOGIN_KEY"
    else
        set --
    fi
    if [ -t 1 ]; then
        ssh -T -p "$SSH_PORT" -o ConnectTimeout=10 \
            -o StrictHostKeyChecking="$STRICT" -o UserKnownHostsFile="$KNOWN_HOSTS" \
            "$@" "$LOGIN_TARGET" "$_remote_cmd"
    else
        ssh -p "$SSH_PORT" -o ConnectTimeout=10 -o BatchMode=yes \
            -o StrictHostKeyChecking="$STRICT" -o UserKnownHostsFile="$KNOWN_HOSTS" \
            "$@" "$LOGIN_TARGET" "$_remote_cmd"
    fi
}

scp_run() {
    scp -P "$SSH_PORT" -i "$KEY" \
        -o BatchMode=yes -o ConnectTimeout=8 \
        -o StrictHostKeyChecking="$STRICT" -o UserKnownHostsFile="$KNOWN_HOSTS" \
        "$@"
}

# ==================== 1. 收集参数 ====================
say "== MTBots 远端接入向导 =="
if [ "$PIPED" = 1 ]; then
    say "（curl 模式：项目根取当前目录，配套脚本按需下载）"
fi
say "项目根：$PROJECT_ROOT"
if [ ! -f "$PROJECT_ROOT/docker-compose.yml" ]; then
    warn "$PROJECT_ROOT 里没看到 docker-compose.yml —— 确认这是 MTBots 项目根吗？（可用 --project-root 指定）"
fi
say ""

if [ -z "$TARGET_HOST" ]; then
    TARGET_HOST=$(ask "远端 IP 或域名" "")
fi
[ -n "$TARGET_HOST" ] || die "远端地址不能为空"

if [ -z "$MODE" ]; then
    say "远端准备方式：" >&2
    say "  1) 复用远端已有账号（它已经能用 docker；不改用户，只装公钥+守卫）" >&2
    say "  2) 新建专用用户（更干净；需要能 sudo 的登录账号）" >&2
    _mode=$(ask "选哪个" "1")
    case "$_mode" in
        2|create|create-user|new) MODE=create ;;
        *) MODE=existing ;;
    esac
fi
case "$MODE" in
    create|1) MODE=create ;;
    existing|reuse|2) MODE=existing ;;
    *) die "--mode 只能是 create 或 existing" ;;
esac

if [ "$MODE" = "create" ]; then
    if [ -z "$SSH_USER" ]; then
        SSH_USER=$(ask "要创建/授权的专用用户名" "mtbots")
    fi
    if [ -z "$LOGIN_USER" ]; then
        LOGIN_USER=$(ask "用哪个账号登录远端做初始化（需要 sudo）" "root")
    fi
    LOGIN_TARGET="$LOGIN_USER@$TARGET_HOST"
else
    if [ -z "$SSH_USER" ]; then
        SSH_USER=$(ask "远端已有账号（要能用 docker）" "$(id -un 2>/dev/null || echo root)")
    fi
fi
[ -n "$SSH_USER" ] || die "远端账号不能为空"

if [ -z "$HOST_ID" ]; then
    _guess=$(printf '%s' "$TARGET_HOST" | tr 'A-Z' 'a-z' | sed 's/[^a-z0-9_-]/-/g; s/^-\{1,\}//' | cut -c1-16)
    HOST_ID=$(ask "主机 id（面板/回调里用它）" "$_guess")
fi
valid_id "$HOST_ID" || die "主机 id 只能用 [a-z0-9_-]，长度 1-16：$HOST_ID"

if [ "$LABEL_SET" != 1 ] && [ -z "$HOST_LABEL" ]; then
    HOST_LABEL=$(ask "面板显示名" "$HOST_ID")
fi
[ -n "$HOST_LABEL" ] || HOST_LABEL=$HOST_ID
if [ "$ROOTS_SET" != 1 ]; then
    ROOTS=$(ask "只允许管理的路径前缀（逗号分隔，留空=不限）" "")
fi

TARGET="$SSH_USER@$TARGET_HOST"
valid_target "$TARGET" || die "目标格式不对：$TARGET"

case "$STRICT" in
    yes|accept-new) ;;
    *) die "--strict 只能是 yes 或 accept-new" ;;
esac

say ""
say "将执行："
if [ "$MODE" = "create" ]; then
    say "  · 远端：用 $LOGIN_TARGET（sudo）准备用户 $SSH_USER（建用户 + docker 组 + 家目录权限 + 装守卫 + 写公钥）"
else
    say "  · 远端：把公钥装到 $TARGET（已存在账号），守卫装到 $([ "$USE_GUARD" = 1 ] && echo "$GUARD_DEST 或 ~/.local/bin" || echo "（--no-guard 跳过）")"
fi
say "  · 密钥：$KEY（不存在则生成，属主交给容器用户 10001）"
if [ "$USE_GUARD" = 1 ]; then
    say "  · 守卫脚本：${GUARD_SRC:-$PROJECT_ROOT/docs/examples/mtbots-compose-guard.sh}（本地没有就按 ref 下载）"
fi
say "  · 清单：$HOSTS_FILE（id=$HOST_ID，label=$HOST_LABEL$([ -n "$ROOTS" ] && echo "，roots=$ROOTS")）"
say "  · known_hosts 策略：$STRICT"
if [ "$DRY_RUN" = 1 ]; then
    say ""
    say "（--dry-run：到此为止，什么都没做）"
    exit 0
fi
if [ "$ASSUME_YES" != 1 ]; then
    if ! ask_yes "继续？" y; then
        say "已取消。"
        exit 0
    fi
fi

need_cmd ssh
need_cmd scp
REF=$(detect_ref)
if [ "$USE_GUARD" = 1 ]; then
    GUARD_SRC=$(resolve_companion docs/examples/mtbots-compose-guard.sh "$GUARD_SRC")
    [ -f "$GUARD_SRC" ] || die "找不到守卫脚本：$GUARD_SRC（用 --guard 指定，或 --no-guard 跳过）"
fi
if [ "$MODE" = "create" ]; then
    REMOTE_SETUP_SRC=$(resolve_companion docs/examples/mtbots-remote-setup.sh "$REMOTE_SETUP_SRC")
    [ -f "$REMOTE_SETUP_SRC" ] || die "找不到远端准备脚本：$REMOTE_SETUP_SRC（用 --remote-setup 指定）"
    need_cmd tar
fi

# ==================== 2. 本地密钥 ====================
mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"
if [ ! -f "$KEY" ]; then
    need_cmd ssh-keygen
    say "→ 生成密钥：$KEY"
    ssh-keygen -t ed25519 -N '' -C mtbots@bot -f "$KEY" >/dev/null
else
    say "→ 复用已有密钥：$KEY"
fi
chmod 600 "$KEY"
[ -f "$PUB" ] || ssh-keygen -y -f "$KEY" > "$PUB"

_uid=$(id -u)
if [ "$_uid" = "10001" ]; then
    say "→ 当前就是容器用户（uid 10001），属主无需处理"
elif [ "$_uid" = "0" ]; then
    chown -R 10001:10001 "$SSH_DIR" 2>/dev/null || warn "chown 失败，稍后请手动处理"
    say "→ 已把 $SSH_DIR 属主设为 10001:10001"
elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sudo -n chown -R 10001:10001 "$SSH_DIR" 2>/dev/null || warn "sudo chown 失败"
    say "→ 已用 sudo 把 $SSH_DIR 属主设为 10001:10001"
else
    warn "当前用户不是 root 也不是 uid 10001，且没有免密 sudo。"
    warn "容器里的 mtbots 用户（uid 10001）必须能读私钥，请执行：sudo chown -R 10001:10001 $SSH_DIR"
fi

# ==================== 3. 远端准备 ====================
if [ "$MODE" = "create" ]; then
    say ""
    say "→ 远端准备（$LOGIN_TARGET 上用 sudo 跑 mtbots-remote-setup.sh）..."
    mkdir -p "$STAGE_DIR"
    cp "$PUB" "$STAGE_DIR/id_ed25519.pub"
    # shellcheck disable=SC2086  # 这里就是要把文件名列表拆成多个参数
    _bundle="id_ed25519.pub"
    if [ "$USE_GUARD" = 1 ]; then
        cp "$GUARD_SRC" "$STAGE_DIR/mtbots-compose-guard.sh"
        _bundle="$_bundle mtbots-compose-guard.sh"
    fi
    cp "$REMOTE_SETUP_SRC" "$STAGE_DIR/mtbots-remote-setup.sh"
    _bundle="$_bundle mtbots-remote-setup.sh"
    ( cd "$STAGE_DIR" && tar czf bundle.tgz $_bundle )
    say "  上传脚本与公钥 ..."
    ssh_admin_pipe 'rm -rf /tmp/mtbots-setup && mkdir -p /tmp/mtbots-setup && tar xzf - -C /tmp/mtbots-setup && chmod 755 /tmp/mtbots-setup/mtbots-remote-setup.sh' < "$STAGE_DIR/bundle.tgz" \
        || die "上传失败（检查 $LOGIN_TARGET 能不能登录，或换 --login-key）"

    SETUP_ARGS="--user '$SSH_USER' --pubkey /tmp/mtbots-setup/id_ed25519.pub --guard-dest '$GUARD_DEST'"
    if [ "$USE_GUARD" = 1 ]; then
        SETUP_ARGS="$SETUP_ARGS --guard /tmp/mtbots-setup/mtbots-compose-guard.sh"
    fi
    say "  执行远端准备（sudo 可能会要密码）..."
    ssh_admin "sudo sh /tmp/mtbots-setup/mtbots-remote-setup.sh $SETUP_ARGS" \
        || die "远端准备失败（看上面的输出；也可手动在远端 sudo sh /tmp/mtbots-setup/mtbots-remote-setup.sh …）"

    say "  验证我们的密钥能登录 $TARGET ..."
    ssh_run true >/dev/null 2>&1 || die "准备完了但我们的密钥登不上：检查远端 sshd 的 AllowUsers/PermitRootLogin/防火墙"
    say "  ✅ 密钥可用"
    GUARD_REMOTE=""
    if [ "$USE_GUARD" = 1 ]; then
        GUARD_REMOTE=$GUARD_DEST/mtbots-compose-guard
    fi
else
    say ""
    say "→ 测试登录 $TARGET ..."
    if ssh_run true >/dev/null 2>&1; then
        say "  密钥已被远端接受（重复执行时会走到这里）"
    else
        warn "用密钥登录失败，尝试用 ssh-copy-id 装公钥（会提示输入远端密码）"
        if command -v ssh-copy-id >/dev/null 2>&1; then
            if ! ssh-copy-id -i "$PUB" -p "$SSH_PORT" \
                    -o StrictHostKeyChecking="$STRICT" -o UserKnownHostsFile="$KNOWN_HOSTS" \
                    "$TARGET"; then
                die "ssh-copy-id 失败。可手动把下面这行追加到远端 ~/.ssh/authorized_keys：
  $(cat "$PUB")"
            fi
        else
            die "没有 ssh-copy-id。请手动把下面这行追加到远端 ~/.ssh/authorized_keys 后重跑：
  $(cat "$PUB")"
        fi
        ssh_run true >/dev/null 2>&1 || die "装了公钥还是连不上，请检查远端 sshd 配置"
        say "  公钥已装上"
    fi

    REMOTE_HOME=$(ssh_run 'printf %s "$HOME"')
    [ -n "$REMOTE_HOME" ] || die "拿不到远端 HOME"
    say "  远端家目录：$REMOTE_HOME"

    GUARD_REMOTE=""
    if [ "$USE_GUARD" = 1 ]; then
        say "→ 安装守卫脚本 ..."
        mkdir -p "$STAGE_DIR"
        scp_run "$GUARD_SRC" "$TARGET:/tmp/mtbots-compose-guard.sh" >/dev/null
        if ssh_run "sudo -n install -m 755 /tmp/mtbots-compose-guard.sh '$GUARD_DEST/mtbots-compose-guard'" >/dev/null 2>&1; then
            GUARD_REMOTE=$GUARD_DEST/mtbots-compose-guard
        else
            ssh_run "mkdir -p '$REMOTE_HOME/.local/bin' && cp /tmp/mtbots-compose-guard.sh '$REMOTE_HOME/.local/bin/mtbots-compose-guard' && chmod 755 '$REMOTE_HOME/.local/bin/mtbots-compose-guard'"
            GUARD_REMOTE=$REMOTE_HOME/.local/bin/mtbots-compose-guard
        fi
        say "  已安装：$GUARD_REMOTE"
    fi

    # ---- 改写 authorized_keys（幂等，按公钥 blob 去重）----
    say "→ 更新 authorized_keys ..."
    mkdir -p "$STAGE_DIR"
    PUB_BLOB=$(awk '{print $2}' "$PUB")
    if [ -n "$GUARD_REMOTE" ]; then
        printf 'command="%s",restrict %s %s\n' "$GUARD_REMOTE" "$(awk '{print $1}' "$PUB")" "$PUB_BLOB" > "$STAGE_DIR/ak-line"
    else
        printf '%s %s mtbots@bot\n' "$(awk '{print $1}' "$PUB")" "$PUB_BLOB" > "$STAGE_DIR/ak-line"
    fi
    printf '%s' "$PUB_BLOB" > "$STAGE_DIR/ak-blob"
    cat > "$STAGE_DIR/apply-ak.sh" <<'APPLY'
#!/bin/sh
set -eu
H=$1; LINE_FILE=$2; BLOB_FILE=$3
F="$H/.ssh/authorized_keys"
mkdir -p "$H/.ssh"
chmod 700 "$H/.ssh"
[ -f "$F" ] || : > "$F"
BLOB=$(cat "$BLOB_FILE")
TMP="$F.mtbots.tmp"
if [ -n "$BLOB" ]; then
    grep -v -F "$BLOB" "$F" > "$TMP" || : > "$TMP"
else
    cp "$F" "$TMP"
fi
cat "$LINE_FILE" >> "$TMP"
if cmp -s "$F" "$TMP" 2>/dev/null; then
    rm -f "$TMP"
    echo "unchanged"
    exit 0
fi
cp "$F" "$F.bak.$(date +%s)" 2>/dev/null || true
mv "$TMP" "$F"
chmod 600 "$F"
echo "updated"
APPLY
    scp_run "$STAGE_DIR/ak-line" "$TARGET:/tmp/mtbots-ak-line" >/dev/null
    scp_run "$STAGE_DIR/ak-blob" "$TARGET:/tmp/mtbots-ak-blob" >/dev/null
    scp_run "$STAGE_DIR/apply-ak.sh" "$TARGET:/tmp/mtbots-apply-ak.sh" >/dev/null
    AK_RESULT=$(ssh_run "sh /tmp/mtbots-apply-ak.sh '$REMOTE_HOME' /tmp/mtbots-ak-line /tmp/mtbots-ak-blob")
    say "  authorized_keys：$AK_RESULT"
    ssh_run 'rm -f /tmp/mtbots-ak-line /tmp/mtbots-ak-blob /tmp/mtbots-apply-ak.sh /tmp/mtbots-compose-guard.sh' >/dev/null 2>&1 || true
fi

# ==================== 4. 验证链路 ====================
say ""
say "→ 验证链路（ssh → 守卫 → docker compose version）..."
VERIFY=$(ssh_run 'docker compose version' 2>&1) || {
    warn "验证失败，远端输出："
    printf '%s\n' "$VERIFY" >&2
    case "$VERIFY" in
        *"command not allowed"*) die "守卫脚本拦下了这条命令：检查 ${GUARD_REMOTE:-守卫} 是否存在且可执行" ;;
        *"not found"*|*"No such file"*) die "远端没有 docker compose（先在远端 sudo -u $SSH_USER -H docker compose version 确认）" ;;
        *) die "远端的 docker 调用失败（见上面的输出）" ;;
    esac
}
say "  $VERIFY"

# ==================== 5. 写主机清单 ====================
say ""
say "→ 写入主机清单 $HOSTS_FILE ..."
if command -v python3 >/dev/null 2>&1; then
    MTBOTS_MERGE_ID=$HOST_ID \
    MTBOTS_MERGE_LABEL=$HOST_LABEL \
    MTBOTS_MERGE_TARGET=$TARGET \
    MTBOTS_MERGE_PORT=$SSH_PORT \
    MTBOTS_MERGE_ROOTS=$ROOTS \
    MTBOTS_MERGE_STRICT=$STRICT \
    MTBOTS_MERGE_WITH_LOCAL=$WITH_LOCAL \
    MTBOTS_MERGE_IDENTITY=$CONTAINER_DATA/ssh/id_ed25519 \
    MTBOTS_MERGE_KNOWN_HOSTS=$CONTAINER_DATA/ssh/known_hosts \
    python3 - "$HOSTS_FILE" <<'PY'
import json, os, sys

path = sys.argv[1]
entry = {
    "id": os.environ["MTBOTS_MERGE_ID"],
    "label": os.environ["MTBOTS_MERGE_LABEL"],
    "kind": "ssh",
    "target": os.environ["MTBOTS_MERGE_TARGET"],
    "port": int(os.environ["MTBOTS_MERGE_PORT"]),
    "identity": os.environ["MTBOTS_MERGE_IDENTITY"],
    "known_hosts": os.environ["MTBOTS_MERGE_KNOWN_HOSTS"],
    "strict": os.environ["MTBOTS_MERGE_STRICT"],
}
roots = [r.strip() for r in os.environ.get("MTBOTS_MERGE_ROOTS", "").split(",") if r.strip()]
if roots:
    entry["roots"] = roots

try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except FileNotFoundError:
    data = {"hosts": []}
except Exception as exc:
    sys.exit("现有清单读不动（%s）：先手工备份，改好 JSON 再重跑本脚本" % exc)

if isinstance(data, list):
    data = {"hosts": data}
hosts = data.get("hosts")
if not isinstance(hosts, list):
    sys.exit("现有清单里没有 hosts 列表：先手工修好再重跑")

if os.environ.get("MTBOTS_MERGE_WITH_LOCAL") == "1" and not any(
    isinstance(h, dict) and h.get("id") == "local" for h in hosts
):
    hosts.insert(0, {"id": "local", "label": "本机", "kind": "local"})

for index, host in enumerate(hosts):
    if isinstance(host, dict) and host.get("id") == entry["id"]:
        hosts[index] = entry
        break
else:
    hosts.append(entry)

data["hosts"] = hosts
dirname = os.path.dirname(os.path.abspath(path))
os.makedirs(dirname, exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
os.replace(tmp, path)
print("  已写入 %s：%d 台主机（%s）" % (path, len(hosts), ", ".join(str(h.get("id")) for h in hosts)))
PY
else
    warn "这台机器没有 python3，无法安全合并 JSON。请把下面这段手工并进 $HOSTS_FILE："
    cat <<JSON
{
  "id": "$HOST_ID",
  "label": "$HOST_LABEL",
  "kind": "ssh",
  "target": "$TARGET",
  "port": $SSH_PORT,
  "identity": "$CONTAINER_DATA/ssh/id_ed25519",
  "known_hosts": "$CONTAINER_DATA/ssh/known_hosts",
  "strict": "$STRICT"$([ -n "$ROOTS" ] && printf ',\n  "roots": ["%s"]' "$(printf '%s' "$ROOTS" | sed 's/,/", "/g')")
}
JSON
fi

# ==================== 6. 重启 ====================
say ""
say "== 完成 =="
say "下一步："
say "  1) 让清单生效：cd $PROJECT_ROOT && docker compose up -d --force-recreate"
say "  2) 逐主机自检：docker compose exec mtbots python -m mtbots --health | grep 🐳"
say "  3) Telegram 里发 /d_list，应能看到主机「$HOST_LABEL」下的项目"
say ""
say "回滚：删掉 $HOSTS_FILE 里的 \"$HOST_ID\" 这一条（或整个文件）再重建容器即可。"

if [ "$DO_RESTART" = "ask" ]; then
    if [ "$ASSUME_YES" = 1 ]; then
        DO_RESTART=0
    elif command -v docker >/dev/null 2>&1 && ask_yes "现在就在 $PROJECT_ROOT 执行 docker compose up -d --force-recreate？" y; then
        DO_RESTART=1
    else
        DO_RESTART=0
    fi
fi
if [ "$DO_RESTART" = 1 ]; then
    ( cd "$PROJECT_ROOT" && docker compose up -d --force-recreate )
fi
