#!/bin/sh
# MTBots **远端**一次性准备脚本（在远端主机上以 root / sudo 运行）。
#
# 它把「远端这边要做的所有事」合成一条命令：
#   1) 需要的话创建专用用户，并加入 docker 组
#   2) 修好家目录与 ~/.ssh 的属主/权限（NAS 上家目录不在 /home 也能用）
#   3) 安装强制命令守卫 mtbots-compose-guard 到 /usr/local/bin
#   4) 把 MTBots 的公钥写进 authorized_keys，并改写成
#      command="<守卫>",restrict <公钥>   （幂等：同一把 key 只保留一行，改前自动备份）
#   5) 顺带验证：sudo -u <user> docker compose version
#
# 一行用法（**在远端主机上** root/sudo 跑；跑起来会**交互问你**，不需要记参数）：
#   sudo bash <(curl -fsSL https://raw.githubusercontent.com/MbAIGC/MTBots/v1.3.2/docs/examples/mtbots-remote-setup.sh)
#   （非 bash 的 sh：curl -fsSL <同一个 URL> | sudo sh —— 脚本读 /dev/tty，提问照样能答）
#
#   它会问：授权哪个账号 → 粘贴公钥（或给路径/URL）→ 装不装守卫（默认从 GitHub 拉）→ 守卫装哪。
#   每一问都有默认值，直接回车也行。
#
# 全参数（自动化/无终端时用，等价于上面那些回答）：
#   sudo sh mtbots-remote-setup.sh --user mtbots --pubkey-line 'ssh-ed25519 AAAA…' --guard-url … --yes
#
# 手动传文件（公钥/守卫已经在远端）：
#   sudo sh mtbots-remote-setup.sh --user mtbots --pubkey /tmp/mtbots.pub --guard /tmp/guard.sh
#
# 通用做法是让 bot 那边的向导自动调用它（它会自己把公钥/守卫送过来）：
#   docker compose exec mtbots sh /app/scripts/setup-remote-host.sh

set -eu

TARGET_USER=""
HOME_DIR=""
PUBKEY=""
GUARD=""
GUARD_DEST=/usr/local/bin
GUARD_NAME=mtbots-compose-guard
#: 本脚本自带的版本号（跟这次提交一致）：守卫默认按它从 GitHub 拉，所以不需要手打 URL
MTBOTS_REF=${MTBOTS_REF:-v1.3.2}
DEFAULT_GUARD_URL=https://raw.githubusercontent.com/MbAIGC/MTBots/$MTBOTS_REF/docs/examples/mtbots-compose-guard.sh
PUBKEY_LINE=""
PUBKEY_URL=""
GUARD_URL=""
DO_USERADD=1
DRY_RUN=0
ASSUME_YES=0
FORCE_ASK=0
DOCKER_GROUP=docker
USER_SET=0
PUBKEY_SET=0
GUARD_SET=0
GUARD_DEST_SET=0

usage() {
    cat <<'EOF'
用法: sudo sh mtbots-remote-setup.sh --pubkey FILE [选项]

不给任何选项时进入**交互向导**（推荐）：会问账号、公钥、是否装守卫、守卫装哪。

  --user NAME        要授权的远端账号（默认：当前登录用户）
  --pubkey FILE      MTBots 的公钥文件（.pub）
  --pubkey-line STR  直接给公钥内容（一行字符串；curl|sh 无终端时用这个）
  --pubkey-url URL   从 URL 拉公钥（例如你放在 Gist/网盘上的 id_ed25519.pub）
  --guard FILE       守卫脚本文件
  --guard-url URL    守卫脚本从 URL 拉（默认就是官方那份：<上面的 DEFAULT_GUARD_URL>）
  --guard-dest DIR   守卫安装目录（默认 /usr/local/bin）
  --no-guard         不装守卫（不推荐：这把 key 就等于远端 shell）
  --no-useradd       不建用户、不加组，只写 authorized_keys（账号已存在）
  --home DIR         指定家目录（默认按 getent 解析；NAS 上家目录不在 /home 时有用）
  --ref REF          拉守卫用的 git ref（默认 v1.3.2）
  -y, --yes          不再提问，全部用默认值/已给的值（自动化用）
  --ask              强制进入交互（没有终端时也能用，例如把答案用管道喂进来）
  --dry-run          只打印将要做什么
  -h, --help         显示本帮助
EOF
}

