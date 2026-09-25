"""LitePan 模块配置：`users.json` 多会话绑定 + `LITEPAN_*` 单用户 env 兜底。

字段名、环境变量名、校验语义全部照搬原 `tgbot.py`（见 `porting-contract.md` §7.2）：
旧 `users.json` 与旧 `.env` 可以直接搬过来用。合并后 `Config` 改名为 `LitePanConfig`，
并接受 `mtbots.config.Settings` 作为默认 `users_file` 来源；**不再**因为配置缺失就抛异常
（合并进程中另外两个模块还要继续跑），问题记录在 `LitePanConfig.error` 里，由 `/id`、日志提示。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

log = logging.getLogger("mtbots.litepan.config")


class ConfigError(Exception):
    """LitePan 配置非法（users.json 条目 / 环境变量）。"""


# ==================== env 工具（原样照搬，改成显式 env 映射） ====================
def _env(env: Mapping[str, str], name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    val = env.get(name)
    if val is None or str(val).strip() == "":
        if required:
            raise ConfigError("缺少环境变量 %s" % name)
        return default
    return str(val).strip()


def _parse_int(value: Any, name: str, minimum: Optional[int] = None) -> int:
    """解析配置里的整数；非法值或低于下限时抛 ConfigError（原 `_parse_int`）。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ConfigError("%s 必须是整数，当前值: %r" % (name, value))
    if minimum is not None and value < minimum:
        raise ConfigError("%s 必须 >= %d，当前值: %d" % (name, minimum, value))
    return value


