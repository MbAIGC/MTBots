"""统一日志：出口脱敏 + 滚动文件日志 + Bot Token 掩码。

合并后三个模块的日志走同一条出口，所以「脱敏」必须是全局的：
LitePan 管理员密码、Cline API Key、Docker 命令输出、Telegram Bot Token
在写进任何 handler 之前都会被抹掉（消息层与日志层双重）。
"""

from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable, Optional

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

#: 出口兜底脱敏的三个模式（沿用 ClinePass core.py 的实现，另加 LitePan 密码字段）
_SECRET_PATTERNS = (
    # Telegram Bot Token：带 bot 前缀的 URL 形态，以及异常消息里裸着的 123456789:AAF...
    (re.compile(r"(?:bot)?\d{5,}:[A-Za-z0-9_\-]{20,}"), "bot<TOKEN>"),
    (re.compile(r"sk_[A-Za-z0-9_\-]{8,}"), "sk_<KEY>"),
    (re.compile(r"lpk_[A-Za-z0-9_\-]{8,}"), "lpk_<KEY>"),
    # 账号邮箱也是敏感信息
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "***@***"),
    # 管理员密码字段（JSON / 查询串 / 表单三种形态）
    (re.compile(r'("(?:password|admin_password)"\s*:\s*")[^"]*(")'), r"\1***\2"),
    (re.compile(r"((?:password|admin_password)=)[^&\s]+"), r"\1***"),
    # 认证头（万一上游库把 header 打出来）
    (re.compile(r"(?i)\b(bearer|authorization)\b\s*[:=]?\s*[A-Za-z0-9._\-]{8,}"), r"\1 ***"),
    # 多主机：IPv4 只留网段（远端地址、主机 id 都可能是 IP）
    (re.compile(r"\b(\d{1,3})\.(\d{1,3})\.\d{1,3}\.\d{1,3}\b"), r"\1.\2.*.*"),
    (re.compile(r"\b(\d{1,3})-(\d{1,3})-\d{1,3}-\d{1,3}\b"), r"\1-\2-*-*"),
    # ssh 目标里的用户名：mtbots@192.168.*.* -> ***@192.168.*.*
    (re.compile(r"\b[A-Za-z0-9._\-]{1,32}@(?=[A-Za-z0-9._\-]|\d)"), "***@"),
    # 私钥文件名（保留目录，方便排查「私钥不存在」）
    (re.compile(r"\bid_(?:rsa|dsa|ecdsa|ed25519)\b"), "id_***"),
    # Telegram 会话/用户 id（日志形如 chat=123 / chat:123 / scope=chat:123 / user=123）
    (re.compile(r"(?i)\b(chat|user|user_id|chat_id|from_id)([=:])(-?\d{4,})"), r"\1\2***"),
)

_REDACT_KEYS = ("password", "api_key", "apikey", "token", "secret")


def redact(text: object) -> str:
    """把 Token / API Key / 邮箱从任何准备写进日志或消息的文本里抹掉。"""
    if not text:
        return "" if text is None else str(text)
    out = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def mask_secret(value: str, keep: int = 4) -> str:
    """展示用掩码：sk_abcd…wxyz -> 'abcd****wxyz'。"""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "****"
    return "%s****%s" % (value[:keep], value[-keep:])


class RedactingFilter(logging.Filter):
    """在 handler 出口处重写 record，保证任何 logger 都无法绕过脱敏。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        try:
            # 关键：带 exc_info 的日志（log.exception / log.error(exc_info=True)）在到达 handler
            # 之前 traceback 还没被格式化，如果不在这里先格式化再脱敏，Token 与 Key 会明文落盘
            # —— PTB 的 InvalidToken 就会把明文 Token 写进 traceback。
            if record.exc_info and not record.exc_text:
                record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
                record.exc_info = None
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, tuple):
                    record.args = tuple(
                        redact(a) if isinstance(a, str) else a for a in record.args
                    )
                elif isinstance(record.args, dict):
                    record.args = {
                        k: (redact(v) if isinstance(v, str) else v) for k, v in record.args.items()
                    }
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
        except Exception:  # pragma: no cover - 脱敏绝不能让日志本身崩掉
            pass
        return True


class TokenMaskFilter(logging.Filter):
    """把确切已知的 Bot Token 换成占位符（比正则更保险，来自 LDMG）。"""

    def __init__(self, token: str, placeholder: str = "[REDACTED_BOT_TOKEN]"):
        super().__init__()
        self.token = token or ""
        self.placeholder = placeholder

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if not self.token:
            return True
        try:
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
                record.exc_info = None
            if isinstance(record.msg, str) and self.token in record.msg:
                record.msg = record.msg.replace(self.token, self.placeholder)
            if record.args:
                if isinstance(record.args, tuple):
                    record.args = tuple(
                        a.replace(self.token, self.placeholder) if isinstance(a, str) else a
                        for a in record.args
                    )
                elif isinstance(record.args, dict):
                    record.args = {
                        k: (v.replace(self.token, self.placeholder) if isinstance(v, str) else v)
                        for k, v in record.args.items()
                    }
            if record.exc_text and self.token in record.exc_text:
                record.exc_text = record.exc_text.replace(self.token, self.placeholder)
        except Exception:  # pragma: no cover
            pass
        return True


def install_excepthook() -> None:
    """最后一道防线：未捕获异常的 traceback 直接打到 stderr，绕过 logging 过滤器。"""

    def hook(exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        import traceback

        text = "".join(traceback.format_exception(exc_type, exc, tb))
        logging.getLogger("mtbots.fatal").critical("未捕获异常，进程退出：\n%s", redact(text))

    sys.excepthook = hook


def setup_logging(
    level: str = "INFO",
    log_dir: Optional[Path | str] = None,
    token: str = "",
    *,
    quiet_libs: bool = True,
) -> logging.Logger:
    """安装根 logger 的 console + rotating file handler，并挂上脱敏过滤器。"""
    lvl = getattr(logging, (level or "INFO").strip().upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(lvl)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(console)

    if log_dir:
        try:
            path = Path(log_dir)
            path.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                path / "mtbots.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
            root.addHandler(file_handler)
        except Exception as exc:  # 文件日志失败不影响主流程
            logging.getLogger("mtbots.logging").warning("文件日志初始化失败（不影响运行）：%s", exc)

    redacting = RedactingFilter()
    mask = TokenMaskFilter(token)
    for handler in root.handlers:
        handler.addFilter(redacting)
        handler.addFilter(mask)

    if quiet_libs and lvl > logging.DEBUG:
        for noisy in ("httpx", "httpcore", "telegram", "telegram.ext", "urllib3", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("mtbots")


def add_log_filters(handlers: Iterable[logging.Handler], token: str = "") -> None:
    """给已经存在的 handler 补装过滤器（测试或外部初始化日志时用）。"""
    for handler in handlers:
        handler.addFilter(RedactingFilter())
        if token:
            handler.addFilter(TokenMaskFilter(token))


def env_log_level(env: Optional[dict] = None) -> str:
    source = os.environ if env is None else env
    return (source.get("LOG_LEVEL") or "INFO").strip().upper() or "INFO"


__all__ = [
    "LOG_FORMAT",
    "RedactingFilter",
    "TokenMaskFilter",
    "redact",
    "mask_secret",
    "install_excepthook",
    "setup_logging",
    "add_log_filters",
    "env_log_level",
]