say()  { printf '%s\n' "$*"; }
warn() { printf '⚠️  %s\n' "$*" >&2; }
die()  { printf '❌ %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --user) TARGET_USER=${2:-}; USER_SET=1; shift 2 ;;
        --home) HOME_DIR=${2:-}; shift 2 ;;
        --pubkey) PUBKEY=${2:-}; PUBKEY_SET=1; shift 2 ;;
        --pubkey-line) PUBKEY_LINE=${2:-}; PUBKEY_SET=1; shift 2 ;;
        --pubkey-url) PUBKEY_URL=${2:-}; PUBKEY_SET=1; shift 2 ;;
        --guard) GUARD=${2:-}; GUARD_SET=1; shift 2 ;;
        --guard-url) GUARD_URL=${2:-}; GUARD_SET=1; shift 2 ;;
        --no-guard) GUARD_SET=2; shift ;;
        --guard-dest) GUARD_DEST=${2:-}; GUARD_DEST_SET=1; shift 2 ;;
        --ref) MTBOTS_REF=${2:-}; DEFAULT_GUARD_URL=https://raw.githubusercontent.com/MbAIGC/MTBots/$MTBOTS_REF/docs/examples/mtbots-compose-guard.sh; shift 2 ;;
        --no-useradd) DO_USERADD=0; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        --ask) FORCE_ASK=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
    esac
done

# ---------- 交互补齐 ----------
# 优先读 /dev/tty：`curl … | sudo sh` 时 stdin 是脚本正文，读 stdin 会把脚本吃光
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

INTERACTIVE=0
if [ "$ASSUME_YES" != 1 ]; then
    if [ "$FORCE_ASK" = 1 ]; then
        INTERACTIVE=1
    elif [ -r /dev/tty ] && ( : < /dev/tty ) 2>/dev/null; then
        INTERACTIVE=1
    elif [ -t 0 ]; then
        INTERACTIVE=1
    fi
fi

if [ "$INTERACTIVE" = 1 ]; then
    say "== MTBots 远端向导（直接回车 = 用默认值）=="
    if [ "$USER_SET" != 1 ]; then
        TARGET_USER=$(ask "要授权/创建的远端账号" "mtbots")
        USER_SET=1
    fi
    if [ "$PUBKEY_SET" != 1 ]; then
        say "把 MTBots 那台 ./data/ssh/id_ed25519.pub 的整行内容粘进来（也可以给文件路径或 http 地址）：" >&2
        _a=$(ask "公钥" "")
        case "$_a" in
            "") : ;;
            ssh-*|ecdsa-*|sk-*) PUBKEY_LINE=$_a; PUBKEY_SET=1 ;;
            http://*|https://*) PUBKEY_URL=$_a; PUBKEY_SET=1 ;;
            *) if [ -f "$_a" ]; then PUBKEY=$_a; PUBKEY_SET=1; else warn "既不像公钥、也不是存在的文件或 URL：$_a"; fi ;;
        esac
    fi
    if [ "$GUARD_SET" = 0 ]; then
        if ask_yes "装强制命令守卫（自动下载官方那份）" y; then
            GUARD_URL=$DEFAULT_GUARD_URL
            GUARD_SET=1
        else
            GUARD_SET=2
            warn "选择不装守卫：这把 key 就等于远端 shell 权限。"
        fi
    fi
    if [ "$GUARD_SET" = 1 ] && [ "$GUARD_DEST_SET" != 1 ]; then
        GUARD_DEST=$(ask "守卫安装目录" "$GUARD_DEST")
    fi
    say ""
fi

fetch_url() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1" -o "$2" || die "下载失败：$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$2" "$1" || die "下载失败：$1"
    else
        die "远端没有 curl/wget，拉不到 $1；改成 --pubkey/--guard 先把文件传过来"
    fi
    say "  已下载：$1"
}

