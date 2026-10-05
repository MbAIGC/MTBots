"""LitePan 自动发现：账号 / STRM 任务 / Webhook 规则 -> 盘名与规则 slug。

照搬原 `tgbot.py` 的 `Discovery`（`_slugify` / `_fit_slug` / `_build_rule_slugs` /
`_build_slugs` / `account_rules`），只做两处改造：

* 所有网络调用仍在 `LitePanClient`（同步），由 `handlers.py` 用 `asyncio.to_thread` 包起来；
* `client` 可注入，方便单元测试用假客户端喂 canned JSON（不联网）。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping, Optional

from .client import LitePanClient, LitePanError

log = logging.getLogger("mtbots.litepan.discovery")

try:  # pragma: no cover - 取决于是否装了 pypinyin
    from pypinyin import lazy_pinyin

    _PINYIN_AVAILABLE = True
except ImportError:  # pragma: no cover
    lazy_pinyin = None  # type: ignore[assignment]
    _PINYIN_AVAILABLE = False

#: Telegram 命令名上限 32 字符（`refresh_` 占 8 个），slug 限 24（原注释）。
MAX_SLUG_LEN = 24


# ==================== 字段容错解析（原样照搬） ====================
def _int_field(obj: Mapping[str, Any], key: str, default: int = 0) -> int:
    """容错解析接口字段为整数：缺失或类型异常时返回默认值。"""
    try:
        return int(obj.get(key) or default)
    except (TypeError, ValueError):
        return default


def _str_field(obj: Mapping[str, Any], key: str, default: str = "") -> str:
    """容错解析接口字段为字符串：缺失或类型异常时返回默认值。"""
    try:
        value = obj.get(key)
        if value is None:
            return str(default)
        return str(value).strip() or str(default)
    except (TypeError, ValueError):
        return str(default)


# ==================== slug ====================
def _slugify(name: str) -> str:
    """把名称转成 Telegram 命令可用的 slug（小写字母/数字/下划线）。

    Telegram 命令名不允许中文，中文部分转拼音（剧集 -> juji）；
    未安装 pypinyin 时退化为仅保留 ASCII 部分。
    """
    if _PINYIN_AVAILABLE and lazy_pinyin is not None:
        name = "".join(lazy_pinyin(name))
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _fit_slug(slug: str, used: set, max_len: int = MAX_SLUG_LEN) -> str:
    """保证 slug 不超长且唯一：'refresh_' 前缀 + slug 总长不超过 32 字符。"""
    slug = slug[:max_len]
    base, i = slug, 1
    while slug in used:
        i += 1
        suffix = "_%d" % i
        slug = base[: max_len - len(suffix)] + suffix
    used.add(slug)
    return slug


class Discovery:
    """从 LitePan 管理接口读取账号、任务、规则，自动生成盘名 -> 规则映射。"""

    def __init__(self, profile: Any, client: Optional[Any] = None):
        self.profile = profile
        self.client = client  # 可注入假客户端（测试用）；None 时按 profile 现建
        self.accounts: dict[int, str] = {}        # account_id -> 账号名（如 光鸭-A）
        self.strm_tasks: dict[int, dict] = {}     # strm task_id -> {name, account_id}
        self.organize_tasks: dict[int, dict] = {}  # organize task_id -> {name, account_id}
        self.rules: list[dict] = []               # webhook 规则：{id,name,event,tasks,accounts,slug}
        self.by_account: dict[int, set] = {}      # account_id -> set(event)（仅单账号规则）
        #: account_id -> set(rule_id)：**完整解析成功**的单盘规则索引。
        #: `/refresh <盘名>` 只能按它选规则，不能回头重扫 self.rules。
        self.safe_by_account: dict[int, set[int]] = {}
        self.slugs: dict[str, str] = {}           # slug（如 gy01） -> 账号名（如 GY01）
        self.rule_by_slug: dict[str, dict] = {}   # slug -> 规则 {id,name,event,tasks}

    # ---------- 拉取 ----------
    def fetch(self) -> None:
        """从 LitePan 管理接口读取账号、任务、规则；解析异常统一降级为 LitePanError。"""
        try:
            self._fetch()
        except LitePanError:
            raise
        except (TypeError, ValueError, AttributeError, KeyError) as e:
            raise LitePanError("自动发现数据解析失败: %s" % e)

    def _fetch(self) -> None:
        client = self.client or LitePanClient(self.profile)
        for acc in client.admin_get("/api/admin/accounts"):
            aid = _int_field(acc, "id")
            if aid:
                self.accounts[aid] = _str_field(acc, "name")
        options: dict = {}
        try:
            options = client.admin_get("/api/admin/automation/options") or {}
        except LitePanError:
            options = {}
        for t in options.get("strm_tasks") or []:
            tid = _int_field(t, "id")
            if tid:
                self.strm_tasks[tid] = {
                    "name": _str_field(t, "name"),
                    "account_id": _int_field(t, "account_id"),
                }
        for t in options.get("organize_tasks") or []:
            tid = _int_field(t, "id")
            if tid:
                self.organize_tasks[tid] = {
                    "name": _str_field(t, "name"),
                    "account_id": _int_field(t, "account_id"),
                }
        for r in client.admin_get("/api/admin/automation/rules"):
            if r.get("trigger_type") != "webhook":
                continue
            ev = _str_field(r.get("trigger_config") or {}, "event")
            if not ev:
                continue
            rid = _int_field(r, "id")
            task_labels: list[str] = []
            task_accounts: set[int] = set()
            parse_ok = True  # 所有动作均成功解析（类型已知且任务存在）
            for a in r.get("actions") or []:
                atype = a.get("type")
                kind = None
                if atype in ("strm", "strm_scrape"):
                    kind = "strm"
                elif atype == "organize":
                    kind = "organize"
                else:
                    parse_ok = False
                    continue
                tid = _int_field(a.get("params") or {}, "task_id")
                task = (self.strm_tasks if kind == "strm" else self.organize_tasks).get(tid)
                if not tid or task is None:
                    parse_ok = False
                    continue
                task_labels.append(task["name"] or "%s任务#%s" % (kind, tid))
                if task.get("account_id"):
                    task_accounts.add(task["account_id"])
            info = {
                "id": rid,
                "name": _str_field(r, "name") or ("规则#%s" % rid),
                "event": ev,
                "tasks": task_labels,
                "accounts": sorted(task_accounts),
            }
            self.rules.append(info)
            # 只把「全部动作解析成功、且所有任务都属于同一账号」的规则算作单盘规则，
            # 避免 /refresh <盘名> 误触发挂未知任务、其他动作或多账号任务的规则。
            if parse_ok and len(task_accounts) == 1:
                account = next(iter(task_accounts))
                self.by_account.setdefault(account, set()).add(ev)
                # 同时记下「这个账号的这条规则完整解析成功」。account_rules() 必须读这份
                # 索引：只按 rules 里的 accounts 字段重扫，会把挂未知动作 / task 已失效的
                # 规则也算成单盘规则，`/refresh A` 就会执行超出按盘确认范围的动作。
                self.safe_by_account.setdefault(account, set()).add(rid)
        self._build_rule_slugs()
        self._build_slugs()

    # ---------- slug 构建 ----------
    def _build_slugs(self) -> None:
        """账号 slug：与规则 slug 共用命名空间，冲突时自动加 _2/_3 后缀。"""
        used = set(self.rule_by_slug)
        pan_i = 0
        for aid in sorted(self.by_account, key=lambda a: self.accounts.get(a, "")):
            name = self.accounts.get(aid, "")
            slug = _slugify(name)
            if not slug:
                pan_i += 1
                slug = "pan%d" % pan_i
            slug = _fit_slug(slug, used)
            self.slugs[slug] = name

    def _build_rule_slugs(self) -> None:
        """按规则名生成菜单命令 slug：限长 + 重名自动加 _2/_3 后缀。"""
        used: set[str] = set()
        for r in sorted(self.rules, key=lambda x: x["id"]):
            slug = _slugify(r["name"])
            if not slug:
                slug = "rule_%s" % r["id"]
            slug = _fit_slug(slug, used)
            r["slug"] = slug
            self.rule_by_slug[slug] = r

    # ---------- 查询 ----------
    def account_rules(self, name: str) -> list[dict]:
        """按账号名查该账号的**完整解析成功**的单盘规则：先精确匹配，再唯一子串匹配。"""
        target = (name or "").strip().lower()
        exact = [aid for aid, n in self.accounts.items() if n.lower() == target]
        ids = set(exact)
        if not ids:
            subs = [
                aid
                for aid, n in self.accounts.items()
                if target and (target in n.lower() or n.lower() in target)
            ]
            if len(subs) != 1:
                return []
            ids = set(subs)
        safe_ids = {rid for aid in ids for rid in self.safe_by_account.get(aid, ())}
        return [r for r in self.rules if r["id"] in safe_ids]

    def account_events(self, name: str) -> list[str]:
        return sorted(set(r["event"] for r in self.account_rules(name)))


__all__ = ["Discovery", "LitePanError", "MAX_SLUG_LEN", "_slugify", "_fit_slug"]
