"""全局配置：一个 token、一份 data/、三个模块的开关与权限。

兼容三家旧配置的**全部环境变量名**，所以旧 `.env` 可以直接搬过来：

    Telegram token : MTBOTS_BOT_TOKEN > TELEGRAM_BOT_TOKEN > BOT_TOKEN > TG_BOT_TOKEN
    白名单         : ALLOWED_USER_IDS ∪ TG_ALLOWED_IDS（默认拒绝）
    数据目录       : DATA_DIR（默认 data/，含 0600 的配置与日志）
    Cline 存储     : CONFIG_FILE（默认 data/config.json）
    LitePan 用户   : LITEPAN_USERS_FILE > USERS_FILE（默认 data/litepan-users.json）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

from .acl import ACL, DEFAULT_MODULE_ROLES, normalize_module

DEFAULT_MODULES = ("docker", "litepan", "cline")
DEFAULT_TG_API_BASE = "https://api.telegram.org"

TOKEN_VARS = ("MTBOTS_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TG_BOT_TOKEN")

_ENV_HELP = {
    "MTBOTS_BOT_TOKEN": "Telegram Bot Token（也接受 TELEGRAM_BOT_TOKEN / BOT_TOKEN / TG_BOT_TOKEN）",
    "ALLOWED_USER_IDS": "允许使用的 Telegram 用户 ID（逗号分隔；也接受 TG_ALLOWED_IDS）",
    "DATA_DIR": "数据目录（默认 data/）",
    "CONFIG_FILE": "Cline 模块的 Key 存储路径（默认 〈DATA_DIR〉/config.json）",
    "LITEPAN_USERS_FILE": "LitePan 多用户配置（默认 〈DATA_DIR〉/litepan-users.json）",
    "MTBOTS_MODULES": "启用的模块，逗号分隔（默认 docker,litepan,cline）",
    "MTBOTS_ROLES": "角色覆盖，如 123:owner,456:admin（默认白名单内全部 owner）",
}


def _env(env: Mapping[str, str], name: str, default: str = "") -> str:
    value = env.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(env: Mapping[str, str], name: str, default: int, low: int = 0, high: int = 10**9) -> int:
    raw = _env(env, name)
    if not raw:
        return default
    try:
        value = int(float(raw))
    except ValueError:
        return default
    return max(low, min(high, value))


def _env_bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = _env(env, name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_ids(env: Mapping[str, str], *names: str) -> frozenset[int]:
    ids: set[int] = set()
    for name in names:
        for part in _env(env, name).replace("，", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                continue
    return frozenset(ids)


def _env_roles(env: Mapping[str, str], name: str = "MTBOTS_ROLES") -> dict[int, str]:
    roles: dict[int, str] = {}
    for part in _env(env, name).replace("，", ",").split(","):
        if not part.strip() or ":" not in part:
            continue
        uid, _, role = part.partition(":")
        try:
            roles[int(uid.strip())] = role.strip().lower()
        except ValueError:
            continue
    return roles


@dataclass
class Settings:
    """运行期配置（非法值回退默认，绝不崩）。"""

    bot_token: str = ""
    allowed_user_ids: frozenset[int] = frozenset()
    data_dir: Path = Path("data")
    config_file: Path = Path("data/config.json")
    users_file: Path = Path("data/litepan-users.json")
    log_dir: Path = Path("data/logs")
    page_size: int = 6
    log_level: str = "INFO"
    demo_mode: bool = False
    show_identity: bool = False
    tg_api_base: str = DEFAULT_TG_API_BASE
    modules_enabled: tuple[str, ...] = DEFAULT_MODULES
    roles: dict[int, str] = field(default_factory=dict)
    module_roles: dict[str, set[str]] = field(
        default_factory=lambda: {k: set(v) for k, v in DEFAULT_MODULE_ROLES.items()}
    )
    default_role: str = "owner"
    default_deny: bool = True

    # ---------- 构造 ----------
    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env

        token = ""
        for name in TOKEN_VARS:
            token = _env(env, name)
            if token:
                break

        data_dir = Path(_env(env, "DATA_DIR", "data"))
        config_file = Path(_env(env, "CONFIG_FILE", str(data_dir / "config.json")))
        users_file = Path(
            _env(env, "LITEPAN_USERS_FILE", _env(env, "USERS_FILE", str(data_dir / "litepan-users.json")))
        )
        log_dir = Path(_env(env, "LOG_DIR", str(data_dir / "logs")))

        raw_modules = _env(env, "MTBOTS_MODULES", ",".join(DEFAULT_MODULES))
        enabled: list[str] = []
        for part in raw_modules.replace("，", ",").split(","):
            mod = normalize_module(part)
            if mod and mod not in enabled:
                enabled.append(mod)
        if not enabled:
            enabled = list(DEFAULT_MODULES)

        return cls(
            bot_token=token,
            allowed_user_ids=_env_ids(env, "ALLOWED_USER_IDS", "TG_ALLOWED_IDS"),
            data_dir=data_dir,
            config_file=config_file,
            users_file=users_file,
            log_dir=log_dir,
            page_size=_env_int(env, "PAGE_SIZE", 6, low=1, high=50),
            log_level=_env(env, "LOG_LEVEL", "INFO").upper() or "INFO",
            demo_mode=_env_bool(env, "DEMO_MODE", False),
            show_identity=_env_bool(env, "SHOW_IDENTITY", False),
            tg_api_base=_env(env, "TG_API_BASE", DEFAULT_TG_API_BASE).rstrip("/") or DEFAULT_TG_API_BASE,
            modules_enabled=tuple(enabled),
            roles=_env_roles(env),
            default_role=_env(env, "MTBOTS_DEFAULT_ROLE", "owner").lower() or "owner",
            default_deny=not _env_bool(env, "MTBOTS_ALLOW_UNLISTED", False),
        )

    # ---------- 派生 ----------
    def build_acl(self) -> ACL:
        return ACL(
            self.allowed_user_ids,
            roles=self.roles,
            module_roles=self.module_roles,
            default_role=self.default_role,
            default_deny=self.default_deny,
        )

    def module_enabled(self, module_id: str) -> bool:
        return normalize_module(module_id) in self.modules_enabled

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.log_dir):
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    def custom_api_base(self) -> Optional[str]:
        """配置了自建 TG API 镜像时返回 base_url，交给 PTB 的 base_url 使用。"""
        base = (self.tg_api_base or "").rstrip("/")
        if not base or base == DEFAULT_TG_API_BASE:
            return None
        return base

    def token_tail(self) -> str:
        return ("…%s" % self.bot_token[-4:]) if len(self.bot_token) > 8 else ""

    # ---------- 自检 ----------
    def problems(self) -> list[str]:
        issues: list[str] = []
        if not self.bot_token:
            issues.append("未配置 Bot Token（%s 任一）" % " / ".join(TOKEN_VARS))
        if not self.allowed_user_ids:
            issues.append(
                "白名单为空：默认拒绝，任何人都无法使用（设置 ALLOWED_USER_IDS，用 /id 查询自己的 ID）"
            )
        for module in self.modules_enabled:
            if module not in DEFAULT_MODULES:
                issues.append("未知模块 %r（可选：%s）" % (module, "、".join(DEFAULT_MODULES)))
        return issues

    def check_summary(self) -> str:
        lines = ["🧾 MTBots 配置自检", "─" * 28]
        lines.append("Token      : %s" % ("已配置 ***%s" % self.token_tail() if self.bot_token else "❌ 缺失"))
        lines.append(
            "白名单     : %s" % (
                "、".join(str(u) for u in sorted(self.allowed_user_ids)) or "❌ 空（默认拒绝）"
            )
        )
        lines.append("模块       : %s" % "、".join(self.modules_enabled))
        lines.append("数据目录   : %s" % self.data_dir)
        lines.append("Cline 存储 : %s" % self.config_file)
        lines.append("LitePan    : %s" % self.users_file)
        lines.append("日志       : %s" % self.log_dir)
        lines.append("TG API     : %s" % self.tg_api_base)
        issues = self.problems()
        lines.append("─" * 28)
        if issues:
            lines.extend("⚠️ %s" % issue for issue in issues)
        else:
            lines.append("✅ 基础配置完整")
        return "\n".join(lines)


__all__ = ["Settings", "DEFAULT_MODULES", "DEFAULT_TG_API_BASE", "TOKEN_VARS", "_ENV_HELP"]