# 公钥三种来源：文件 > 字符串 > URL
if [ -z "$PUBKEY" ] && [ -z "$PUBKEY_LINE" ] && [ -n "$PUBKEY_URL" ] && [ "$DRY_RUN" != 1 ]; then
    PUBKEY=${TMPDIR:-/tmp}/mtbots-pubkey.$$
    fetch_url "$PUBKEY_URL" "$PUBKEY"
fi
if [ -z "$PUBKEY" ] && [ -n "$PUBKEY_LINE" ]; then
    PUBKEY=${TMPDIR:-/tmp}/mtbots-pubkey-line.$$
    printf '%s\n' "$PUBKEY_LINE" > "$PUBKEY"
fi
[ -n "$PUBKEY" ] || die "得给一个公钥来源：--pubkey FILE / --pubkey-line 'ssh-ed25519 AAAA…' / --pubkey-url URL（不加参数直接跑，脚本会问你）"
[ -f "$PUBKEY" ] || die "公钥文件不存在：$PUBKEY"

# 守卫也可以从 URL 拉（dry-run 只说明，不联网）
GUARD_FROM_URL=0
if [ -z "$GUARD" ] && [ -n "$GUARD_URL" ]; then
    GUARD_FROM_URL=1
    if [ "$DRY_RUN" = 1 ]; then
        say "（dry-run）守卫将从 $GUARD_URL 下载到 $GUARD_DEST/$GUARD_NAME"
    else
        GUARD=${TMPDIR:-/tmp}/mtbots-guard.$$
        fetch_url "$GUARD_URL" "$GUARD"
    fi
fi

if [ "$DRY_RUN" != 1 ]; then
    [ "$(id -u)" = "0" ] || die "请用 root 运行（sudo sh $0 …）"
fi

if [ -z "$TARGET_USER" ]; then
    TARGET_USER=$(id -un)
fi
say "== MTBots 远端准备 =="
say "目标账号：$TARGET_USER$([ "$DO_USERADD" = 1 ] && echo "（必要时创建并加入 $DOCKER_GROUP 组）" || echo "（--no-useradd：只写公钥）")"

# ---------- 1. 用户与 docker 组 ----------
if [ "$DO_USERADD" = 1 ]; then
    if id -u "$TARGET_USER" >/dev/null 2>&1; then
        say "→ 用户已存在"
    else
        say "→ 创建用户 $TARGET_USER"
        [ "$DRY_RUN" = 1 ] || useradd -m -s /bin/bash "$TARGET_USER"
    fi
    if getent group "$DOCKER_GROUP" >/dev/null 2>&1; then
        if id -nG "$TARGET_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$DOCKER_GROUP"; then
            say "→ 已在 $DOCKER_GROUP 组"
        else
            say "→ 加入 $DOCKER_GROUP 组"
            [ "$DRY_RUN" = 1 ] || usermod -aG "$DOCKER_GROUP" "$TARGET_USER"
        fi
    else
        warn "远端没有 $DOCKER_GROUP 组（docker 可能装在 rootless/snap 里）：请手动确认「$TARGET_USER 能用 docker」"
    fi
fi

# ---------- 2. 家目录与 .ssh ----------
if [ -z "$HOME_DIR" ]; then
    HOME_DIR=$(getent passwd "$TARGET_USER" | cut -d: -f6)
fi
[ -n "$HOME_DIR" ] || die "拿不到 $TARGET_USER 的家目录（用 --home 指定）"
SSH_DIR=$HOME_DIR/.ssh
AK=$SSH_DIR/authorized_keys
say "家目录：$HOME_DIR"

if [ "$DRY_RUN" = 1 ]; then
    say "→ 将确保 $SSH_DIR 存在、属主 $TARGET_USER、权限 700"
else
    mkdir -p "$SSH_DIR"
    chown "$TARGET_USER":"$TARGET_USER" "$HOME_DIR" "$SSH_DIR" 2>/dev/null || chown "$TARGET_USER" "$HOME_DIR" "$SSH_DIR"
    chmod 700 "$SSH_DIR"
fi

