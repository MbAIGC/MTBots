"""多主机：把「在哪台机器上跑 docker」抽象成一个可校验的对象。

* `local`：今天的方式——挂进容器的 `docker.sock` + 容器里的 `docker compose`；
* `ssh`  ：**SSH 执行**——`ssh <目标> docker compose -f <远端路径> …`。yml 留在远端、由远端
  的 CLI 解析，所以**不需要挂载任何远端目录**，也不存在副本过期/漂移的问题（这也是选它而不是
  `DOCKER_HOST` 的原因：后者要求容器内能读到 yml，且相对 bind 会被解析成绝对路径发给远端
  daemon，路径不一致时 dockerd 会静默建一个空目录顶上）。

没有 `data/docker-hosts.json` 时只有一台 `local` 主机，行为与单机版完全一致。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from ...text import esc

log = logging.getLogger("mtbots.docker")

#: 主机 id：只用小写字母/数字/_/-，避免拼进回调或命令时出花样
HOST_ID_RE = re.compile(r"^[a-z0-9_-]{1,16}$")
#: ssh 目标：user@host / user@1.2.3.4 / user@[::1]。
#: user 部分**不许以 `-` 开头**：否则 `-oProxyCommand=…@host` 会被 ssh 当成选项（选项注入）。
TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*@[A-Za-z0-9._:\-\[\]]+$")

DEFAULT_IDENTITY = "/app/data/ssh/id_ed25519"
DEFAULT_KNOWN_HOSTS = "/app/data/ssh/known_hosts"
DEFAULT_PORT = 22
#: 只允许这两种：`yes`（配预置 known_hosts）与 `accept-new`（首次自动记录）
STRICT_VALUES = ("yes", "accept-new")

#: ssh 自己失败时的退出码 → 面板提示（不是 docker 的错，得分开说）
SSH_EXIT_HINTS = {
    255: "SSH 连不上或认证失败（检查网络、端口、私钥、known_hosts）",
    126: "远端授权只允许 compose 操作（守卫脚本拒绝了这条命令）",
    127: "远端未安装 docker compose / docker",
}


def explain_exit(code: int, output: str, host: "DockerHost") -> str:
    """把一次远端命令的失败翻成人话（远端主机才有 ssh 那套退出码）。"""
    if host.is_remote and int(code) in SSH_EXIT_HINTS:
        return SSH_EXIT_HINTS[int(code)]
    detail = (output or "").strip().splitlines()
    first = detail[0].strip() if detail else ""
    if first:
        return first[:200]
    return "退出码 %d" % int(code)


@dataclass(frozen=True)
class DockerHost:
    """一台被管理的主机。`error` 非空表示这条配置有问题（面板据此给提示，不做探测）。"""

    id: str
    label: str = ""
    kind: str = "local"
    target: str = ""
    port: int = DEFAULT_PORT
    identity: str = DEFAULT_IDENTITY
    known_hosts: str = DEFAULT_KNOWN_HOSTS
    strict: str = "yes"
    roots: tuple[str, ...] = ()
    error: str = ""

    @property
    def is_remote(self) -> bool:
        return self.kind == "ssh"

    @property
    def display(self) -> str:
        return self.label or self.id

    def command(self, cmd: Sequence[str]) -> list[str]:
        """把一条 docker 命令包成 ssh 调用（local 主机原样返回）。

        远端命令必须是**一个字符串**：一律 `shlex.join`，路径带空格也不会散。
        """
        cmd = [str(part) for part in cmd]
        if not self.is_remote:
            return cmd
        return [
            "ssh",
            "-p",
            str(self.port),
            "-i",
            self.identity,
            "-o",
            "BatchMode=yes",  # 绝不弹交互式密码提示：拿不到密钥就直接失败
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "StrictHostKeyChecking=%s" % self.strict,
            "-o",
            "UserKnownHostsFile=%s" % self.known_hosts,
            # `--` 放在目标**之前**：选项解析在这里结束，目标即便形似选项也只会被当成主机名
            # （放在目标之后的话，`-oProxyCommand=…` 这种目标会被 ssh 当选项吃掉）
            "--",
            self.target,
            shlex.join(cmd),
        ]

    def cwd(self, local_dir: str) -> Optional[str]:
        """远端项目的 yml 在远端，本地目录不存在，不能当 cwd（会让 ssh 起不来）。"""
        if self.is_remote:
            return None
        return local_dir or None

    def allows(self, path: str) -> bool:
        """路径白名单（可选）：只管理 `roots` 前缀下的项目。"""
        if not self.roots:
            return True
        clean = str(path or "").rstrip("/")
        for root in self.roots:
            prefix = root.rstrip("/")
            if clean == prefix or clean.startswith(prefix + "/"):
                return True
        return False


LOCAL_HOST = DockerHost(id="local", label="本机", kind="local")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_disabled(raw: Mapping) -> bool:
    """`enabled: false` / `"false"` / `"0"` / `"no"` 都算临时下线这台主机。"""
    if "enabled" not in raw:
        return False
    value = raw.get("enabled")
    if isinstance(value, str):
        return value.strip().lower() in ("0", "false", "no", "off")
    return not value


def _parse_host(raw: Any) -> DockerHost:
    """解析一条主机配置；字段非法就把原因塞进 `error`，绝不抛异常。"""
    if not isinstance(raw, Mapping):
        return DockerHost(id="?", error="主机配置必须是对象")

    host_id = str(raw.get("id") or "").strip().lower()
    label = str(raw.get("label") or "").strip()
    kind = str(raw.get("kind") or "local").strip().lower()
    if not HOST_ID_RE.match(host_id):
        return DockerHost(id=host_id or "?", label=label, error="id 只能用小写字母/数字/_/-，长度 1-16")
    if kind not in ("local", "ssh"):
        return DockerHost(id=host_id, label=label, kind=kind, error="kind 只能是 local 或 ssh")

    if kind == "local":
        return DockerHost(id=host_id, label=label or "本机", kind="local")

    target = str(raw.get("target") or "").strip()
    if not TARGET_RE.match(target):
        return DockerHost(id=host_id, label=label, kind="ssh", target=target, error="target 必须是 user@host")
    port = _as_int(raw.get("port"), DEFAULT_PORT)
    if not 1 <= port <= 65535:
        return DockerHost(id=host_id, label=label, kind="ssh", target=target, error="port 超出范围")
    strict = str(raw.get("strict") or "yes").strip().lower()
    if strict not in STRICT_VALUES:
        return DockerHost(
            id=host_id, label=label, kind="ssh", target=target, port=port,
            error="strict 只能是 %s" % "/".join(STRICT_VALUES),
        )

    identity = str(raw.get("identity") or DEFAULT_IDENTITY).strip() or DEFAULT_IDENTITY
    known_hosts = str(raw.get("known_hosts") or DEFAULT_KNOWN_HOSTS).strip() or DEFAULT_KNOWN_HOSTS

    roots_raw = raw.get("roots")
    roots: list[str] = []
    if isinstance(roots_raw, Iterable) and not isinstance(roots_raw, (str, bytes)):
        for item in roots_raw:
            path = str(item or "").strip().rstrip("/")
            if path and not path.startswith("/"):
                return DockerHost(
                    id=host_id, label=label, kind="ssh", target=target, port=port,
                    identity=identity, known_hosts=known_hosts, strict=strict,
                    error="roots 必须是绝对路径",
                )
            if path:
                roots.append(path)

    host = DockerHost(
        id=host_id,
        label=label or host_id,
        kind="ssh",
        target=target,
        port=port,
        identity=identity,
        known_hosts=known_hosts,
        strict=strict,
        roots=tuple(roots),
    )
    if not os.path.exists(identity):
        return DockerHost(**{**host.__dict__, "error": "私钥不存在：%s" % identity})
    return host


def load_hosts(path: Any = None) -> tuple[list[DockerHost], list[str]]:
    """读主机清单；返回 `(主机列表, 全局提示)`。

    * 文件不存在 → 单机模式（`[local]`），无提示（这是默认部署形态）；
    * JSON 坏 / 顶层不是对象 → 退回单机并在提示里说明原因（绝不静默忽略）；
    * 单台主机字段非法 → 该主机带 `error` 返回，面板对它单独给提示。
    """
    notes: list[str] = []
    target = Path(str(path)) if path else None
    if target is None or not str(target) or str(target) == ".":
        return [LOCAL_HOST], notes
    if not target.exists():
        return [LOCAL_HOST], notes

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("主机清单读取失败（按单机跑）：%s", exc)
        return [LOCAL_HOST], ["⚠️ <code>%s</code> 读取失败，已按单机模式运行：%s" % (target, exc)]

    items = raw.get("hosts") if isinstance(raw, Mapping) else raw
    if not isinstance(items, list) or not items:
        return [LOCAL_HOST], ["⚠️ <code>%s</code> 里没有 hosts 列表，已按单机模式运行" % target]

    hosts: list[DockerHost] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, Mapping) and _is_disabled(item):
            continue  # enabled: false —— 临时下线，不出现在面板/扫描/自检里
        host = _parse_host(item)
        if host.id in seen:
            notes.append("⚠️ 主机 id 重复：<code>%s</code>（已忽略后一条）" % esc(host.id))
            continue
        seen.add(host.id)
        hosts.append(host)

    if not hosts:
        return [LOCAL_HOST], notes + ["⚠️ 主机清单里没有可用条目，已按单机模式运行"]
    return hosts, notes


__all__ = [
    "DEFAULT_IDENTITY",
    "DEFAULT_KNOWN_HOSTS",
    "DockerHost",
    "LOCAL_HOST",
    "SSH_EXIT_HINTS",
    "explain_exit",
    "load_hosts",
]
