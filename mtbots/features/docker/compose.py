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
from typing import Any, Callable, Iterable, Optional, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ...panels import cb_simple
from ...text import esc, progress_bar

from .config import DockerSettings

log = logging.getLogger("mtbots.docker")

#: compose 探测 / 服务列表 / 项目扫描的子进程超时（秒）
PROBE_TIMEOUT = 10
SERVICES_TIMEOUT = 15
SCAN_TIMEOUT = 30

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
    """清理快照：只保留尾部 limit 个字符并做 HTML 转义（长快照不撑爆消息）。"""
    return esc((text or "")[-limit:])


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


# ==================== 全局状态 ====================
class DockerState:
    """LDMG 全部可变全局状态的宿主（每个 register() 建一个）。"""

    def __init__(self, settings: DockerSettings):
        self.settings = settings
        self.exec_lock: Optional[asyncio.Lock] = None
        self.current_process: Optional[asyncio.subprocess.Process] = None
        self.cancel_requested: bool = False
        self.current_task: Optional[str] = None
        #: 只缓存探测成功的结果；失败置 [] 以便 Docker/Compose 稍后启动时重新探测
        self.compose_bin: Optional[list[str]] = None
        self.projects_cache: list[dict] = []
        self.projects_cache_time: float = 0.0
        #: 测试/嵌入用：替换真实扫描（返回项目列表）
        self.scan_hook: Optional[Callable[[], list[dict]]] = None
        #: 最近一次扫描的失败原因（空 = 成功）。以前这里失败是静默的，
        #: 结果「权限不足 / 目录没挂载」都表现成一句「暂未检测到任何项目」，没法排查。
        self.last_scan_error: str = ""
        #: 扫到了、但 compose 目录在容器里不存在的项目目录（宿主机路径没挂进来）
        self.hidden_dirs: list[str] = []

    # ---------- 任务锁 ----------
    def get_lock(self) -> asyncio.Lock:
        if self.exec_lock is None:
            self.exec_lock = asyncio.Lock()
        return self.exec_lock

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

    def build_compose_cmd(self, project: dict, *args: str) -> list[str]:
        """按项目生成 compose 命令（携带完整 -f 文件列表，兼容多 compose 文件项目）。"""
        compose_bin = self.get_compose_bin()
        if not compose_bin:
            raise RuntimeError("未检测到 docker compose / docker-compose 命令")
        cmd = list(compose_bin)
        for config_file in project.get("config_files") or []:
            cmd += ["-f", config_file]
        cmd += list(args)
        return cmd

    def get_project_services(self, work_dir: str, config_files: Sequence[str]) -> list[str]:
        """获取项目的服务定义。"""
        compose_bin = self.get_compose_bin()
        if not compose_bin:
            return []
        try:
            cmd = list(compose_bin)
            for config_file in config_files:
                cmd += ["-f", config_file]
            cmd += ["config", "--services"]
            result = subprocess.run(
                cmd,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=SERVICES_TIMEOUT,
            )
            if result.returncode == 0:
                return [s.strip() for s in result.stdout.strip().splitlines() if s.strip()]
        except Exception as exc:
            log.warning("获取 %s 服务列表失败: %s", work_dir, exc)
        return []

    # ---------- 项目扫描 ----------
    def scan_projects_sync(self) -> list[dict]:
        """`docker compose ls -a --format json` 扫描（同步，交给 to_thread 跑）。

        失败不再静默：`last_scan_error` 记下原因，`hidden_dirs` 记下「扫到了、但 compose
        目录在容器里不存在」的项目，面板和 `--health` 据此给出可操作提示。
        """
        projects: list[dict] = []
        seen_keys: set[str] = set()
        self.last_scan_error = ""
        self.hidden_dirs = []
        compose_bin = self.get_compose_bin()
        if not compose_bin:
            self.last_scan_error = "未找到 docker compose / docker-compose 命令"
            return projects

        try:
            result = subprocess.run(
                [*compose_bin, "ls", "-a", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=SCAN_TIMEOUT,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                self.last_scan_error = detail.splitlines()[0] if detail else "退出码 %d" % result.returncode
                log.warning("docker compose ls 失败：%s", self.last_scan_error)
                return projects

            if result.stdout.strip():
                try:
                    data = json.loads(result.stdout)
                    if isinstance(data, dict):
                        data = [data]
                except json.JSONDecodeError:
                    data = []
                    for line in result.stdout.strip().splitlines():
                        if line.strip():
                            try:
                                data.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass

                for item in data:
                    name = item.get("Name", "")
                    status = item.get("Status", "")
                    config_files = [
                        c.strip()
                        for c in (item.get("ConfigFiles", "") or "").split(",")
                        if c.strip()
                    ]
                    if not name:
                        continue
                    first_file = config_files[0] if config_files else ""
                    work_dir = os.path.dirname(first_file) if first_file else ""
                    if work_dir and not os.path.isdir(work_dir):
                        # compose 文件在宿主机有、容器里没有 => 没挂载，单独提示
                        if work_dir not in self.hidden_dirs:
                            self.hidden_dirs.append(work_dir)
                        continue

                    unique_key = "%s:%s" % (name, work_dir)
                    if work_dir and unique_key not in seen_keys:
                        seen_keys.add(unique_key)
                        services = self.get_project_services(work_dir, config_files)
                        projects.append(
                            {
                                "name": name,
                                "dir": work_dir,
                                "status": status,
                                "services": services,
                                "config_files": config_files,
                            }
                        )
        except Exception as exc:
            self.last_scan_error = str(exc)
            log.warning("docker compose ls 扫描失败: %s", exc)

        projects.sort(key=lambda x: x["name"])
        return projects

    async def get_projects(self, force_refresh: bool = False) -> list[dict]:
        """带 TTL 的项目列表，避免每次点击按钮都全量扫描。"""
        now = time.monotonic()
        if (
            not force_refresh
            and self.projects_cache
            and (now - self.projects_cache_time) < self.settings.projects_cache_ttl
        ):
            return self.projects_cache

        scanner = self.scan_hook or self.scan_projects_sync
        projects = await asyncio.to_thread(scanner)
        self.projects_cache = list(projects)
        self.projects_cache_time = now
        return self.projects_cache

    def cached_projects(self) -> list[dict]:
        """只读缓存（summary 用：绝不触发 docker / 阻塞）。"""
        return list(self.projects_cache)

    def has_scan(self) -> bool:
        """是否已经完成过一次扫描（用于区分「没扫过」和「扫到 0 个项目」）。"""
        return self.projects_cache_time > 0.0

    def invalidate_cache(self) -> None:
        """使项目扫描缓存立即过期（升级/清理操作后调用）。"""
        self.projects_cache_time = 0.0


def scan_hint(state: DockerState, *, include_compose: bool = True) -> list[str]:
    """扫描不到项目时的可操作提示（把权限 / 挂载 / 缺命令三种原因分开说）。

    以前这三种情况都只表现成一句「暂未检测到任何 Docker Compose 项目」，只能靠猜。
    这里把 :attr:`DockerState.last_scan_error` 和 :attr:`DockerState.hidden_dirs` 翻成人话。
    """
    hints: list[str] = []
    if include_compose and state.compose_bin is not None and not state.compose_bin:
        hints.append("⚠️ 容器里没有 <code>docker compose</code> / <code>docker-compose</code> 命令。")

    error = (state.last_scan_error or "").strip()
    low = error.lower()
    if error:
        if "permission denied" in low:
            hints.append(
                "⚠️ 读不到 Docker：<code>permission denied</code> —— 容器里的 mtbots 用户不在宿主机 docker 组。"
            )
            hints.append(
                "   修：<code>export DOCKER_GID=$(getent group docker | cut -d: -f3)</code> "
                "后 <code>docker compose up -d</code> 重建。"
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

    if state.hidden_dirs:
        shown = "、".join(state.hidden_dirs[:2])
        hints.append(
            "ℹ️ 有 %d 个 compose 项目扫到了，但它们的目录在容器里不存在：<code>%s</code>"
            % (len(state.hidden_dirs), esc(shown))
        )
        hints.append(
            "   修：把宿主机目录按相同路径挂进容器，例如 <code>-v /opt/stacks:/opt/stacks</code>。"
        )
    return hints


# ==================== 只读 docker 查询 ====================
async def run_docker_capture(
    state: DockerState, *args: str, timeout: Optional[float] = None
) -> tuple[int, str]:
    """执行只读 Docker 查询，返回退出码和合并后的输出（在线程里跑，不阻塞事件循环）。"""
    budget = float(timeout if timeout is not None else state.settings.command_timeout)

    def _run() -> tuple[int, str]:
        try:
            proc = subprocess.run(
                ["docker", *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=budget,
            )
        except Exception as exc:  # 超时 / 找不到 docker 都按失败返回
            return 1, str(exc)
        return int(proc.returncode or 0), proc.stdout or ""

    return await asyncio.to_thread(_run)


async def scan_prune_candidates(state: DockerState, prune_all: bool) -> tuple[bool, str, str]:
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
        )
        if rc != 0:
            return False, "", output
        return True, output.strip(), ""

    rc, image_output = await run_docker_capture(
        state, "image", "ls", "-a", "--no-trunc", "--format", IMAGE_FORMAT
    )
    if rc != 0:
        return False, "", image_output

    rc, container_output = await run_docker_capture(state, "ps", "-aq")
    if rc != 0:
        return False, "", container_output

    container_ids = [line.strip() for line in container_output.splitlines() if line.strip()]
    referenced_ids: set[str] = set()
    if container_ids:
        rc, inspect_output = await run_docker_capture(
            state, "inspect", "--format", "{{.Image}}", *container_ids
        )
        if rc != 0:
            return False, "", inspect_output
        referenced_ids = {line.strip() for line in inspect_output.splitlines() if line.strip()}

    candidates = select_unused_images(image_output.splitlines(), referenced_ids)
    return True, "\n".join(candidates), ""


async def dump_container_status(state: DockerState) -> tuple[bool, str]:
    """`docker ps -a` 容器状态速览（/d_status）。"""
    rc, output = await run_docker_capture(
        state, "ps", "-a", "--format", "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    )
    return rc == 0, output.strip()


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
) -> bool:
    """执行一条 compose / docker 命令，并把逐层进度原地刷新到同一条状态消息上。

    最终消息只保留关键结果行（过滤 pull 的逐层噪音），HTML 编辑失败自动降级纯文本。
    """
    start_time = time.time()
    safe_title = esc(title)
    safe_cmd = esc(" ".join(cmd))
    timeout = state.settings.command_timeout

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
                    preview = "\n".join(preview_lines)
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
        safe_full_output = esc(full_output[-PREVIEW_CHARS:])

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
            return True

        await edit_html_safe(
            status_msg,
            "❌ <b>%s 失败 (Code %s)</b>\n⏱ <b>耗时：</b>%ss\n<code>%s</code>"
            % (safe_title, returncode, elapsed, safe_full_output),
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
        await edit_html_safe(status_msg, "❌ 执行发生异常: %s" % esc(str(exc)))
        return False

    finally:
        state.current_process = None


__all__ = [
    "DockerState",
    "ProgressCallback",
    "PULL_FINAL_NOISE_RE",
    "edit_html_safe",
    "dump_container_status",
    "filter_pull_noise",
    "format_prune_snapshot",
    "is_pull_noise",
    "normalize_image_id",
    "paginate_projects",
    "run_command_with_feedback",
    "run_docker_capture",
    "scan_prune_candidates",
    "select_unused_images",
    "sort_projects_for_display",
    "stop_process_tree",
]