# ---------- 3. 守卫脚本 ----------
GUARD_REMOTE=""
if [ -n "$GUARD" ] || [ "$GUARD_FROM_URL" = 1 ]; then
    if [ -n "$GUARD" ]; then
        [ -f "$GUARD" ] || die "守卫脚本不存在：$GUARD"
    fi
    GUARD_REMOTE=$GUARD_DEST/$GUARD_NAME
    say "→ 安装守卫：$GUARD_REMOTE"
    if [ "$DRY_RUN" = 1 ]; then
        say "   （dry-run）"
    else
        mkdir -p "$GUARD_DEST"
        cp "$GUARD" "$GUARD_REMOTE"
        chmod 755 "$GUARD_REMOTE"
        chown root:root "$GUARD_REMOTE" 2>/dev/null || true
    fi
else
    warn "没有 --guard/--guard-url：只装公钥、不装守卫。这把 key 等于远端 shell，建议补上守卫。"
fi

# ---------- 4. authorized_keys（幂等） ----------
KEY_TYPE=$(awk '{print $1}' "$PUBKEY")
KEY_BLOB=$(awk '{print $2}' "$PUBKEY")
[ -n "$KEY_TYPE" ] && [ -n "$KEY_BLOB" ] || die "公钥文件格式不对：$PUBKEY"
if [ -n "$GUARD_REMOTE" ]; then
    LINE="command=\"$GUARD_REMOTE\",restrict $KEY_TYPE $KEY_BLOB"
else
    LINE="$KEY_TYPE $KEY_BLOB"
fi

say "→ 写入 $AK"
if [ "$DRY_RUN" = 1 ]; then
    say "   将追加（并先删掉同一把 key 的旧行）："
    say "   $LINE"
else
    TMP=$(mktemp "${TMPDIR:-/tmp}/mtbots-ak.XXXXXX")
    if [ -f "$AK" ]; then
        grep -v -F "$KEY_BLOB" "$AK" > "$TMP" || : > "$TMP"
        cp "$AK" "$AK.bak.$(date +%s)"
    fi
    printf '%s\n' "$LINE" >> "$TMP"
    if [ -f "$AK" ] && cmp -s "$AK" "$TMP"; then
        say "   （内容无变化）"
        rm -f "$TMP"
    else
        cat "$TMP" > "$AK"
        rm -f "$TMP"
        say "   已更新（旧文件已备份为 $AK.bak.*）"
    fi
    chmod 600 "$AK"
    chown "$TARGET_USER":"$TARGET_USER" "$AK" 2>/dev/null || chown "$TARGET_USER" "$AK"
    say "   现在共 $(grep -c . "$AK" 2>/dev/null || echo 0) 行"
fi

# ---------- 5. 验证 ----------
if [ "$DRY_RUN" = 1 ]; then
    say "（dry-run：到此为止）"
    exit 0
fi

say ""
say "→ 验证「$TARGET_USER 能用 docker」..."
# 有 sudo 就以目标用户身份验证；没有 sudo（例如远端直接用 root 跑）就按当前身份验证
if command -v sudo >/dev/null 2>&1; then
    if sudo -u "$TARGET_USER" -H docker compose version >/dev/null 2>&1; then
        say "   ✅ $(sudo -u "$TARGET_USER" -H docker compose version 2>/dev/null | head -1)"
    else
        warn "没验证通过。可能是：docker 组刚加还没生效（让 $TARGET_USER 重新登录/新会话）、或该用户没有 docker 权限。"
        warn "手动确认：sudo -u $TARGET_USER -H docker compose version"
    fi
elif docker compose version >/dev/null 2>&1; then
    say "   ✅ $(docker compose version 2>/dev/null | head -1)（远端没有 sudo，按当前身份直接验证）"
else
    warn "没验证通过，且远端没有 sudo。手动确认：docker compose version"
fi

say ""
say "== 远端准备好了 =="
if [ -n "$GUARD_REMOTE" ]; then
    say "authorized_keys 里的行（守卫已启用）："
    say "  command=\"$GUARD_REMOTE\",restrict $KEY_TYPE $KEY_BLOB"
else
    say "authorized_keys 里的行（未加守卫）："
    say "  $KEY_TYPE $KEY_BLOB"
fi
say ""
say "接下来在 MTBots 那边：把主机写进 data/docker-hosts.json（用向导会自动写），"
say "然后 docker compose up -d --force-recreate。"
