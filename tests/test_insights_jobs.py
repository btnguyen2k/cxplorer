"""In-memory job ownership, capacity, retry, expiry, and shutdown."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from cxplorer.insights.errors import InsightError
from cxplorer.insights.jobs import JobManager
from cxplorer.insights.schemas import CachedReport, InsightRequest, SeedInput
from tests.insights_fixtures import report

OWNER = "a" * 64
OTHER = "b" * 64
FINGERPRINT = "c" * 64


def trusted_request() -> InsightRequest:
    # Source validation has separate coverage; isolate the manager from network components.
    seed = SeedInput.model_construct(url="https://contoso.example", purpose="homepage")
    return InsightRequest(seeds=[seed], audiences=["ceo"])


async def finished(manager: JobManager, report_id: str) -> None:
    async with asyncio.timeout(2):
        while manager.get_job(report_id, OWNER).finished_at is None:
            await asyncio.sleep(0.001)


def test_job_deduplication_and_owner_boundaries() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        calls = []

        async def runner(report_id, request, progress):
            calls.append(report_id)
            progress("verifying", "Checking company sources.")
            await release.wait()
            return report(report_id)

        manager = JobManager(runner)
        job = manager.submit(OWNER, trusted_request(), "submission", FINGERPRINT)
        assert manager.submit(OWNER, trusted_request(), "submission", FINGERPRINT) is job
        with pytest.raises(InsightError, match="different inputs"):
            manager.submit(OWNER, trusted_request(), "submission", "d" * 64)
        with pytest.raises(InsightError, match="existing generation"):
            manager.submit(OWNER, trusted_request(), "second", FINGERPRINT)
        assert manager.get_job(job.id, OTHER) is None
        assert not await manager.cancel(job.id, OTHER)
        release.set()
        await finished(manager, job.id)
        assert calls == [job.id]
        assert manager.get_report(job.id, OWNER).report.status == "completed"
        assert manager.get_report(job.id, OTHER) is None
        await manager.close()

    asyncio.run(scenario())


def test_job_lifecycle_logs_safe_progress_without_request_content(caplog) -> None:
    private_message = "private Contoso source details must not be logged"

    async def scenario() -> str:
        async def runner(report_id, request, progress):
            progress("preflight", private_message)
            progress("verify_sources", private_message)
            return report(report_id)

        manager = JobManager(runner)
        job = manager.submit(OWNER, trusted_request(), "submission", FINGERPRINT)
        await finished(manager, job.id)
        await manager.close()
        return job.id

    with caplog.at_level("INFO", logger="cxplorer.insights.jobs"):
        job_id = asyncio.run(scenario())
    messages = [record.getMessage() for record in caplog.records]
    assert f"Insight job {job_id} queued" in messages
    assert any(f"Insight job {job_id} started: queue_seconds=" in message for message in messages)
    assert f"Insight job {job_id} step before: stage=preflight" in messages
    assert any(
        f"Insight job {job_id} step after: stage=preflight status=completed" in message
        for message in messages
    )
    assert f"Insight job {job_id} step before: stage=verify_sources" in messages
    assert any(
        f"Insight job {job_id} step after: stage=verify_sources status=completed" in message
        for message in messages
    )
    assert any(
        f"Insight job {job_id} completed: status=completed" in message for message in messages
    )
    assert private_message not in caplog.text
    assert OWNER not in caplog.text
    assert "contoso.example" not in caplog.text


def test_queue_and_shutdown_are_bounded() -> None:
    async def scenario() -> None:
        async def runner(report_id, request, progress):
            await asyncio.Event().wait()

        manager = JobManager(runner, max_running=1, max_queued=1)
        first = manager.submit(OWNER, trusted_request(), "first", FINGERPRINT)
        second = manager.submit(OTHER, trusted_request(), "second", FINGERPRINT)
        with pytest.raises(InsightError, match="busy"):
            manager.submit("d" * 64, trusted_request(), "third", FINGERPRINT)
        await manager.cancel(second.id, OTHER)
        assert second.state == "cancelled"
        await manager.close()
        assert manager.get_job(first.id, OWNER) is None
        with pytest.raises(InsightError, match="restarts"):
            manager.submit(OWNER, trusted_request(), "later", FINGERPRINT)

    asyncio.run(scenario())


def test_failed_jobs_cannot_grow_without_bound() -> None:
    async def scenario() -> None:
        async def runner(report_id, request, progress):
            raise InsightError("source_unavailable", "A company source is unavailable.")

        manager = JobManager(runner, retained_limit=2)
        first = manager.submit(OWNER, trusted_request(), "one", FINGERPRINT)
        await finished(manager, first.id)
        second = manager.submit(OWNER, trusted_request(), "two", FINGERPRINT)
        await finished(manager, second.id)
        with pytest.raises(InsightError, match="capacity"):
            manager.submit(OWNER, trusted_request(), "three", FINGERPRINT)
        await manager.close()

    asyncio.run(scenario())


def test_expiry_forgets_checkpoints_and_restored_reports() -> None:
    async def scenario() -> None:
        now = [datetime.now(UTC)]
        forgotten = []

        async def runner(report_id, request, progress):
            return report(report_id, generated_at=now[0])

        manager = JobManager(
            runner, retention_seconds=10, clock=lambda: now[0], forget=forgotten.append
        )
        job = manager.submit(OWNER, trusted_request(), "one", FINGERPRINT)
        await finished(manager, job.id)
        cached = CachedReport(
            owner_hash=OWNER,
            input_fingerprint=FINGERPRINT,
            expires_at=now[0] + timedelta(days=7),
            report=report("d" * 32, generated_at=now[0]),
        )
        manager.restore(cached)
        now[0] += timedelta(seconds=11)
        assert manager.get_report(job.id, OWNER) is None
        assert manager.get_report(cached.report.report_id, OWNER) is None
        assert manager.get_job(job.id, OWNER) is None
        assert forgotten == [job.id]
        await manager.close()

    asyncio.run(scenario())


def test_eligible_retry_reuses_the_job_and_rejects_other_owners() -> None:
    async def scenario() -> None:
        calls = []

        async def runner(report_id, request, progress):
            calls.append(report_id)
            if len(calls) == 1:
                raise InsightError("provider_timeout", "The AI request timed out.")
            return report(report_id)

        manager = JobManager(runner, can_retry=lambda report_id: True)
        job = manager.submit(OWNER, trusted_request(), "one", FINGERPRINT)
        await finished(manager, job.id)
        assert manager.retry_available(job.id, OWNER)
        with pytest.raises(InsightError, match="no longer available"):
            manager.retry(job.id, OTHER)
        assert manager.retry(job.id, OWNER) is job
        await finished(manager, job.id)
        assert calls == [job.id, job.id]
        assert manager.get_report(job.id, OWNER) is not None
        await manager.close()

    asyncio.run(scenario())


def test_processing_deadline_and_unexpected_failure_are_visible(caplog) -> None:
    async def scenario() -> None:
        async def slow(report_id, request, progress):
            progress("model_wait", "Waiting for a private Contoso model response.")
            await asyncio.sleep(1)

        manager = JobManager(slow, timeout_seconds=0.01)
        job = manager.submit(OWNER, trusted_request(), "slow", FINGERPRINT)
        await finished(manager, job.id)
        assert job.state == "failed"
        assert job.error_code == "job_timeout"
        await manager.close()

        async def broken(report_id, request, progress):
            raise RuntimeError("internal details must not be shown")

        manager = JobManager(broken)
        job = manager.submit(OWNER, trusted_request(), "broken", FINGERPRINT)
        await finished(manager, job.id)
        await asyncio.sleep(0)
        assert job.state == "failed"
        assert "internal details" not in job.message
        await manager.close()

    asyncio.run(scenario())
    assert "step after: stage=model_wait status=failed error_code=job_timeout" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "internal details" not in caplog.text
