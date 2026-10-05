"""统一 ACL：默认拒绝 + 角色 + 按模块授权。

合并前的三套权限模型是三种不同的东西：
  · LDMG     —— 白名单内所有人共享宿主 Docker 权限（要么全给、要么不给）
  · LitePan  —— 按 chat_id 绑定各自的实例（隔离好，但没有角色概念）
  · ClinePass—— 每个用户管自己的 Key，**白名单留空 = 所有人可用**（必须改掉）

合并后统一为：**默认拒绝**；用户必须出现在白名单里；角色决定能看到哪些模块。
"""

from __future__ import annotations

import logging
from typing import Iterable, Mapping, Optional

log = logging.getLogger("mtbots.acl")

#: 角色从高到低
ROLE_ORDER = ("owner", "admin", "user")

#: 模块默认可见角色（设计文档 §5.6）
DEFAULT_MODULE_ROLES: dict[str, set[str]] = {
    "docker": {"owner", "admin"},
    "litepan": {"owner", "admin", "user"},
    "cline": {"owner", "admin", "user"},
}

#: 同一模块的多种写法（命令前缀 / 全名）
MODULE_ALIASES = {
    "d": "docker",
    "docker": "docker",
    "ldmg": "docker",
    "p": "litepan",
    "litepan": "litepan",
    "pan": "litepan",
    "c": "cline",
    "cline": "cline",
    "clinepass": "cline",
}


def normalize_module(module_id: str) -> str:
    key = (module_id or "").strip().lower()
    return MODULE_ALIASES.get(key, key)


class ACL:
    """白名单 + 角色 + 模块可见性；任何不确定的情况都判为「不允许」。"""

    def __init__(
        self,
        allowed_user_ids: Iterable[int] = (),
        roles: Optional[Mapping[int | str, str]] = None,
        module_roles: Optional[Mapping[str, Iterable[str]]] = None,
        *,
        default_role: str = "owner",
        default_deny: bool = True,
    ):
        self.allowed_user_ids = frozenset(int(uid) for uid in allowed_user_ids)
        role = (default_role or "").strip().lower()
        if role not in ROLE_ORDER:
            # 原来是 `default_role if ... else "owner"`：把默认角色写成 admn/usr 这类笔误，
            # 结果是**静默拿到最高权限**。宁可起不来，也不要带着错误权限跑。
            raise ValueError(
                "非法的默认角色 %r；可选值：%s" % (default_role, "、".join(ROLE_ORDER))
            )
        self.default_role = role
        self.default_deny = default_deny
        self.roles: dict[int, str] = {}
        for key, role in (roles or {}).items():
            try:
                uid = int(key)
            except (TypeError, ValueError):
                raise ValueError("无法解析的角色配置用户 ID：%r -> %r" % (key, role)) from None
            role = (role or "").strip().lower()
            if role not in ROLE_ORDER:
                # 原来只 warning + continue：写错的用户会静默落到 default_role（常是 owner）。
                raise ValueError(
                    "非法的角色配置 %r -> %r；可选值：%s" % (key, role, "、".join(ROLE_ORDER))
                )
            self.roles[uid] = role

        self.module_roles: dict[str, set[str]] = {
            normalize_module(mod): {r.strip().lower() for r in rs}
            for mod, rs in DEFAULT_MODULE_ROLES.items()
        }
        for mod, rs in (module_roles or {}).items():
            self.module_roles[normalize_module(mod)] = {r.strip().lower() for r in rs}

    # ---------- 判定 ----------
    def is_allowed(self, user_id: Optional[int]) -> bool:
        if user_id is None:
            return False
        if not self.allowed_user_ids:
            # 默认拒绝：白名单为空 = 谁都不能用（而不是谁都能用）
            return not self.default_deny
        return int(user_id) in self.allowed_user_ids

    def role(self, user_id: Optional[int]) -> Optional[str]:
        if not self.is_allowed(user_id):
            return None
        uid = int(user_id)  # type: ignore[arg-type]
        return self.roles.get(uid, self.default_role)

    def is_admin(self, user_id: Optional[int]) -> bool:
        return self.role(user_id) in ("owner", "admin")

    def can(self, user_id: Optional[int], module_id: str) -> bool:
        if not self.is_allowed(user_id):
            return False
        role = self.role(user_id)
        if role is None:
            return False
        module = normalize_module(module_id)
        allowed_roles = self.module_roles.get(module)
        if allowed_roles is None:
            return role == "owner"
        return role in allowed_roles

    def visible_modules(self, user_id: Optional[int], module_ids: Iterable[str]) -> list[str]:
        return [m for m in module_ids if self.can(user_id, m)]

    def describe(self, user_id: Optional[int]) -> str:
        if user_id is None:
            return "未识别用户（拒绝）"
        role = self.role(user_id)
        if role is None:
            return "不在白名单（拒绝）"
        mods = [m for m in ("docker", "litepan", "cline") if self.can(user_id, m)]
        return "角色 %s；可用模块：%s" % (role, "、".join(mods) or "无")


__all__ = [
    "ROLE_ORDER",
    "DEFAULT_MODULE_ROLES",
    "MODULE_ALIASES",
    "normalize_module",
    "ACL",
]
