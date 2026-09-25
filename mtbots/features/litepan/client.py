"""LitePan HTTP 客户端（urllib，逻辑照搬原 `tgbot.py`）。

只保留「同步 stdlib HTTP」这一层：CookieJar opener、Bearer API Key、管理员登录
401 自动重登。调用方（`handlers.py`）必须用 `asyncio.to_thread(...)` 包起来，
否则一次 LitePan 卡顿会冻住整个 MTBots 事件循环（Docker / Cline 的按钮也一起卡死）。
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Optional

log = logging.getLogger("mtbots.litepan.client")

#: 管理员登录串行化：多个回执轮询同时撞上 401 时只登一次（原 `_AUTH_LOCK`）。
_AUTH_LOCK = threading.Lock()

#: 运行终态（原 `TERMINAL_STATUSES`）：进入这些状态才推回执。
TERMINAL_STATUSES = frozenset({"success", "failed", "error", "cancelled"})


class LitePanError(Exception):
    """LitePan 接口/网络错误，message 直接面向用户展示。"""


def _safe_json(raw: str) -> Any:
    """尽力解析 JSON；失败时退化成 `{"success": False, "message": <前300字>}`。"""
    try:
        return json.loads(raw)
    except Exception:
        return {"success": False, "message": raw[:300]}


class LitePanClient:
    """单个 `UserProfile`（一个 LitePan 实例 + 一套凭据）的同步 HTTP 客户端。"""

    def __init__(self, profile: Any):
        self.cfg = profile
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    # ---------- 底层 ----------
    def request(self, path: str, method: str = "GET", data: Any = None, headers: Optional[dict] = None):
        """发一次请求，返回 `(status, body)`；网络异常统一抛 `LitePanError`。"""
        url = self.cfg.lite_url + path
        hdrs = dict(headers or {})
        body = None
        if data is not None:
            if hdrs.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
                body = urllib.parse.urlencode(data).encode("utf-8")
            else:
                body = json.dumps(data).encode("utf-8")
                hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with self.opener.open(req, timeout=self.cfg.lite_timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, _safe_json(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            return e.code, _safe_json(raw)
        except urllib.error.URLError as e:
            raise LitePanError("无法连接 LitePan: %s" % e.reason)
        except (socket.timeout, TimeoutError) as e:
            raise LitePanError("连接 LitePan 超时: %s" % e)
        except OSError as e:
            raise LitePanError("无法连接 LitePan: %s" % e)

    # ---------- 开放接口 ----------
    def trigger(self, event: str, source: str, path: str) -> dict:
        """POST /api/open/automation/events（Bearer API Key）触发 Webhook 事件。"""
        payload = {"event": event, "source": source, "path": path}
        if self.cfg.message:
            payload["message"] = self.cfg.message
        status, body = self.request(
            "/api/open/automation/events",
            method="POST",
            data=payload,
            headers={"Authorization": "Bearer %s" % self.cfg.api_key},
        )
        if status >= 400 or not body.get("success"):
            raise LitePanError(body.get("message") if isinstance(body, dict) else "HTTP %d" % status)
        return body.get("data") or {}

    def health(self) -> dict:
        """GET /api/health：连接检测。"""
        status, body = self.request("/api/health")
        if status >= 400 or not (isinstance(body, dict) and body.get("success")):
            msg = body.get("message") if isinstance(body, dict) else "HTTP %d" % status
            raise LitePanError(msg)
        return body.get("data") or {}

    # ---------- 管理接口 ----------
    def login(self) -> None:
        """表单登录管理员账号，拿到 CookieJar 里的会话 cookie。"""
        with _AUTH_LOCK:
            status, body = self.request(
                "/api/auth/login",
                method="POST",
                data={"username": self.cfg.admin_user, "password": self.cfg.admin_password, "remember": "0"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if status >= 400 or not body.get("success"):
            msg = body.get("message") if isinstance(body, dict) else "HTTP %d" % status
            raise LitePanError("管理员登录失败：%s" % msg)

    def admin_get(self, path: str):
        """管理接口 GET，401 时自动重新登录再试一次。"""
        status, body = self.request(path)
        if status == 401 and self.cfg.receipt_enabled:
            self.login()
            status, body = self.request(path)
        if status >= 400 or not body.get("success"):
            msg = body.get("message") if isinstance(body, dict) else "HTTP %d" % status
            raise LitePanError(msg)
        return body.get("data") or []

    def max_run_id(self) -> int:
        """当前全局最大运行 ID（运行列表按 ID 倒序，取第一条即可）。"""
        runs = self.admin_get("/api/admin/automation/runs?limit=1")
        if runs:
            return int((runs[0] or {}).get("id") or 0)
        return 0

    def run_rule(self, rule_id: Any) -> dict:
        """按规则 ID 精确执行（管理接口），401 时自动重新登录再试一次。"""
        path = "/api/admin/automation/rules/%s/run" % rule_id
        status, body = self.request(path, method="POST")
        if status == 401 and self.cfg.receipt_enabled:
            self.login()
            status, body = self.request(path, method="POST")
        if status >= 400 or not body.get("success"):
            msg = body.get("message") if isinstance(body, dict) else "HTTP %d" % status
            raise LitePanError(msg)
        return body.get("data") or {}

    def list_runs(self, rule_id: Any, limit: int = 5) -> list:
        """按规则 ID 查运行记录（回执轮询用）。"""
        qs = urllib.parse.urlencode({"rule_id": rule_id, "limit": limit})
        return self.admin_get("/api/admin/automation/runs?%s" % qs)


__all__ = ["LitePanError", "LitePanClient", "TERMINAL_STATUSES", "_AUTH_LOCK"]
