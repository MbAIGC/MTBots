"""统一 JSON 存储：原子写 + 0600 权限 + 损坏自愈 + 自检。

合并前三家各写各的：LDMG 只写日志、LitePan 写 users.json（含管理员密码）、
ClinePass 写 config.json（含用户 API Key）。合并后统一走这一层：
**任何含密钥的文件都必须是 0600、原子替换、可自检。**
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("mtbots.store")

FILE_MODE = 0o600


class StoreError(RuntimeError):
    """存储不可用。

    `kind` 让调用方能区分「文件坏了」（可备份重建）与「环境不可写」（要用户去 chown）：
      · ``corrupt`` —— JSON 解析失败
      · ``shape``   —— 能解析但顶层不是对象
      · ``io``      —— 读写失败（权限/磁盘/路径）
    """

    def __init__(self, message: str, kind: str = "io"):
        super().__init__(message)
        self.kind = kind


def atomic_write_json(path: Path | str, data: Any, mode: int = FILE_MODE) -> None:
    """先写临时文件 + fsync，再 os.replace，最后收紧权限。"""
    target = Path(path)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % target.name, dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(target, mode)
    except OSError:  # 某些文件系统不支持 chmod，不致命
        pass


def read_json(path: Path | str, default: Optional[Any] = None) -> Any:
    """读 JSON；文件缺失返回 default，损坏时抛 StoreError（由调用方决定重建）。"""
    target = Path(path)
    try:
        with target.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise StoreError("配置文件损坏（%s）：%s" % (target, exc), kind="corrupt") from exc


class JsonStore:
    """带进程内锁的 JSON 文档存储；`mutate` 回调里做读改写，天然串行。"""

    def __init__(self, path: Path | str, default: Optional[dict] = None, mode: int = FILE_MODE):
        self.path = Path(path)
        self.mode = mode
        self._default = dict(default or {})
        self._lock = threading.RLock()
        self._data: dict = dict(self._default)
        self._loaded = False

    # ---------- 生命周期 ----------
    def load(self) -> dict:
        with self._lock:
            data = read_json(self.path, None)  # 损坏时抛 StoreError(kind="corrupt")
            if data is None:
                self._data = dict(self._default)
            elif isinstance(data, dict):
                self._data = data
            else:
                raise StoreError(
                    "配置根节点必须是 JSON 对象：%s" % self.path, kind="shape"
                )
            self._loaded = True
            return self._data

    def ensure_loaded(self) -> dict:
        if not self._loaded:
            self.load()
        return self._data

    @property
    def data(self) -> dict:
        return self.ensure_loaded()

    # ---------- 读写 ----------
    def save(self) -> None:
        with self._lock:
            try:
                atomic_write_json(self.path, self._data, self.mode)
            except OSError as exc:
                raise StoreError(
                    "写入失败（%s）：%s" % (self.path, exc), kind="io"
                ) from exc
            self._loaded = True

    def mutate(self, mutator: Callable[[dict], Any]) -> Any:
        """读改写一步完成并落盘；mutator 抛异常或写盘失败都不动内存（在副本上改）。

        直接在 `self._data` 上跑 mutator 有两个坑：写盘失败（磁盘满/只读/权限变更）后
        内存已变成新值，以及 mutator 改了一半抛异常留下半成品。改成深拷贝副本 →
        落盘 → 成功才提交，失败时内存保持原内容，也不会出现「用户被告知失败、内存却已生效」。
        """
        with self._lock:
            data = copy.deepcopy(self.ensure_loaded())
            result = mutator(data)
            try:
                atomic_write_json(self.path, data, self.mode)
            except OSError as exc:
                raise StoreError(
                    "写入失败（%s）：%s" % (self.path, exc), kind="io"
                ) from exc
            self._data = data
            self._loaded = True
            return result

    def self_check(self) -> tuple[bool, str]:
        """写入探针：确认「能不能真的存下来」，而不是假装成功。"""
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                probe = self.path.parent / (".%s.probe" % self.path.name)
                atomic_write_json(probe, {"ts": time.time()}, self.mode)
                os.unlink(probe)
            except Exception as exc:
                return False, "不可写：%s" % exc
            mode = "不存在（首次写入时创建）"
            if self.path.exists():
                try:
                    mode = oct(self.path.stat().st_mode & 0o777)
                except OSError:
                    mode = "未知"
            return True, "路径 %s（权限 %s）" % (self.path, mode)


__all__ = ["StoreError", "FILE_MODE", "atomic_write_json", "read_json", "JsonStore"]
