"""Docker 模块配置：全局 Settings + 环境变量 → 一份不可变的运行参数。

保留 LDMG 的环境变量名（`COMMAND_TIMEOUT` / `PROJECTS_CACHE_TTL`），
每页项目数 `PAGE_SIZE` 直接沿用全局 `mtbots.config.Settings`（它已经读过同一个变量），
日志目录也取自全局 Settings，避免三个模块各写各的路径。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

#: 单条 docker / compose 命令超时（秒），对应 LDMG 的 COMMAND_TIMEOUT
DEFAULT_COMMAND_TIMEOUT = 300
#: 项目扫描结果缓存秒数，对应 LDMG 的 PROJECTS_CACHE_TTL
DEFAULT_PROJECTS_CACHE_TTL = 15.0
#: 主面板每页项目数（LDMG 的 PAGE_SIZE，默认 6）
DEFAULT_PAGE_SIZE = 6


def _env(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    return value.strip() if isinstance(value, str) else ""


def _env_int(env: Mapping[str, str], name: str, default: int, *, low: int = 1) -> int:
    raw = _env(env, name)
    if not raw:
        return default
    try:
        value = int(float(raw))
    except ValueError:
        return default
    return max(low, value)


def _env_float(env: Mapping[str, str], name: str, default: float, *, low: float = 0.0) -> float:
    raw = _env(env, name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(low, value)


@dataclass(frozen=True)
class DockerSettings:
    """Docker 模块的运行参数（非法值一律回退默认，绝不崩）。"""

    page_size: int = DEFAULT_PAGE_SIZE
    command_timeout: int = DEFAULT_COMMAND_TIMEOUT
    projects_cache_ttl: float = DEFAULT_PROJECTS_CACHE_TTL
    log_dir: Path = Path("data/logs")

    @classmethod
    def from_env(cls, settings: Any, env: Optional[Mapping[str, str]] = None) -> "DockerSettings":
        """从全局 Settings（page_size / log_dir）与旧 LDMG 环境变量构造。"""
        env = os.environ if env is None else env

        page_size: Any = getattr(settings, "page_size", DEFAULT_PAGE_SIZE)
        try:
            page_size = int(page_size)
        except (TypeError, ValueError):
            page_size = DEFAULT_PAGE_SIZE
        if page_size < 1:
            page_size = DEFAULT_PAGE_SIZE

        log_dir: Any = getattr(settings, "log_dir", None) or Path("data/logs")

        return cls(
            page_size=page_size,
            command_timeout=_env_int(env, "COMMAND_TIMEOUT", DEFAULT_COMMAND_TIMEOUT, low=1),
            projects_cache_ttl=_env_float(
                env, "PROJECTS_CACHE_TTL", DEFAULT_PROJECTS_CACHE_TTL, low=0.0
            ),
            log_dir=Path(log_dir),
        )


__all__ = [
    "DockerSettings",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_COMMAND_TIMEOUT",
    "DEFAULT_PROJECTS_CACHE_TTL",
]
