"""compose 探测 / 项目扫描 / 命令执行 —— LDMG 的全局状态全部收进 `DockerState`。

LDMG 原来是单文件 + 7 个模块级可变全局（执行锁、当前进程、取消标记、当前任务、
compose 路径、项目缓存、缓存时间）。合并后同一个进程里还有别的模块，测试也要能建
两个互不干扰的实例，所以它们统一住进 `DockerState`，由 `register()` 放进
`core.data["docker"]`；本文件只保留纯函数与不可变常量。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ...logging_setup import redact
from ...panels import cb_simple
from ...text import esc, progress_bar

from .config import DockerSettings
from .hosts import LOCAL_HOST, DockerHost, explain_exit, load_hosts

log = logging.getLogger("mtbots.docker")

#: compose 探测 / 服务列表 / 项目扫描的子进程超时（秒）
PROBE_TIMEOUT = 10
SERVICES_TIMEOUT = 15
SCAN_TIMEOUT = 30

#: 服务列表（`compose config --services`）缓存秒数：compose 文件很少改，缓存久一点没关系；
#: 升级/清理之后 `invalidate_cache()` 会连它一起清掉。
SERVICES_CACHE_TTL = 300.0
#: 服务列表并发获取的最大线程数（每个项目一条子进程/一次 ssh）
SERVICES_PARALLEL = 6
#: 主机并发扫描的最大线程数（每台主机一条 `compose ls`）
HOST_PARALLEL = 4

#: docker pull 每层进度的噪音行，仅在「最终完成消息」中过滤：
#: Downloading [==>...] 1MB/2MB、Downloading 3%、Download complete、Extracting 12s、Verifying Checksum 等
PULL_FINAL_NOISE_RE = re.compile(
    r"^[0-9a-f]{8,64}\s+(?:Downloading\b|Download complete\b|Extracting\b|Verifying Checksum\b|Waiting\b)",
    re.IGNORECASE,
)

#: 流式进度：保留的输出行数 / 预览字数 / 最终行数
OUTPUT_BUFFER = 100
PREVIEW_LINES = 11
PREVIEW_CHARS = 3500
FINAL_LINES = 25
STREAM_READ_SIZE = 4096
STREAM_EDIT_INTERVAL = 1.2

#: docker image ls 的固定格式（制表符分隔，便于解析）
IMAGE_FORMAT = "{{.ID}}\t{{.Repository}}:{{.Tag}}\t{{.Size}}"

ProgressCallback = Callable[[int, str], None]


# ==================== 纯函数 ====================
def stop_process_tree(process: Any) -> None:
    """终止 compose 进程组，避免取消时留下子进程。"""
    if not process or process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (AttributeError, ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def sort_projects_for_display(projects: Iterable[dict]) -> list[dict]:
    """使用与主面板一致的顺序，避免编号命令选中错误项目。

    运行中优先，其次按名称；`/upgrade 01` 与面板里的「01」因此始终指向同一个项目。
    """
    return sorted(
        projects,
        key=lambda p: (
            0 if "running" in str(p.get("status", "")).lower() else 1,
            str(p.get("name", "")).lower(),
        ),
    )


def ordered_projects(projects: Iterable[dict], *, by_host: bool = False) -> list[dict]:
    """面板与编号命令共用的顺序：多主机时先按主机分组，组内保持「运行中优先」。

    `/upgrade 01` 与面板里的「01」必须永远指向同一个项目，所以两处都调这个函数。
    """
    ordered = sort_projects_for_display(projects)
    if by_host:
        ordered.sort(key=lambda p: str(p.get("host") or ""))
    return ordered


def paginate_projects(
    projects: Sequence[dict], page: int = 1, page_size: int = 6
) -> tuple[list[dict], int, int]:
    """切片分页并夹紧页码；返回 (本页项目, 实际页码, 总页数)。"""
    size = max(1, int(page_size or 1))
    total_pages = max(1, (len(projects) + size - 1) // size)
    try:
        current = int(page)
    except (TypeError, ValueError):
        current = 1
    current = max(1, min(current, total_pages))
    start = (current - 1) * size
    return list(projects[start : start + size]), current, total_pages


def is_pull_noise(line: str) -> bool:
    """判断是否为 docker pull 的逐层进度噪音行（仅最终消息过滤用）。"""
    return bool(PULL_FINAL_NOISE_RE.match((line or "").strip()))


def filter_pull_noise(lines: Iterable[str]) -> list[str]:
    """过滤逐层进度噪音，保持原有顺序。"""
    return [line for line in lines if not is_pull_noise(line)]


def normalize_image_id(image_id: str) -> str:
    """把 `docker image ls --no-trunc` 的 ID 规范成 inspect 输出的 sha256 形式。"""
    image_id = (image_id or "").strip()
    if not image_id:
        return ""
    return image_id if image_id.startswith("sha256:") else "sha256:%s" % image_id


def select_unused_images(image_lines: Iterable[str], referenced_ids: Iterable[str]) -> list[str]:
    """从 `docker image ls -a` 输出里挑出「没有任何容器引用」的镜像。

    与 `docker image prune -a` 的语义保持一致（按 ID 去重、跳过被容器引用的镜像），
    避免把正在使用的镜像误报成待删除。
    """
    referenced = set(referenced_ids or ())
    candidates: list[str] = []
    seen: set[str] = set()
    for line in image_lines:
        parts = str(line).split("\t", 2)
        if len(parts) != 3:
            continue
        image_id, repo_tag, size = (part.strip() for part in parts)
        normalized = normalize_image_id(image_id)
        if not normalized or normalized in referenced or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append("%s\t%s\t%s" % (image_id, repo_tag, size))
    return candidates


def format_prune_snapshot(text: str, limit: int = 3000) -> str:
    """清理快照：只保留尾部 limit 个字符，脱敏 + HTML 转义（长快照不撑爆消息）。"""
    return esc(redact((text or "")[-limit:]))


async def edit_html_safe(message: Any, html_text: str, fallback: Optional[str] = None) -> bool:
    """先按 HTML 编辑，失败则降级为纯文本，避免长输出截断导致消息卡死。

    返回是否至少有一种编辑成功（流式进度用它决定要不要节流下一帧）。
    """
    if fallback is None:
        fallback = re.sub(r"<[^>]+>", "", html_text)
    try:
        await message.edit_text(html_text, parse_mode="HTML")
        return True
    except Exception:
        try:
            await message.edit_text(fallback)
            return True
        except Exception:
            return False


async def delete_message_quietly(message: Any) -> bool:
    """删掉一条已经没用的执行消息；删不掉就算了（群里没删消息权限、消息超过 48 小时都会失败）。"""
    try:
        await message.delete()
        return True
    except Exception as exc:
        log.debug("删除执行消息失败（忽略）：%s", exc)
        return False


# ==================== 全局状态 ====================
@dataclass
class HostScan:
    """一台主机的扫描结果（线程里跑，结果由主线程合并——不再让 worker 直接改共享属性）。"""

    host_id: str
    projects: list[dict] = field(default_factory=list)
    error: str = ""
    hidden_dirs: list[str] = field(default_factory=list)
    skipped_projects: list[str] = field(default_factory=list)
    roots_filtered: list[str] = field(default_factory=list)


class DockerState:
    """LDMG 全部可变全局状态的宿主（每个 register() 建一个）。"""

    def __init__(
        self,
        settings: DockerSettings,
        hosts: Optional[Sequence[DockerHost]] = None,
        host_notes: Optional[Sequence[str]] = None,
    ):
        self.settings = settings
        self.exec_lock: Optional[asyncio.Lock] = None
        self.current_process: Optional[asyncio.subprocess.Process] = None
        self.cancel_requested: bool = False
        self.current_task: Optional[str] = None
        #: 只缓存探测成功的结果；失败置 [] 以便 Docker/Compose 稍后启动时重新探测
        self.compose_bin: Optional[list[str]] = None
        self.projects_cache: list[dict] = []
        self.projects_cache_time: float = 0.0
        #: 项目扫描互斥：并发的面板刷新/后台刷新只跑一次全量扫描
        self._scan_lock: Optional[asyncio.Lock] = None
        #: 服务列表缓存 `key -> (时间, [服务…], 来源)`：扫描**不再**逐项目跑 `compose config`，
        #: 只对当前页/详情页按需取，取过一次就长期复用（这是主面板从秒级回到亚秒级的关键）。
        #: `来源` = "compose"（解析配置文件）或 "containers"（容器 label 兜底），面板据此标注。
        self.services_cache: dict[str, tuple[float, list[str], str]] = {}
        #: 测试/嵌入用：替换真实扫描（返回项目列表）
        self.scan_hook: Optional[Callable[[], list[dict]]] = None
        #: 最近一次扫描的失败原因（空 = 成功）。以前这里失败是静默的，
        #: 结果「权限不足 / 目录没挂载」都表现成一句「暂未检测到任何项目」，没法排查。
        #: 多主机时这里是**本机**的错误（`host_errors` 才是全部主机）。
        self.last_scan_error: str = ""
        #: 扫到了、但 compose 目录在容器里不存在的项目目录（宿主机路径没挂进来；只对本机有意义）
        self.hidden_dirs: list[str] = []
        #: 主机清单（默认单机）；`host_errors` / `host_notes` 分别是逐主机探测失败原因与配置级提示
        self.hosts: list[DockerHost] = list(hosts) if hosts else [LOCAL_HOST]
        self.host_errors: dict[str, str] = {}
        self.host_notes: list[str] = list(host_notes or [])
        #: 远端 compose 命令探测结果（`docker compose` / `docker-compose`），按主机缓存
        self.remote_compose: dict[str, list[str]] = {}
        #: `docker compose ls` 没给 ConfigFiles / 路径为空的项目（不静默丢，面板与 --health 里说清）
        self.skipped_projects: list[str] = []
        #: 被 roots 白名单挡掉的项目（只在日志里 INFO 过，用户看不到）
        self.roots_filtered: dict[str, list[str]] = {}
        #: 上一次扫描的结果签名/错误，仅用于「只在有变化时打 INFO」
        self._last_scan_signature: Optional[tuple] = None
        self._last_scan_errors: dict[str, str] = {}

    # ---------- 主机 ----------
    @property
    def multi_host(self) -> bool:
        return len(self.hosts) > 1

    @property
    def local_host(self) -> DockerHost:
        for host in self.hosts:
            if not host.is_remote:
                return host
        return self.hosts[0]

    def host_by_id(self, host_id: Optional[str]) -> Optional[DockerHost]:
        """按 id 找主机；id 为空时返回第一台（单机部署的调用方不用关心主机）。"""
        if not host_id:
            return self.hosts[0] if self.hosts else LOCAL_HOST
        for host in self.hosts:
            if host.id == host_id:
                return host
        return None

    def host_of(self, project: Any) -> DockerHost:
        """项目所属主机（项目里没有 host 字段时=第一台，保持单机行为不变）。"""
        host_id = project.get("host") if isinstance(project, dict) else None
        return self.host_by_id(host_id) or self.local_host

    def project_label(self, project: Any) -> str:
        """多主机时项目标签带主机前缀（`vps/blog`），单机时与以前一字不差。"""
        name = str(project.get("name", "")) if isinstance(project, dict) else str(project)
        if not self.multi_host:
            return name
        return "%s/%s" % (self.host_of(project).id, name)

    def order(self, projects: Iterable[dict]) -> list[dict]:
        """面板与 `/upgrade NN` 共用的排序：多主机时先按主机分组，组内仍是「运行中优先」。"""
        ordered = sort_projects_for_display(projects)
        if self.multi_host:
            ordered.sort(key=lambda p: str(p.get("host") or ""))
        return ordered

    def cwd_for(self, project: Any) -> Optional[str]:
        """远端项目不能传本地 cwd（本地路径不存在，ssh 会起不来）。"""
        work_dir = project.get("dir", "") if isinstance(project, dict) else ""
        return self.host_of(project).cwd(str(work_dir or ""))

    # ---------- 任务锁 ----------
    def get_lock(self) -> asyncio.Lock:
        if self.exec_lock is None:
            self.exec_lock = asyncio.Lock()
        return self.exec_lock

    def get_scan_lock(self) -> asyncio.Lock:
        """项目扫描锁（惰性创建：DockerState 可能在事件循环之外被构造）。"""
        if self._scan_lock is None:
            self._scan_lock = asyncio.Lock()
        return self._scan_lock

    async def begin_task(self, task_id: str) -> bool:
        """原子地获取执行锁并登记当前任务；拿不到锁返回 False。

        用近零超时把「检查 + 获取」合并为一步，避免快速连点时
        `lock.locked()` 检查与 `async with` 之间出现竞态。
        """
        lock = self.get_lock()
        try:
            await asyncio.wait_for(lock.acquire(), timeout=0.05)
        except asyncio.TimeoutError:
            return False
        self.current_task = task_id
        self.cancel_requested = False
        return True

    def end_task(self) -> None:
        """释放执行锁并清理任务状态（必须在任务的 finally 中调用）。"""
        self.current_task = None
        self.cancel_requested = False
        self.current_process = None
        try:
            self.get_lock().release()
        except RuntimeError:
            pass

    def request_cancel(self, task_id: Optional[str] = None) -> bool:
        """请求中断当前任务；task_id 不匹配或当前没有任务时返回 False。"""
        if task_id and self.current_task and task_id != self.current_task:
            return False
        if self.current_task is None and self.current_process is None:
            return False
        self.cancel_requested = True
        process = self.current_process
        if process is not None:
            try:
                stop_process_tree(process)
            except Exception:  # 取消本身不能抛
                pass
        return True

    # ---------- compose 探测 ----------
    def get_compose_bin(self) -> list[str]:
        """探测可用的 compose 命令：优先 docker compose（v2 插件），回退 docker-compose。"""
        if self.compose_bin:
            return self.compose_bin

        for cand in (["docker", "compose"], ["docker-compose"]):
            try:
                probe = subprocess.run(
                    [*cand, "version"],
                    capture_output=True,
                    text=True,
                    timeout=PROBE_TIMEOUT,
                )
                if probe.returncode == 0:
                    self.compose_bin = list(cand)
                    log.info("使用 compose 命令: %s", " ".join(cand))
                    return self.compose_bin
            except Exception:
                continue

        self.compose_bin = []
        log.warning("未检测到 docker compose / docker-compose 命令")
        return []

    def get_remote_compose_bin(self, host: DockerHost) -> list[str]:
        """这台主机用哪条 compose 命令（远端探测一次并缓存；探测不到返回空列表）。

        远端用**它自己的** CLI：所以远端只有 `docker-compose`（老 NAS）也能用，
        不存在「bot 镜像里的版本和远端 daemon 不匹配」的问题。
        """
        if not host.is_remote:
            return self.get_compose_bin()
        if host.error:
            # 配置本身就有错（私钥不存在 / target 非法）：绝不为了探测再发一次 ssh
            return []
        cached = self.remote_compose.get(host.id)
        if cached is not None:
            return cached

        for cand in (["docker", "compose"], ["docker-compose"]):
            try:
                probe = subprocess.run(
                    host.command([*cand, "version"]),
                    capture_output=True,
                    text=True,
                    timeout=PROBE_TIMEOUT,
                )
                if probe.returncode == 0:
                    self.remote_compose[host.id] = list(cand)
                    log.info("远端主机 %s 使用 compose 命令: %s", host.id, " ".join(cand))
                    return self.remote_compose[host.id]
                if probe.returncode == 255:
                    # ssh 自己就没通（连不上/认证失败/守卫拒绝）：换 `docker-compose` 再试一次
                    # 只是白等一个 ConnectTimeout（10s），直接放弃
                    detail = (probe.stderr or probe.stdout or "").strip().splitlines()
                    log.warning(
                        "远端主机 %s 的 ssh 探测失败（退出码 %s）：%s｜%s",
                        host.id,
                        probe.returncode,
                        explain_exit(probe.returncode, probe.stderr or probe.stdout, host),
                        detail[0][:200] if detail else "-",  # ssh 的原话，排查时最有用
                    )
                    break
            except Exception:
                continue

        self.remote_compose[host.id] = []
        log.warning("远端主机 %s 未检测到 docker compose / docker-compose", host.id)
        return []

    def build_compose_cmd(self, project: dict, *args: str) -> list[str]:
        """按项目生成 compose 命令（携带完整 -f 文件列表；远端项目自动包成 ssh 调用）。"""
        host = self.host_of(project)
        compose_bin = self.get_remote_compose_bin(host)
        if not compose_bin:
            # 配置本身有错时（私钥不存在/target 非法）说「没装 compose」会把人带沟里
            if host.error:
                raise RuntimeError("主机 %s 配置有问题：%s" % (host.id, host.error))
            if host.is_remote:
                raise RuntimeError("远端主机 %s 未检测到 docker compose / docker-compose" % host.id)
            raise RuntimeError("未检测到 docker compose / docker-compose 命令")
        cmd = list(compose_bin)
        for config_file in project.get("config_files") or []:
            cmd += ["-f", config_file]
        cmd += list(args)
        return host.command(cmd)

    def get_project_services(
        self, work_dir: str, config_files: Sequence[str], host: Optional[DockerHost] = None
    ) -> tuple[Optional[list[str]], str]:
        """获取项目的服务定义（远端项目在远端跑 `config --services`）。

        返回 `(services, error)`：

        * `services is None` 表示**这次没取到**（compose 不可用/命令失败），与「取到了、
          就是空列表」区分开：前者不写缓存、下次再试，后者是有效结果；
        * `error` 是给面板看的失败摘要（成功时为空串）。它只会带路径、变量名、退出码
          这类诊断信息，上屏前仍会再走一次 `redact()`。

        注意 compose 解析**必须能读项目目录下的 `.env`**：`.env` 归 root、容器用户
        （uid 10001）读不到时，`${VAR:?}` 插值会直接失败，这条命令会返回「0 行 + 非 0 退出」。
        这种情况由 :meth:`get_project_services_from_containers` 兜底。
        """
        host = host or self.local_host
        compose_bin = self.get_remote_compose_bin(host)
        if not compose_bin:
            return None, "未检测到 docker compose / docker-compose 命令"
        try:
            cmd = list(compose_bin)
            for config_file in config_files:
                cmd += ["-f", config_file]
            cmd += ["config", "--services"]
            result = subprocess.run(
                host.command(cmd),
                cwd=host.cwd(work_dir),
                capture_output=True,
                text=True,
                timeout=SERVICES_TIMEOUT,
            )
            if result.returncode == 0:
                return [s.strip() for s in result.stdout.strip().splitlines() if s.strip()], ""
            # 先脱敏再交给 explain_exit：它内部按行截断（`[:200]`），先截断的话可能把敏感串
            # 切掉一半，后面的 redact 正则就匹配不上了。面板出口还会再过一次脱敏。
            detail = redact((result.stderr or result.stdout or "").strip())
            reason = explain_exit(result.returncode, detail, host)
            log.warning("获取 %s 服务列表失败：%s", work_dir, reason)
            return None, reason
        except Exception as exc:
            log.warning("获取 %s 服务列表失败: %s", work_dir, exc)
            # 压成一行 + 截断：这类异常可能带换行，直接上屏会把面板行数撑乱
            return None, " ".join(str(exc).split())[:200]

    def get_project_services_from_containers(
        self, project_name: str, host: Optional[DockerHost] = None
    ) -> Optional[list[str]]:
        """从容器 label 读项目实际的服务名——**不读任何文件，不需要 `.env` 权限**。

        `com.docker.compose.project` / `com.docker.compose.service` 两个 label 在容器创建时
        就写死了，所以即便项目目录里的 `.env` 属于 root、容器用户读不到（`config --services`
        会 `permission denied`），这里照样能列出服务名。用的是已经挂载的 docker.sock。

        用 `-a` 连已停止的容器一起算，停掉的服务也能从面板升级。
        返回 `None` = 这次没取到（docker 命令失败/项目名为空）。
        """
        if not project_name:
            return None
        host = host or self.local_host
        try:
            result = subprocess.run(
                host.command(
                    [
                        "docker", "ps", "-a",
                        "--filter", "label=com.docker.compose.project=%s" % project_name,
                        "--format", '{{.Label "com.docker.compose.service"}}',
                    ]
                ),
                capture_output=True,
                text=True,
                timeout=SERVICES_TIMEOUT,
            )
        except Exception as exc:
            log.warning("读取项目 %s 的容器 label 失败：%s", project_name, exc)
            return None
        if result.returncode != 0:
            log.warning(
                "读取项目 %s 的容器 label 失败：%s",
                project_name,
                explain_exit(result.returncode, result.stderr, host),
            )
            return None
        names = sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})
        return names or None

    # ---------- 服务列表（按需 + 缓存） ----------
    def services_key(self, project: Any) -> str:
        """服务列表的缓存键：同名项目在不同主机/不同 yml 下算不同条目。"""
        if not isinstance(project, dict):
            return ""
        host = self.host_of(project)
        config_files = ",".join(str(c) for c in (project.get("config_files") or []))
        return "%s|%s|%s" % (host.id, project.get("name", ""), config_files or project.get("dir", ""))

    def _cached_services(self, key: str) -> Optional[tuple[list[str], str]]:
        entry = self.services_cache.get(key)
        if not entry:
            return None
        stamp, services, source = entry
        if time.monotonic() - stamp >= SERVICES_CACHE_TTL:
            return None
        return list(services), source

    def _fill_services(self, project: dict) -> None:
        """取一个项目的服务列表并写回项目 dict（结果同时进缓存，跨扫描复用）。

        来源按优先级：

        1. **compose 文件解析**——能列出配置里定义的全部服务（含从未启动过的）；
        2. **容器 label 兜底**——文件读不到时用（典型：`.env` 归 root、容器用户读不到，
           `${VAR:?}` 插值失败）。它只认创建过容器的服务，但不需要任何文件权限；
        3. 两者都失败——保持「未加载」下次重试，并把失败摘要留在项目上供面板显示。
        """
        host = self.host_of(project)
        services, error = self.get_project_services(
            str(project.get("dir") or ""),
            list(project.get("config_files") or []),
            host,
        )
        source = "compose"
        if services is None:
            services = self.get_project_services_from_containers(
                str(project.get("name") or ""), host
            )
            source = "containers"
        if services is None:  # 两个来源都没取到：不写缓存，下次再看这一页时重试
            project["services_error"] = error or "未获取到服务列表"
            return
        project["services"] = services
        project["services_loaded"] = True
        project["services_source"] = source
        project.pop("services_error", None)
        key = self.services_key(project)
        if key:
            self.services_cache[key] = (time.monotonic(), list(services), source)

    def load_services(self, projects: Sequence[dict]) -> int:
        """给一批项目补齐服务列表（同步，交给 `ensure_services` 的 to_thread 跑）。

        只处理「这一屏真正要渲染的项目」——全量扫描时逐项目跑 `compose config` 是主面板
        慢到十来秒的元凶（远端更是每个项目一次完整 SSH 握手）。返回本次真正取过的数量。
        """
        pending = [p for p in projects if isinstance(p, dict) and self._needs_services(p)]
        if not pending:
            return 0
        workers = max(1, min(SERVICES_PARALLEL, len(pending)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="docker-svc") as pool:
            list(pool.map(self._fill_services, pending))
        return len(pending)

    async def ensure_services(self, projects: Sequence[dict]) -> None:
        """按需补齐服务列表（不阻塞事件循环）。已经加载过的项目直接跳过。"""
        pending = [p for p in projects if self._needs_services(p)]
        if pending:
            await asyncio.to_thread(self.load_services, pending)

    @staticmethod
    def _needs_services(project: Any) -> bool:
        """这个项目还要不要跑一次 `compose config --services`。

        有 `services_loaded` 标记（扫描时命中缓存）或**已经带着服务列表**（外部塞进来的
        project dict）就不用；取失败的项目保持「未加载」，下次再看这一页时重试。
        """
        return bool(
            isinstance(project, dict)
            and not project.get("services_loaded")
            and not project.get("services")
            and project.get("dir")
        )

    # ---------- 项目扫描 ----------
    def scan_projects_sync(self) -> list[dict]:
        """扫描各主机 `docker compose ls -a --format json`（同步，交给 to_thread 跑）。

        多主机时**按主机并行**（各台互不依赖，串行等一台连不上的机器纯粹浪费时间）；
        每台主机的结果由 worker 返回、在主线程合并，所以 worker 里不碰共享属性。

        失败不再静默：每台主机的原因写进 `host_errors[host_id]`（本机那份同时写进
        `last_scan_error`，兼容旧的提示路径）；本机「扫到了、但 compose 目录在容器里不存在」
        的项目目录写进 `hidden_dirs`。**远端主机不做本地目录检查**——远端路径本来就不在本机，
        检查了会把它自己的项目全部误判成「没挂载」。

        服务列表（`容器：…`）**不在这里取**：那是对每个项目再跑一条 `compose config --services`
        （远端=每项目一次 SSH 握手），15+10 个项目就是十来秒。改为 `ensure_services()`
        只对当前页/详情页按需取。
        """
        started = time.monotonic()
        self.host_errors = {}
        self.last_scan_error = ""
        self.hidden_dirs = []
        self.skipped_projects = []
        self.roots_filtered = {}

        scans = self._scan_hosts(self.hosts)
        projects: list[dict] = []
        for scan in scans:
            projects.extend(scan.projects)
            if scan.error:
                self.host_errors[scan.host_id] = scan.error
            for work_dir in scan.hidden_dirs:
                if work_dir not in self.hidden_dirs:
                    self.hidden_dirs.append(work_dir)
            for label in scan.skipped_projects:
                if label not in self.skipped_projects:
                    self.skipped_projects.append(label)
            if scan.roots_filtered:
                self.roots_filtered.setdefault(scan.host_id, []).extend(scan.roots_filtered)
        self.last_scan_error = self.host_errors.get(self.local_host.id, "")
        projects.sort(key=lambda x: (str(x.get("host") or ""), x["name"]))

        # 日志要说清「扫了什么」：面板每 15s 可能就扫一次，所以只在结果/错误有变化时 INFO，
        # 其余降到 DEBUG —— 出问题时 `docker compose logs` 里一定有那几行，平时不刷屏。
        summary = "、".join(
            "%s %d" % (host.id, sum(1 for p in projects if p.get("host") == host.id))
            for host in self.hosts
        )
        signature = tuple((str(p.get("host") or ""), str(p.get("name") or "")) for p in projects)
        changed = signature != self._last_scan_signature
        errors_changed = self.host_errors != self._last_scan_errors
        if changed or errors_changed or not projects:
            level = log.info if (changed or errors_changed) else log.debug
            level(
                "扫描完成（%.1fs）：%s，共 %d 个项目",
                time.monotonic() - started,
                summary,
                len(projects),
            )
            for host_id, error in self.host_errors.items():
                if self._last_scan_errors.get(host_id) != error:
                    log.warning("主机 %s 扫描失败：%s", host_id, error)
            for work_dir in self.hidden_dirs:
                log.warning("compose 项目目录 %s 在容器里不存在（没挂进来？）", work_dir)
        else:
            log.debug(
                "扫描完成（%.1fs）：%s，共 %d 个项目（无变化）",
                time.monotonic() - started,
                summary,
                len(projects),
            )
        self._last_scan_signature = signature
        self._last_scan_errors = dict(self.host_errors)
        return projects

    def _scan_hosts(self, hosts: Sequence[DockerHost]) -> list[HostScan]:
        """并行扫多台主机（单主机时原样直调，省掉线程池的仪式感）。"""
        if len(hosts) <= 1:
            return [self._scan_host_safe(host) for host in hosts]
        workers = max(1, min(HOST_PARALLEL, len(hosts)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="docker-scan") as pool:
            # map 保序：错误与提示的合并顺序稳定，日志/面板不会一次一个样
            return list(pool.map(self._scan_host_safe, hosts))

    def _scan_host_safe(self, host: DockerHost) -> HostScan:
        """扫一台主机并兜住异常（单台主机炸了不能拖垮其它主机）。"""
        try:
            return self._scan_host_sync(host)
        except Exception as exc:
            log.warning("主机 %s 扫描异常：%s", host.id, exc)
            return HostScan(host_id=host.id, error=str(exc))

    def _scan_host_sync(self, host: DockerHost) -> HostScan:
        """扫一台主机；失败原因放进返回值，不抛异常。"""
        scan = HostScan(host_id=host.id)
        if host.error:
            scan.error = host.error
            return scan

        compose_bin = self.get_remote_compose_bin(host)
        if not compose_bin:
            scan.error = (
                "远端未安装 docker compose / docker"
                if host.is_remote
                else "未找到 docker compose / docker-compose 命令"
            )
            return scan

        try:
            result = subprocess.run(
                host.command([*compose_bin, "ls", "-a", "--format", "json"]),
                capture_output=True,
                text=True,
                timeout=SCAN_TIMEOUT,
            )
        except Exception as exc:
            scan.error = str(exc)
            log.warning("主机 %s 的 docker compose ls 起不来：%s", host.id, exc)
            return scan

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            scan.error = explain_exit(result.returncode, detail, host)
            log.warning("主机 %s 的 docker compose ls 失败：%s", host.id, scan.error)
            return scan

        projects: list[dict] = []
        seen_keys: set[str] = set()
        data: list[Any] = []
        if result.stdout.strip():
            try:
                parsed = json.loads(result.stdout)
                data = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                for line in result.stdout.strip().splitlines():
                    if line.strip():
                        try:
                            data.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass

        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("Name", "")
            status = item.get("Status", "")
            config_files = [
                c.strip() for c in (item.get("ConfigFiles", "") or "").split(",") if c.strip()
            ]
            if not name:
                continue
            first_file = config_files[0] if config_files else ""
            work_dir = os.path.dirname(first_file) if first_file else ""
            if host.is_remote:
                # 远端返回的是它自己的路径：不做本地存在性检查，只按可选白名单过滤
                if work_dir and not host.allows(work_dir):
                    log.info("主机 %s 的项目 %s 不在 roots 白名单内，跳过", host.id, name)
                    scan.roots_filtered.append(name)
                    continue
            elif work_dir and not os.path.isdir(work_dir):
                # compose 文件在宿主机有、容器里没有 => 没挂载，单独提示
                if work_dir not in scan.hidden_dirs:
                    scan.hidden_dirs.append(work_dir)
                continue

            unique_key = "%s:%s:%s" % (host.id, name, work_dir)
            if not work_dir:
                # `docker compose ls` 没给 ConfigFiles（`-f -`/stdin 起的项目、label 缺失）：
                # 以前这里直接 continue，项目在面板上「凭空消失」，用户无从排查
                label = "%s/%s" % (host.id, name)
                scan.skipped_projects.append(label)
                log.warning("项目 %s 没有 compose 文件路径（ConfigFiles 为空），无法升级，已跳过", label)
                continue
            if work_dir and unique_key not in seen_keys:
                seen_keys.add(unique_key)
                project = {
                    "name": name,
                    "dir": work_dir,
                    "status": status,
                    "config_files": config_files,
                    "host": host.id,
                    "host_label": host.display,
                }
                cached = self._cached_services(self.services_key(project))
                if cached is None:
                    project["services"] = []
                    project["services_loaded"] = False
                else:
                    project["services"], project["services_source"] = cached
                    project["services_loaded"] = True
                projects.append(project)
        scan.projects = projects
        return scan

    def _fresh_enough(self, now: float, ttl: float, *, allow_empty: bool = True) -> bool:
        if self.projects_cache_time <= 0.0:
            return False
        if not allow_empty and not self.projects_cache:
            return False
        return (now - self.projects_cache_time) < ttl

    async def get_projects(self, force_refresh: bool = False) -> list[dict]:
        """带 TTL 的项目列表；并发调用走同一把锁，避免连点触发多次全量扫描。

        **只合并「在飞」的扫描**：等锁期间别人刚好扫完（而且是在我这次调用**到达之后**
        才扫完的）就复用他的结果。顺序发生的第二次「🔄 强制刷新」照常重扫——
        用户点刷新就是要新数据，不能悄悄给他 3 秒前的缓存。
        """
        now = time.monotonic()
        if not force_refresh and self._fresh_enough(now, self.settings.projects_cache_ttl, allow_empty=False):
            return self.projects_cache

        arrived = time.monotonic()
        async with self.get_scan_lock():
            if self.projects_cache_time >= arrived:
                return self.projects_cache  # 我到达之后有人扫完了（并发刷新/连点）
            if not force_refresh and self._fresh_enough(
                time.monotonic(), self.settings.projects_cache_ttl, allow_empty=False
            ):
                return self.projects_cache

            scanner = self.scan_hook or self.scan_projects_sync
            projects = await asyncio.to_thread(scanner)
            self.projects_cache = list(projects)
            # 记**扫完**的时间：它同时也是「数据有多旧」和上面那个合并判断的依据
            self.projects_cache_time = time.monotonic()
            return self.projects_cache

    def cached_projects(self) -> list[dict]:
        """只读缓存（summary 用：绝不触发 docker / 阻塞）。"""
        return list(self.projects_cache)

    def has_scan(self) -> bool:
        """是否已经完成过一次扫描（用于区分「没扫过」和「扫到 0 个项目」）。"""
        return self.projects_cache_time > 0.0

    def invalidate_cache(self) -> None:
        """使项目扫描缓存立即过期（升级/清理操作后调用）；服务列表一并作废。"""
        self.projects_cache_time = 0.0
        self.services_cache.clear()


DOCKER_SOCKET = "/var/run/docker.sock"


def socket_group_hint(
    path: str = DOCKER_SOCKET,
    *,
    gid: Optional[int] = None,
    groups: Optional[list[int]] = None,
) -> list[str]:
    """权限不够时把「到底该填哪个 GID」直接量出来。

    比 `getent group docker` 靠谱：NAS（busybox）上常常没有 docker 组条目，用户只能瞎猜
    （线上就有人先猜 998、再猜 0，两次都无效）。这里直接对比 socket 的属组和本进程的附加组：
    组已经在了就闭嘴（说明不是组权限问题，别乱指路），是 root:root 就说清 group_add 救不了。

    `gid` / `groups` 允许注入，测试不用碰真实 socket。
    """
    if gid is None:
        try:
            gid = os.stat(path).st_gid
        except OSError:
            return []
    if groups is None:
        if os.geteuid() == 0:
            return []  # root 不受文件模式约束，组权限不是原因，别乱指路
        try:
            groups = sorted(set(os.getgroups()) | {os.getgid()})
        except OSError:  # pragma: no cover - 极少见，仅防御
            groups = []
    if gid == 0:
        return [
            "   实测：容器里 <code>%s</code> 属组是 root（gid=0），<code>group_add</code> 加组救不了——"
            "要么让容器以 root 跑（compose 里加 <code>user: \"0:0\"</code>），"
            "要么改用 docker-socket-proxy。" % esc(path)
        ]
    if gid in groups:
        return []
    return [
        "   实测：容器里 <code>%s</code> 属组 <code>gid=%d</code>，本进程附加组是 <code>%s</code>，不含它。"
        "在 .env 写 <code>DOCKER_GID=%d</code>，再用 "
        "<code>docker compose up -d --force-recreate</code> 重建（<code>restart</code> 不生效）。"
        % (esc(path), gid, ",".join(str(g) for g in groups), gid)
    ]


def common_mount_root(dirs: Sequence[str]) -> Optional[str]:
    """一组「没挂进来」的目录的公共父目录，用来给一条能直接抄的 `-v` 建议。

    太浅（`/`、`/mnt` 这种）就没有意义——挂 `/` 显然不行，返回 None 让调用方退回泛化提示。
    """
    cleaned = [os.path.abspath(str(d)) for d in dirs if d]
    if not cleaned:
        return None
    try:
        root = os.path.commonpath(cleaned)
    except ValueError:  # 相对/绝对混用，或不同盘符（Windows）
        return None
    root = root.rstrip("/") or "/"
    if len([part for part in root.split("/") if part]) < 2:
        return None
    return root


def _local_error_hints(error: str) -> list[str]:
    """本机扫描失败的原因 → 人话（权限 / 连不上 / 其它）。"""
    error = (error or "").strip()
    low = error.lower()
    hints: list[str] = []
    if not error:
        return hints
    if "permission denied" in low:
        hints.append(
            "⚠️ 读不到 Docker：<code>permission denied</code> —— "
            "容器里的 mtbots 用户没有 <code>/var/run/docker.sock</code> 的权限。"
        )
        hints.extend(socket_group_hint())
        hints.append(
            "   兜底：宿主机上 <code>stat -c '%%g' %s</code> 看 socket 属组 GID。" % DOCKER_SOCKET
        )
    elif any(
        mark in low
        for mark in ("cannot connect", "no such file", "connection refused", "is the docker daemon running")
    ):
        hints.append(
            "⚠️ 连不上 Docker 守护进程：确认挂载了 "
            "<code>-v /var/run/docker.sock:/var/run/docker.sock</code>。"
        )
    else:
        hints.append("⚠️ <code>docker compose ls</code> 失败：<code>%s</code>" % esc(error))
    return hints


def _remote_error_hints(host: DockerHost, error: str) -> list[str]:
    """远端主机的失败原因 → 人话（含一条能直接抄的自测命令）。"""
    hints = [
        "⚠️ 主机 <b>%s</b>（%s）：%s" % (esc(host.id), esc(host.display), esc(error or "未知原因"))
    ]
    if host.error and "私钥" in host.error:
        hints.append(
            "   把 bot 主机上的私钥放到 <code>%s</code>（与 <code>data/</code> 同目录，"
            "<code>chmod 600</code>）。" % esc(host.identity)
        )
    elif host.error:
        hints.append("   修好 <code>data/docker-hosts.json</code> 里这台主机的配置后重启。")
    else:
        hints.append(
            "   自测：<code>ssh -p %d -i %s %s docker compose version</code>"
            % (host.port, esc(host.identity), esc(host.target))
        )
    return hints


def scan_hint(state: DockerState, *, include_compose: bool = True) -> list[str]:
    """扫描不到项目时的可操作提示（把权限 / 挂载 / 缺命令 / 远端连不上分开说）。

    以前这几种情况都只表现成一句「暂未检测到任何 Docker Compose 项目」，只能靠猜。
    这里把 :attr:`DockerState.host_errors`（逐主机）、:attr:`DockerState.last_scan_error` 与
    :attr:`DockerState.hidden_dirs` 翻成人话。
    """
    hints: list[str] = []
    if include_compose and state.compose_bin is not None and not state.compose_bin:
        hints.append("⚠️ 容器里没有 <code>docker compose</code> / <code>docker-compose</code> 命令。")

    hints.extend(state.host_notes)

    errors = dict(getattr(state, "host_errors", {}) or {})
    if errors:
        for host_id, error in errors.items():
            host = state.host_by_id(host_id)
            if host is not None and host.is_remote:
                hints.extend(_remote_error_hints(host, error))
            else:
                hints.extend(_local_error_hints(error))
    else:
        hints.extend(_local_error_hints(state.last_scan_error))

    skipped = list(getattr(state, "skipped_projects", []) or [])
    if skipped:
        shown = "、".join(skipped[:3]) + (" 等 %d 个" % len(skipped) if len(skipped) > 3 else "")
        hints.append(
            "ℹ️ 有 %d 个项目没拿到 compose 文件路径（<code>docker compose ls</code> 的 ConfigFiles 为空），"
            "面板里不显示、也无法升级：<code>%s</code>" % (len(skipped), esc(shown))
        )

    for host_id, names in (getattr(state, "roots_filtered", {}) or {}).items():
        host = state.host_by_id(host_id)
        shown = "、".join(names[:3]) + (" 等 %d 个" % len(names) if len(names) > 3 else "")
        hints.append(
            "ℹ️ 主机 <b>%s</b> 有 %d 个项目被 <code>roots</code> 白名单挡掉：<code>%s</code>"
            % (esc(host.display if host is not None else host_id), len(names), esc(shown))
        )

    if state.hidden_dirs:
        dirs = state.hidden_dirs
        shown = "、".join(dirs[:2]) + (" 等 %d 个" % len(dirs) if len(dirs) > 2 else "")
        hints.append(
            "ℹ️ 有 %d 个 compose 项目扫到了，但它们的目录在容器里不存在：<code>%s</code>"
            % (len(dirs), esc(shown))
        )
        root = common_mount_root(dirs)
        if root:
            # 给出能直接抄的一行：这些目录都在同一个根下面时，挂一次就够
            hints.append(
                "   修：compose 命令按宿主机的原路径执行，所以要按相同路径挂进来——"
                "在 compose 的 volumes 里加 <code>-v %s:%s</code>，再重建容器。" % (esc(root), esc(root))
            )
        else:
            hints.append(
                "   修：把宿主机目录按相同路径挂进容器，例如 <code>-v /opt/stacks:/opt/stacks</code>。"
            )
    return hints


# ==================== 只读 docker 查询 ====================
async def run_docker_capture(
    state: DockerState,
    *args: str,
    timeout: Optional[float] = None,
    host: Optional[DockerHost] = None,
) -> tuple[int, str]:
    """执行只读 Docker 查询，返回退出码和合并后的输出（在线程里跑，不阻塞事件循环）。

    `host` 指定在哪台机器上跑（远端会被包成 ssh 调用）；不传 = 第一台（单机行为不变）。
    """
    budget = float(timeout if timeout is not None else state.settings.command_timeout)
    target = host or state.hosts[0]

    def _run() -> tuple[int, str]:
        try:
            proc = subprocess.run(
                target.command(["docker", *args]),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=budget,
            )
        except Exception as exc:  # 超时 / 找不到 docker 都按失败返回
            return 1, str(exc)
        return int(proc.returncode or 0), proc.stdout or ""

    return await asyncio.to_thread(_run)


async def scan_prune_candidates(
    state: DockerState, prune_all: bool, host: Optional[DockerHost] = None
) -> tuple[bool, str, str]:
    """扫描与 prune 命令语义一致的候选镜像，禁止把全部镜像误报为待删除。

    返回 (是否成功, 候选快照, 错误输出)。
    """
    if not prune_all:
        rc, output = await run_docker_capture(
            state,
            "image",
            "ls",
            "--filter",
            "dangling=true",
            "--no-trunc",
            "--format",
            IMAGE_FORMAT,
            host=host,
        )
        if rc != 0:
            return False, "", output
        return True, output.strip(), ""

    rc, image_output = await run_docker_capture(
        state, "image", "ls", "-a", "--no-trunc", "--format", IMAGE_FORMAT, host=host
    )
    if rc != 0:
        return False, "", image_output

    rc, container_output = await run_docker_capture(state, "ps", "-aq", host=host)
    if rc != 0:
        return False, "", container_output

    container_ids = [line.strip() for line in container_output.splitlines() if line.strip()]
    referenced_ids: set[str] = set()
    if container_ids:
        rc, inspect_output = await run_docker_capture(
            state, "inspect", "--format", "{{.Image}}", *container_ids, host=host
        )
        if rc != 0:
            return False, "", inspect_output
        referenced_ids = {line.strip() for line in inspect_output.splitlines() if line.strip()}

    candidates = select_unused_images(image_output.splitlines(), referenced_ids)
    return True, "\n".join(candidates), ""


async def dump_container_status(
    state: DockerState, host: Optional[DockerHost] = None
) -> tuple[bool, str]:
    """`docker ps -a` 容器状态速览（/d_status）。"""
    rc, output = await run_docker_capture(
        state,
        "ps",
        "-a",
        "--format",
        "table {{.Names}}\t{{.Status}}\t{{.Ports}}",
        host=host,
    )
    return rc == 0, output.strip()


def make_state(settings: DockerSettings) -> DockerState:
    """按配置建 DockerState（`register()` 与 `--health` 共用，保证两处看到同样的主机清单）。"""
    hosts, notes = load_hosts(getattr(settings, "hosts_file", None))
    for note in notes:
        log.warning("主机清单提示：%s", note)
    return DockerState(settings, hosts=hosts, host_notes=notes)


# ==================== 流式执行 ====================
async def _wait_process(process: Any, output_lines: "deque[str]") -> int:
    """获取子进程返回码，带超时兜底。

    个别环境下 asyncio 可能收不到子进程退出通知，导致 process.wait()
    永久挂起（进而卡死全局任务锁）。这里用短超时 + kill 重试兜底，
    仍无法获得退出码时按输出内容推断结果。
    """
    try:
        return await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        pass

    try:
        stop_process_tree(process)
        try:
            return await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            return -9
    except ProcessLookupError:
        pass

    rc = process.returncode
    if rc is not None:
        return rc
    # 进程实际已退出（管道 EOF）但退出码丢失：按输出中是否有错误关键字推断
    snippet = "\n".join(output_lines)
    return 1 if re.search(r"(?i)\b(error|failed|denied|no such file)\b", snippet) else 0


async def run_command_with_feedback(
    state: DockerState,
    message: Any,
    cmd: Sequence[str],
    *,
    cwd: Optional[str] = None,
    title: str = "执行中",
    progress_pct: int = 50,
    task_id: Optional[str] = None,
    on_progress: Optional[ProgressCallback] = None,
    delete_on_success: bool = False,
    out: Optional[list[str]] = None,
    host: Optional[DockerHost] = None,
) -> bool:
    """执行一条 compose / docker 命令，并把逐层进度原地刷新到同一条状态消息上。

    最终消息只保留关键结果行（过滤 pull 的逐层噪音），HTML 编辑失败自动降级纯文本。

    `delete_on_success=True` 时，命令**成功**后把这条执行消息删掉：多步任务里每一步的
    「✅ 拉取新镜像 - mt 完成」只是过程，留一串会盖住面板的结论。失败/取消/超时一律保留，
    因为那几条输出就是排错依据（先改成「❌ 失败」再删，删不掉也不会留个假进度）。

    `out` 是可选的结果回传（列表尾插一条过滤后的输出），给「删掉执行消息但结论还得留着」
    的场景用，例如镜像清理要把 `Total reclaimed space` 抄进收尾面板。

    `host` 指定在哪台机器上跑：远端主机的命令**不能带本地 cwd**（本地没那个目录），
    失败时再按 ssh 的退出码补一句人话提示。
    """
    start_time = time.time()
    safe_title = esc(title)
    safe_cmd = esc(" ".join(cmd))
    timeout = state.settings.command_timeout
    if host is not None:
        cwd = host.cwd(cwd or "")

    cancel_markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🛑 中断执行",
                    callback_data=cb_simple("d", "task_cancel", task_id or "-"),
                )
            ]
        ]
    )

    status_msg = await message.reply_text(
        "⚙️ <b>%s</b> [%s]\n⏱ <b>已用时：</b>0.0s\n<code>%s</code>"
        % (safe_title, progress_bar(progress_pct), safe_cmd),
        reply_markup=cancel_markup,
        parse_mode="HTML",
    )

    output_lines: "deque[str]" = deque(maxlen=OUTPUT_BUFFER)
    process: Optional[asyncio.subprocess.Process] = None

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        state.current_process = process

        last_update_time = time.time()
        last_reported = -1

        async def read_stream() -> None:
            nonlocal last_update_time, last_reported
            partial = ""
            while True:
                if state.cancel_requested:
                    stop_process_tree(process)
                    break

                chunk = await process.stdout.read(STREAM_READ_SIZE)
                if not chunk:
                    break

                partial += chunk.decode("utf-8", errors="replace")

                while "\n" in partial:
                    line, partial = partial.split("\n", 1)
                    line = line.rstrip("\r")
                    if line.strip():
                        output_lines.append(line)

                # 进度模式用 \r 原地刷新同一行，只保留最后一次进度
                if "\r" in partial:
                    partial = partial.rsplit("\r", 1)[-1]

                now = time.time()
                if now - last_update_time >= STREAM_EDIT_INTERVAL:
                    elapsed = round(now - start_time, 1)
                    dyn_pct = min(progress_pct, max(5, 5 + int(elapsed * (progress_pct - 5) / 45)))
                    preview_lines = list(output_lines)[-PREVIEW_LINES:]
                    if partial.strip():
                        preview_lines.append(partial.strip())
                    preview = redact("\n".join(preview_lines))
                    edited = await edit_html_safe(
                        status_msg,
                        "⚙️ <b>%s</b> [%s]\n⏱ <b>已用时：</b>%ss\n<code>%s</code>"
                        % (
                            safe_title,
                            progress_bar(dyn_pct),
                            elapsed,
                            esc(preview[-PREVIEW_CHARS:]),
                        ),
                        fallback="⚙️ %s [%s]\n已用时：%ss\n%s"
                        % (title, progress_bar(dyn_pct), elapsed, preview[-PREVIEW_CHARS:]),
                    )
                    if edited:
                        last_update_time = now
                    if on_progress is not None and dyn_pct != last_reported:
                        last_reported = dyn_pct
                        try:
                            on_progress(dyn_pct, preview[-120:])
                        except Exception:  # 进度回调不能影响命令执行
                            pass

        await asyncio.wait_for(read_stream(), timeout=timeout)
        returncode = await _wait_process(process, output_lines)

        elapsed = round(time.time() - start_time, 1)
        # 最终消息只保留关键结果行，过滤逐层进度噪音
        full_output = "\n".join(filter_pull_noise(list(output_lines)[-FINAL_LINES:]))
        if out is not None:
            out.append(full_output)
        # 进聊天 / 任务详情的命令输出先脱敏再转义：esc() 只挡 HTML，挡不住密码、
        # Token、带认证信息的 URL 或镜像引用（日志过滤器管不到这些消息出口）。
        safe_full_output = esc(redact(full_output)[-PREVIEW_CHARS:])

        if state.cancel_requested:
            await edit_html_safe(
                status_msg,
                "🛑 <b>%s 已取消</b>\n⏱ <b>已用时：</b>%ss\n<code>%s</code>"
                % (safe_title, elapsed, safe_full_output),
            )
            return False

        if returncode == 0:
            await edit_html_safe(
                status_msg,
                "✅ <b>%s 完成</b> [%s]\n⏱ <b>总耗时：</b>%ss\n<code>%s</code>"
                % (safe_title, progress_bar(100), elapsed, safe_full_output),
            )
            if delete_on_success:
                await delete_message_quietly(status_msg)
            return True

        hint = ""
        if host is not None and host.is_remote:
            hint = "\n💡 %s" % esc(redact(explain_exit(int(returncode), full_output, host)))
        await edit_html_safe(
            status_msg,
            "❌ <b>%s 失败 (Code %s)</b>%s\n⏱ <b>耗时：</b>%ss\n<code>%s</code>"
            % (safe_title, returncode, hint, elapsed, safe_full_output),
        )
        return False

    except asyncio.TimeoutError:
        if process is not None:
            try:
                stop_process_tree(process)
                await asyncio.wait_for(process.wait(), timeout=10)
            except Exception:
                pass
        await edit_html_safe(
            status_msg,
            "⏰ <b>%s 超时中断</b>\n单条指令耗时超过 %d 秒，已强行终止。" % (safe_title, timeout),
        )
        return False

    except Exception as exc:
        if process is not None:
            stop_process_tree(process)
        await edit_html_safe(status_msg, "❌ 执行发生异常: %s" % esc(redact(str(exc))))
        return False

    finally:
        state.current_process = None


__all__ = [
    "DockerState",
    "ProgressCallback",
    "PULL_FINAL_NOISE_RE",
    "delete_message_quietly",
    "edit_html_safe",
    "dump_container_status",
    "filter_pull_noise",
    "format_prune_snapshot",
    "is_pull_noise",
    "make_state",
    "normalize_image_id",
    "ordered_projects",
    "paginate_projects",
    "run_command_with_feedback",
    "run_docker_capture",
    "scan_prune_candidates",
    "select_unused_images",
    "sort_projects_for_display",
    "stop_process_tree",
]
