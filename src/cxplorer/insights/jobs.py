"""Bounded application-lifetime jobs; reloads intentionally discard unfinished work."""

import asyncio
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from cxplorer.insights.cache import MAX_REPORT_BYTES
from cxplorer.insights.errors import InsightError
from cxplorer.insights.schemas import (
    AcceptedReport,
    CachedReport,
    InsightRequest,
    SourceOutcome,
)

logger = logging.getLogger(__name__)
Progress = Callable[[str, str], None]
Runner = Callable[[str, InsightRequest, Progress], Awaitable[AcceptedReport]]
FINISHED_STATES = frozenset({"completed", "partial", "failed", "cancelled"})


@dataclass
class Job:
    id: str
    owner_hash: str
    request: InsightRequest
    submission_id: str
    input_fingerprint: str
    created_at: datetime
    state: str = "queued"
    stage: str = "queued"
    message: str = "Waiting for generation capacity."
    error: str | None = None
    error_code: str | None = None
    finished_at: datetime | None = None
    source_outcomes: list[SourceOutcome] = field(default_factory=list)


@dataclass
class StoredReport:
    owner_hash: str
    report: AcceptedReport
    input_fingerprint: str
    expires_at: datetime
    size: int


class JobManager:
    def __init__(
        self,
        runner: Runner,
        *,
        max_running: int = 2,
        max_queued: int = 4,
        max_per_owner: int = 1,
        timeout_seconds: float = 900,
        retention_seconds: int = 3600,
        retained_limit: int = 16,
        max_retained_bytes: int = 64 * 1024 * 1024,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        forget: Callable[[str], None] | None = None,
        can_retry: Callable[[str], bool] | None = None,
    ) -> None:
        self._runner = runner
        self._semaphore = asyncio.Semaphore(max_running)
        self._max_pending = max_running + max_queued
        self._max_per_owner = max_per_owner
        self._timeout = timeout_seconds
        self._retention = timedelta(seconds=retention_seconds)
        self._retained_limit = retained_limit
        self._max_retained_bytes = max_retained_bytes
        self._clock = clock
        self._forget = forget
        self._can_retry = can_retry
        self._jobs: dict[str, Job] = {}
        self._reports: dict[str, StoredReport] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closing = False

    def _elapsed(self, job: Job) -> float:
        return max((self._clock() - job.created_at).total_seconds(), 0.0)

    @staticmethod
    def _safe_stage(stage: str) -> str:
        return stage if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", stage) else "unknown"

    def _purge(self) -> None:
        now = self._clock()
        self._reports = {
            key: stored for key, stored in self._reports.items() if stored.expires_at > now
        }
        expired_jobs = {
            key
            for key, job in self._jobs.items()
            if job.finished_at is not None and job.finished_at + self._retention <= now
        }
        self._jobs = {
            key: job
            for key, job in self._jobs.items()
            if job.finished_at is None or job.finished_at + self._retention > now
        }
        if self._forget is not None:
            for report_id in expired_jobs:
                self._forget(report_id)

    def get_job(self, report_id: str, owner_hash: str) -> Job | None:
        self._purge()
        job = self._jobs.get(report_id)
        return job if job is not None and job.owner_hash == owner_hash else None

    def get_report(self, report_id: str, owner_hash: str) -> StoredReport | None:
        self._purge()
        stored = self._reports.get(report_id)
        return stored if stored is not None and stored.owner_hash == owner_hash else None

    def _reserve_result(self, *, additional: int = 1) -> None:
        active = sum(job.state not in FINISHED_STATES for job in self._jobs.values())
        if (
            len(set(self._reports) | set(self._jobs)) + additional > self._retained_limit
            or sum(item.size for item in self._reports.values())
            + (active + additional) * MAX_REPORT_BYTES
            > self._max_retained_bytes
        ):
            raise InsightError(
                "capacity_exhausted",
                "Temporary report capacity is full. Delete an existing copy or try again later.",
            )

    def submit(
        self,
        owner_hash: str,
        request: InsightRequest,
        submission_id: str,
        input_fingerprint: str,
    ) -> Job:
        self._purge()
        if self._closing:
            raise InsightError(
                "shutting_down", "Generation is unavailable while the server restarts."
            )
        for job in self._jobs.values():
            if job.owner_hash == owner_hash and job.submission_id == submission_id:
                if job.input_fingerprint != input_fingerprint:
                    raise InsightError(
                        "submission_conflict",
                        "This submission was already used for different inputs. Refresh the workspace.",
                    )
                return job
        active = [job for job in self._jobs.values() if job.state not in FINISHED_STATES]
        if sum(job.owner_hash == owner_hash for job in active) >= self._max_per_owner:
            raise InsightError(
                "active_job",
                "Your existing generation is still running. Finish or cancel it before starting another.",
            )
        if len(active) >= self._max_pending:
            raise InsightError("queue_full", "Generation is busy. Please try again shortly.")
        self._reserve_result()
        job = Job(
            id=secrets.token_hex(16),
            owner_hash=owner_hash,
            request=request.model_copy(deep=True),
            submission_id=submission_id,
            input_fingerprint=input_fingerprint,
            created_at=self._clock(),
        )
        self._jobs[job.id] = job
        task = asyncio.create_task(self._run(job), name=f"insights-{job.id}")
        self._tasks[job.id] = task
        task.add_done_callback(lambda finished: self._task_finished(job, finished))
        logger.info("Insight job %s queued", job.id)
        return job

    def _task_finished(self, job: Job, task: asyncio.Task[None]) -> None:
        if self._tasks.get(job.id) is task:
            self._tasks.pop(job.id, None)
        if task.cancelled():
            if job.state not in FINISHED_STATES:
                job.state = "cancelled"
                job.message = "Generation was interrupted."
                job.finished_at = self._clock()
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Insight job %s failed unexpectedly: stage=%s error_type=%s duration_seconds=%.3f",
                job.id,
                self._safe_stage(job.stage),
                type(error).__name__,
                self._elapsed(job),
            )
            job.state = "failed"
            job.error_code = "unexpected_error"
            job.error = "An unexpected error stopped generation. Please try again later."
            job.message = job.error
            job.finished_at = self._clock()

    async def _run(self, job: Job) -> None:
        active_stage: str | None = None
        stage_started = 0.0

        def finish_stage(status: str, error_code: str = "none") -> None:
            nonlocal active_stage
            if active_stage is None:
                return
            log = logger.info if status == "completed" else logger.warning
            log(
                "Insight job %s step after: stage=%s status=%s error_code=%s duration_seconds=%.3f",
                job.id,
                active_stage,
                status,
                error_code,
                max(time.monotonic() - stage_started, 0.0),
            )
            active_stage = None

        def progress(stage: str, message: str) -> None:
            nonlocal active_stage, stage_started
            safe_stage = self._safe_stage(stage)
            if active_stage == safe_stage:
                finish_stage("completed")
                job.stage = stage
                job.message = message
                return
            if active_stage is not None:
                finish_stage("completed")
            job.stage = stage
            job.message = message
            active_stage = safe_stage
            stage_started = time.monotonic()
            logger.info("Insight job %s step before: stage=%s", job.id, safe_stage)

        try:
            async with asyncio.timeout(self._timeout * self._max_pending):
                async with self._semaphore:
                    job.state = "running"
                    logger.info(
                        "Insight job %s started: queue_seconds=%.3f",
                        job.id,
                        self._elapsed(job),
                    )
                    async with asyncio.timeout(self._timeout):
                        report = await self._runner(job.id, job.request, progress)
                    finish_stage("completed")
                    size = len(report.model_dump_json().encode("utf-8"))
                    if size > MAX_REPORT_BYTES:
                        raise InsightError(
                            "report_too_large",
                            "The generated report exceeds the supported size. Use fewer sources or audiences.",
                        )
                    self._reports[job.id] = StoredReport(
                        owner_hash=job.owner_hash,
                        report=report,
                        input_fingerprint=job.input_fingerprint,
                        expires_at=self._clock() + self._retention,
                        size=size,
                    )
                    job.state = report.status
                    job.stage = "complete"
                    job.message = (
                        "Your report is ready."
                        if report.status == "completed"
                        else "Your report is ready with the limitations shown below."
                    )
                    logger.info(
                        "Insight job %s completed: status=%s duration_seconds=%.3f "
                        "sources=%d facts=%d audiences=%d evidence_gaps=%d",
                        job.id,
                        report.status,
                        self._elapsed(job),
                        len(report.sources),
                        len(report.facts),
                        len(report.audiences),
                        len(report.evidence_gaps),
                    )
        except asyncio.CancelledError:
            finish_stage("cancelled", "cancelled")
            job.state = "cancelled"
            job.message = "Generation was cancelled. Provider requests already submitted may still incur cost."
            logger.warning(
                "Insight job %s cancelled: stage=%s duration_seconds=%.3f",
                job.id,
                self._safe_stage(job.stage),
                self._elapsed(job),
            )
            raise
        except TimeoutError:
            finish_stage("failed", "job_timeout")
            job.state = "failed"
            job.error_code = "job_timeout"
            job.error = "Generation reached its time limit. It was not restarted automatically."
            job.message = job.error
            logger.warning(
                "Insight job %s stopped: stage=%s code=job_timeout duration_seconds=%.3f",
                job.id,
                self._safe_stage(job.stage),
                self._elapsed(job),
            )
        except InsightError as error:
            finish_stage("failed", error.code)
            job.state = "failed"
            job.error_code = error.code
            job.error = error.public_message
            job.message = error.public_message
            job.source_outcomes = error.source_outcomes
            logger.warning(
                "Insight job %s stopped: stage=%s code=%s duration_seconds=%.3f",
                job.id,
                self._safe_stage(job.stage),
                error.code,
                self._elapsed(job),
            )
        finally:
            job.finished_at = self._clock()

    async def cancel(self, report_id: str, owner_hash: str) -> bool:
        job = self.get_job(report_id, owner_hash)
        if job is None:
            return False
        task = self._tasks.get(report_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return True

    def retry(self, report_id: str, owner_hash: str) -> Job:
        job = self.get_job(report_id, owner_hash)
        if job is None:
            raise InsightError("not_found", "This temporary job is no longer available.")
        if (
            job.state not in {"failed", "partial"}
            or self._can_retry is None
            or not self._can_retry(report_id)
        ):
            raise InsightError(
                "not_retryable",
                "This job cannot be retried. Update the sources or configuration and start a new generation.",
            )
        active = [entry for entry in self._jobs.values() if entry.state not in FINISHED_STATES]
        if (
            len(active) >= self._max_pending
            or sum(entry.owner_hash == owner_hash for entry in active) >= self._max_per_owner
        ):
            raise InsightError("active_job", "Another generation is already queued or running.")
        self._reserve_result(additional=0)
        job.state = "queued"
        job.stage = "queued"
        job.message = "Retry queued using the accepted work still in memory."
        job.error = None
        job.error_code = None
        job.finished_at = None
        self._reports.pop(report_id, None)
        task = asyncio.create_task(self._run(job), name=f"insights-{job.id}")
        self._tasks[job.id] = task
        task.add_done_callback(lambda finished: self._task_finished(job, finished))
        logger.info("Insight job %s retry queued", job.id)
        return job

    def retry_available(self, report_id: str, owner_hash: str) -> bool:
        job = self.get_job(report_id, owner_hash)
        return bool(
            job is not None
            and job.state in {"failed", "partial"}
            and self._can_retry is not None
            and self._can_retry(report_id)
        )

    def restore(self, cached: CachedReport) -> StoredReport:
        self._purge()
        report_id = cached.report.report_id
        existing = self._reports.get(report_id)
        if existing is not None:
            if existing.owner_hash != cached.owner_hash:
                raise InsightError("wrong_owner", "This report belongs to a different account.")
            return existing
        self._reserve_result()
        stored = StoredReport(
            owner_hash=cached.owner_hash,
            report=cached.report,
            input_fingerprint=cached.input_fingerprint,
            expires_at=min(self._clock() + self._retention, cached.expires_at),
            size=len(cached.report.model_dump_json().encode("utf-8")),
        )
        self._reports[report_id] = stored
        return stored

    async def delete(self, report_id: str, owner_hash: str) -> bool:
        found = self.get_report(report_id, owner_hash) is not None
        if self.get_job(report_id, owner_hash) is not None:
            await self.cancel(report_id, owner_hash)
            self._jobs.pop(report_id, None)
            found = True
        if found:
            self._reports.pop(report_id, None)
            if self._forget is not None:
                self._forget(report_id)
        return found

    async def close(self) -> None:
        self._closing = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        if self._forget is not None:
            for report_id in self._jobs:
                self._forget(report_id)
        self._jobs.clear()
        self._reports.clear()