def _env_int(env: Mapping[str, str], name: str, default: int, minimum: Optional[int] = None) -> int:
    """宽松版 env 整数：非法值回退 default（定时器类配置不值得让进程起不来）。"""
    raw = _env(env, name, str(default))
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        log.warning("环境变量 %s 不是整数（%r），回退 %d", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        log.warning("环境变量 %s 低于下限 %d（%r），回退 %d", name, minimum, raw, default)
        return default
    return value


def _raw_value(raw: Mapping[str, Any], key: str, default: Any) -> Any:
    """取 JSON 配置字段：缺失或空白字符串视为未设置（保留 0 等合法值）。"""
    value = raw.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    return value


def _parse_bool(value: Any, name: str) -> bool:
    """严格解析布尔配置：true/false、1/0、yes/no、on/off；非法值抛 ConfigError。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
    raise ConfigError("%s 必须是 true/false/1/0/yes/no/on/off，当前值: %r" % (name, value))


def _env_drives(env: Mapping[str, str], name: str) -> dict[str, str]:
    """解析「盘名 -> 事件」映射：支持 '别名:事件,别名:事件' 或 JSON 对象两种格式。"""
    raw = (_env(env, name, "") or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not a dict")
            return {str(k).strip(): str(v).strip() for k, v in data.items() if k and v}
        except Exception:
            raise ConfigError("%s 不是合法的 JSON 对象" % name)
    drives: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        sep = ":" if ":" in item else ("：" if "：" in item else None)
        if sep is None:
            raise ConfigError(
                "%s 格式错误：应为「别名:事件名,别名:事件名」，当前项: %s" % (name, item)
            )
        alias, event = item.split(sep, 1)
        alias, event = alias.strip(), event.strip()
        if alias and event:
            drives[alias] = event
    return drives


def _normalize_event(value: str) -> str:
    """'auto' / 空 表示默认规则由自动发现决定（原 `_normalize_event`）。"""
    value = (value or "").strip()
    if value.lower() in ("", "auto"):
        return ""
    return value


# ==================== 单个会话（或一组会话）的 LitePan 绑定 ====================
class UserProfile:
    """单个 Telegram 会话（或一组会话）绑定的 LitePan 配置。

    字段与 `users.json` 字段名一一对应，一个都不能少（旧配置直接可用）。
    """

    def __init__(
        self,
        chat_ids: Sequence[Any],
        lite_url: str,
        api_key: str,
        default_event: str = "tg_refresh",
        source: str = "telegram",
        default_path: str = "/",
        message: str = "",
        drives: Optional[Mapping[str, Any]] = None,
        admin_user: str = "",
        admin_password: str = "",
        lite_timeout: int = 15,
        receipt_poll: int = 5,
        receipt_timeout: int = 1800,
        show_url: bool = False,
    ):
        self.chat_ids = [int(c) for c in chat_ids]
        self.lite_url = lite_url.rstrip("/")
        self.api_key = api_key
        self.default_event = _normalize_event(default_event)
        self.source = source
        self.default_path = default_path
        self.message = message
        self.drives = {str(k).strip(): str(v).strip() for k, v in (drives or {}).items() if k and v}
        self.admin_user = admin_user
        self.admin_password = admin_password
        self.lite_timeout = lite_timeout
        self.receipt_poll = receipt_poll
        self.receipt_timeout = receipt_timeout
        self.show_url = show_url
        if receipt_timeout < receipt_poll:
            raise ConfigError(
                "receipt_timeout (%d) 必须 >= receipt_poll (%d)" % (receipt_timeout, receipt_poll)
            )

    # ---------- 派生属性 ----------
    @property
    def receipt_enabled(self) -> bool:
        """有管理员账号才能登录读运行记录 / 自动发现（原语义）。"""
        return bool(self.admin_user and self.admin_password)

    def masked_key(self) -> str:
        if len(self.api_key) <= 8:
            return "****"
        return self.api_key[:4] + "****" + self.api_key[-4:]

    def lookup_drive(self, alias: str) -> Optional[str]:
        """按盘名/规则别名（忽略大小写）查事件名，未命中返回 None。"""
        target = (alias or "").strip().lower()
        for name, event in self.drives.items():
            if name.strip().lower() == target:
                return event
        return None

    def drive_list_text(self) -> str:
        if not self.drives:
            return "（未配置盘名映射）"
        return "、".join("%s → %s" % (name, event) for name, event in self.drives.items())

    def describe(self) -> str:
        lines = []
        if self.show_url:
            lines.append("LitePan URL : %s" % self.lite_url)
        lines += [
            "默认事件   : %s (source=%s, 默认路径=%s)" % (
                self.default_event or "auto", self.source, self.default_path),
            "API Key    : %s" % self.masked_key(),
            "回执模式   : %s" % ("开启（管理员轮询）" if self.receipt_enabled else "关闭"),
        ]
        if self.drives:
            lines.append("手动映射(DRIVES) : %s" % self.drive_list_text())
        if self.message:
            lines.append("附带消息   : %s" % self.message)
        return "\n".join(lines)

    # ---------- 构造 ----------
    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "UserProfile":
        """从 `users.json` 的一个条目构造；非法值抛 ConfigError。"""
        chat_ids = raw.get("chat_ids") or raw.get("chat_id") or []
        if isinstance(chat_ids, str):
            chat_ids = re.split(r"[,，\s]+", chat_ids)
        elif isinstance(chat_ids, (int, float)):
            chat_ids = [chat_ids]
        parsed = []
        for c in chat_ids:
            try:
                parsed.append(int(str(c).strip()))
            except (TypeError, ValueError):
                raise ConfigError("users.json 条目 chat_ids 含非法值: %r" % (c,))
        if not parsed:
            raise ConfigError("users.json 中存在没有 chat_ids 的条目")
        if len(set(parsed)) != len(parsed):
            raise ConfigError("users.json 条目 chat_ids 不能重复: %r" % (parsed,))
        chat_ids = parsed
        lite_url = str(raw.get("litepan_url") or "").strip()
        api_key = str(raw.get("api_key") or "").strip()
        if not lite_url or not api_key:
            raise ConfigError("users.json 条目（chat_ids=%s）缺少 litepan_url 或 api_key" % chat_ids)
        return UserProfile(
            chat_ids=chat_ids,
            lite_url=lite_url,
            api_key=api_key,
            default_event=_normalize_event(str(raw.get("default_event") or "").strip()),
            source=str(raw.get("source") or "telegram").strip(),
            default_path=str(raw.get("default_path") or "/").strip(),
            message=str(raw.get("message") or "").strip(),
            drives=raw.get("drives") or {},
            admin_user=str(raw.get("admin_user") or "").strip(),
            admin_password=str(raw.get("admin_password") or "").strip(),
            lite_timeout=_parse_int(_raw_value(raw, "lite_timeout", 15), "lite_timeout", minimum=1),
            receipt_poll=_parse_int(_raw_value(raw, "receipt_poll", 5), "receipt_poll", minimum=1),
            receipt_timeout=_parse_int(_raw_value(raw, "receipt_timeout", 1800), "receipt_timeout", minimum=1),
            show_url=_parse_bool(raw.get("show_url", False), "show_url"),
        )

    @staticmethod
    def from_env(env: Optional[Mapping[str, str]] = None) -> "UserProfile":
        """单用户兼容：读取 `.env` 中的 LitePan 配置（变量名与原版完全一致）。"""
        env = os.environ if env is None else env
        return UserProfile(
            chat_ids=[],
            lite_url=_env(env, "LITEPAN_URL", required=True),
            api_key=_env(env, "LITEPAN_API_KEY", required=True),
            # 兼容旧配置：优先读新名，未设置时回退旧名 LITEPAN_EVENT
            default_event=_normalize_event(
                _env(env, "LITEPAN_FALLBACK_EVENT", _env(env, "LITEPAN_EVENT", "tg_refresh"))
            ),
            source=_env(env, "LITEPAN_SOURCE", "telegram"),
            default_path=_env(env, "LITEPAN_DEFAULT_PATH", "/"),
            message=_env(env, "LITEPAN_MESSAGE", ""),
            drives=_env_drives(env, "DRIVES"),
            admin_user=_env(env, "LITEPAN_ADMIN_USER", ""),
            admin_password=_env(env, "LITEPAN_ADMIN_PASSWORD", ""),
            lite_timeout=_parse_int(_env(env, "LITEPAN_TIMEOUT", "15"), "LITEPAN_TIMEOUT", minimum=1),
            receipt_poll=_parse_int(_env(env, "TG_RECEIPT_POLL_SECONDS", "5"), "TG_RECEIPT_POLL_SECONDS", minimum=1),
            receipt_timeout=_parse_int(
                _env(env, "TG_RECEIPT_TIMEOUT_SECONDS", "1800"), "TG_RECEIPT_TIMEOUT_SECONDS", minimum=1
            ),
            show_url=_parse_bool(_env(env, "SHOW_LITEPAN_URL", "0"), "SHOW_LITEPAN_URL"),
        )


# ==================== 全部会话配置（原 Config） ====================
class LitePanConfig:
    """`users.json`（多会话）优先，`.env` 单用户兜底。

    `settings.users_file`（`data/litepan-users.json`）是默认路径；
    `LITEPAN_USERS_FILE` / `USERS_FILE` 可覆盖（保留原变量名）。
    """

    def __init__(
        self,
        settings: Any = None,
        env: Optional[Mapping[str, str]] = None,
        *,
        users_file: Any = None,
    ):
        self.settings = settings
        self.env: dict[str, str] = dict(os.environ if env is None else env)

        path = users_file
        if not path:
            path = _env(self.env, "LITEPAN_USERS_FILE") or _env(self.env, "USERS_FILE")
        if not path and settings is not None:
            path = getattr(settings, "users_file", None)
        self.users_file = Path(str(path)) if path else Path("data/litepan-users.json")

        self.profiles: dict[int, UserProfile] = {}
        self.fallback: Optional[UserProfile] = None
        #: 配置问题的用户可读说明（缺配置 / JSON 坏 / 条目非法），不抛异常
        self.error: str = ""
        self.menu_refresh_minutes = _env_int(self.env, "TG_MENU_REFRESH_MINUTES", 30, minimum=1)
        self.menu_budget = _env_int(self.env, "LITEPAN_MENU_BUDGET", 30, minimum=1)
        self.load()

    # ---------- 加载 ----------
    def load(self) -> None:
        """重新读取 users.json；失败只记录 `error`，不打断进程。"""
        self.profiles = {}
        self.fallback = None
        self.error = ""

        if self.users_file.is_file():
            try:
                with self.users_file.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as exc:
                self.error = "读取 %s 失败: %s" % (self.users_file, exc)
                log.error("读取 LitePan 用户配置失败：%s", self.error)
                data = None
            if data is not None:
                raw_users = data.get("users") if isinstance(data, dict) else data
                if not isinstance(raw_users, list):
                    self.error = "%s 格式错误：应为 {\"users\": [...]}" % self.users_file
                    log.error(self.error)
                elif not raw_users:
                    self.error = (
                        "%s 存在但没有用户条目；若要用 .env 单用户模式，请删除或改名该文件" % self.users_file
                    )
                    log.warning(self.error)
                else:
                    for raw in raw_users:
                        try:
                            profile = UserProfile.from_dict(raw)
                        except ConfigError as exc:
                            self.error = str(exc)
                            log.error("LitePan 用户条目非法，已跳过：%s", exc)
                            continue
                        for cid in profile.chat_ids:
                            self.profiles[cid] = profile
                    log.info("已加载 %d 个 LitePan 会话配置（%s）", len(self.profiles), self.users_file)

        if not self.profiles and _env(self.env, "LITEPAN_URL") and _env(self.env, "LITEPAN_API_KEY"):
            try:
                self.fallback = UserProfile.from_env(self.env)
                log.info("未发现可用 users.json，使用 .env 单用户 LitePan 模式")
            except ConfigError as exc:
                self.error = str(exc)
                log.error("LitePan env 单用户配置非法：%s", exc)

        if not self.enabled and not self.error:
            self.error = (
                "未找到任何 LitePan 配置：请配置 %s，或使用 .env 单用户模式（LITEPAN_URL / LITEPAN_API_KEY）"
                % self.users_file
            )
            log.warning(self.error)

    # ---------- 查询 ----------
    @property
    def enabled(self) -> bool:
        return bool(self.profiles or self.fallback)

    @property
    def allowed_ids(self) -> set[int]:
        """兼容旧属性：env 白名单优先，否则就是 users.json 的 chat_ids。"""
        raw = _env(self.env, "TG_ALLOWED_IDS", "") or ""
        ids: set[int] = set()
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                log.warning("TG_ALLOWED_IDS 含非法值: %r", part)
        if ids:
            return ids
        return set(self.profiles.keys())

    def profile_for(self, chat_id: Any) -> Optional[UserProfile]:
        """按 chat_id 取绑定；未命中时回退 env 单用户 profile（没有则 None）。"""
        try:
            key = int(chat_id)
        except (TypeError, ValueError):
            return self.fallback
        return self.profiles.get(key, self.fallback)

    def all_profiles(self) -> list[UserProfile]:
        """去重后的全部 profile（多人共享同一 LitePan 实例时只算一个）。"""
        seen: set[int] = set()
        out: list[UserProfile] = []
        for profile in list(self.profiles.values()) + ([self.fallback] if self.fallback else []):
            if profile is None or id(profile) in seen:
                continue
            seen.add(id(profile))
            out.append(profile)
        return out

    def check_summary(self) -> str:
        lines = ["LitePan 用户配置：%s" % self.users_file]
        if self.profiles:
            for cid in sorted(self.profiles):
                p = self.profiles[cid]
                lines.append(
                    "  chat %s -> %s（默认规则 %s，%d 个别名）"
                    % (cid, p.lite_url, p.default_event, len(p.drives))
                )
        if self.fallback is not None:
            lines.append("  （兼容模式）env -> %s" % self.fallback.lite_url)
        if not self.enabled:
            lines.append("  ⚠️ %s" % self.error)
        return "\n".join(lines)


__all__ = ["ConfigError", "UserProfile", "LitePanConfig"]
