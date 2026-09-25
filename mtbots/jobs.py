"""跨模块任务中心：所有长任务都注册到这里，`/jobs` 一屏看完全部。

合并前三个 bot 各跑各的、完成通知各发各的；合并后用同一个 Job 模型
（`id / feature / title / status / progress / cancel_event`），
完成推送统一带模块标签 `🐳 / 🎬 / 🤖`，一眼看出是哪条线的通知。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from .text import esc, humanize_delta, humanize_duration, progress_bar

log = logging.getLogger("mtbots.jobs")

STATUS_ICON = {
    "running": "⏳",
    "done": "✅",
    "failed": "❌",
    "cancelled": "🛑",
}

RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = (DONE, FAILED, CANCELLED)


@dataclass
class Job:
    module: str
    title: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    status: str = RUNNING
    detail: str = ""
    progress: Optional[int] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    cancel: Optional[Callable[[], None]] = None
    chat_id: Optional[int] = None

    @property
    def running(self) -> bool:
        return self.status == RUNNING

    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)

    def line(self, icon: str = "•") -> str:
        mark = STATUS_ICON.get(self.status, "•")
        head = "%s %s %s" % (mark, icon, esc(self.title))
        if self.status == RUNNING:
            if self.progress is not None:
                pct = max(0, min(100, int(self.progress)))
                head += "  %s %d%%" % (progress_bar(pct, 8), pct)
            else:
                head += "  ⏳ %s" % humanize_duration(self.elapsed())
        else:
            when = humanize_delta(time.time() - (self.finished_at or time.time()))
            head += " · %s" % when
        if self.detail and self.status != RUNNING:
            head += "\n    %s" % esc(self.detail)
        return head


class JobCenter:
    """内存任务表（重启即清空，这符合「当前在跑什么」的语义）。"""

    def __init__(self, keep_finished: int = 20):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._keep_finished = max(1, keep_finished)

    # ---------- 写 ----------
    def add(
        self,
        module: str,
        title: str,
        *,
        detail: str = "",
        progress: Optional[int] = None,
        cancel: Optional[Callable[[], None]] = None,
        chat_id: Optional[int] = None,
    ) -> Job:
        job = Job(
            module=module,
            title=title,
            detail=detail,
            progress=progress,
            cancel=cancel,
            chat_id=chat_id,
        )
        self._jobs[job.id] = job
        self._order.append(job.id)
        self._prune()
        log.info("任务开始 [%s] %s (id=%s)", module, title, job.id)
        return job

    def update(self, job: Job, *, detail: Optional[str] = None, progress: Optional[int] = None) -> None:
        if detail is not None:
            job.detail = detail
        if progress is not None:
            job.progress = max(0, min(100, int(progress)))

    def finish(self, job: Job, status: str = DONE, detail: str = "") -> Job:
        if job.status in TERMINAL and status == DONE:
            return job
        job.status = status
        if detail:
            job.detail = detail
        job.finished_at = time.time()
        job.progress = 100 if status == DONE else job.progress
        log.info(
            "任务结束 [%s] %s -> %s（耗时 %.1fs）", job.module, job.title, status, job.elapsed()
        )
        return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or not job.running:
            return False
        try:
            if job.cancel is not None:
                job.cancel()
        except Exception as exc:  # 取消回调失败也要把任务标记为已取消
            log.warning("取消回调失败 job=%s: %s", job_id, exc)
        self.finish(job, CANCELLED, "已按用户请求取消")
        return True

    # ---------- 读 ----------
    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def running(self, module: Optional[str] = None) -> list[Job]:
        items = [self._jobs[i] for i in self._order if i in self._jobs and self._jobs[i].running]
        if module:
            items = [j for j in items if j.module == module]
        return items

    def recent(self, limit: int = 5, module: Optional[str] = None) -> list[Job]:
        items = [self._jobs[i] for i in self._order if i in self._jobs and not self._jobs[i].running]
        if module:
            items = [j for j in items if j.module == module]
        items.reverse()
        return items[:limit]

    def all_jobs(self) -> list[Job]:
        return [self._jobs[i] for i in self._order if i in self._jobs]

    def has_running(self) -> bool:
        return bool(self.running())

    # ---------- 渲染 ----------
    def render(self, icons: Optional[dict[str, str]] = None) -> str:
        icons = icons or {}
        running = self.running()
        recent = self.recent(5)
        if not running and not recent:
            return "暂时没有任务。\n长任务（升级 / 清理 / LitePan 触发回执）会自动出现在这里。"
        lines: list[str] = []
        if running:
            lines.append("<b>进行中（%d）</b>" % len(running))
            for job in running:
                lines.append(job.line(icons.get(job.module, "•")))
            lines.append("")
        if recent:
            lines.append("<b>最近完成</b>")
            for job in recent:
                lines.append(job.line(icons.get(job.module, "•")))
        return "\n".join(lines).strip()

    async def announce(self, bot, chat_id: int, job: Job, *, actions=None) -> None:
        """长任务结束后推一条带模块标签的卡片（可带跨模块下一步按钮）。"""
        from telegram.constants import ParseMode

        icon = {"docker": "🐳", "litepan": "🎬", "cline": "🤖"}.get(job.module, "•")
        mark = STATUS_ICON.get(job.status, "•")
        when = time.strftime("%H:%M", time.localtime(job.finished_at or time.time()))
        text = "%s %s <b>%s</b> · %s（%s）" % (mark, icon, esc(job.title), when, humanize_duration(job.elapsed()))
        if job.detail:
            text += "\n%s" % esc(job.detail)
        try:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=actions)
        except Exception as exc:  # 推送失败不影响任务本身
            log.warning("任务完成推送失败 chat=%s job=%s: %s", chat_id, job.id, exc)

    # ---------- 内部 ----------
    def _prune(self) -> None:
        finished = [i for i in self._order if i in self._jobs and not self._jobs[i].running]
        excess = len(finished) - self._keep_finished
        for job_id in finished[: max(0, excess)]:
            self._jobs.pop(job_id, None)
            try:
                self._order.remove(job_id)
            except ValueError:
                pass


__all__ = [
    "Job",
    "JobCenter",
    "RUNNING",
    "DONE",
    "FAILED",
    "CANCELLED",
    "TERMINAL",
    "STATUS_ICON",
]
