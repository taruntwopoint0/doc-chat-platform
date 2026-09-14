"""Background worker: an asyncio task in the API process.

One instance, so this is a loop over the Postgres queue rather than a broker.
`worker_concurrency` runs several of these loops side by side; SKIP LOCKED keeps
them off each other's jobs.

Swapping to Celery means replacing this loop and app/workers/queue.py. The
pipeline it calls does not change.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid

from app.config import get_settings
from app.interfaces.types import DocumentStatus
from app.models.ingest_job import IngestJob
from app.pipeline import persist
from app.pipeline.ingest import run_job
from app.workers import queue

logger = logging.getLogger(__name__)

#: How often a running job refreshes its heartbeat, so a stalled job is
#: distinguishable from a slow one.
_HEARTBEAT_SECONDS = 15


async def _heartbeat_loop(job_id: uuid.UUID) -> None:
    while True:
        await asyncio.sleep(_HEARTBEAT_SECONDS)
        try:
            await queue.heartbeat(job_id)
        except Exception as exc:  # pragma: no cover - transient db blip
            logger.warning("Heartbeat failed for job %s: %s", job_id, exc)


async def process(job: IngestJob) -> None:
    """Run one job, recording success or failure against it."""
    settings = get_settings()
    beat = asyncio.create_task(_heartbeat_loop(job.id))
    try:
        await run_job(job)
        await queue.finish(job.id)
    except asyncio.CancelledError:
        # Shutting down mid-job: leave it running-with-no-heartbeat so
        # reclaim_stalled returns it to the queue, checkpoint intact.
        logger.info("Job %s interrupted by shutdown; it will be reclaimed", job.id)
        raise
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("Job %s failed: %s", job.id, message)
        await queue.release(job.id, message, settings.job_max_attempts)
        # The document is only marked failed once the job has no retries left.
        refreshed = await _job_status(job.id)
        if refreshed == "failed":
            await persist.set_document_status(
                job.document_id, DocumentStatus.FAILED, error_message=message
            )
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat


async def _job_status(job_id: uuid.UUID) -> str | None:
    from sqlalchemy import select

    from app.db import session_scope

    async with session_scope() as session:
        return (
            await session.execute(
                select(IngestJob.status).where(IngestJob.id == job_id)
            )
        ).scalar_one_or_none()


async def worker_loop(worker_id: str) -> None:
    settings = get_settings()
    logger.info("Worker %s started", worker_id)
    while True:
        try:
            job = await queue.claim(worker_id)
            if job is None:
                await asyncio.sleep(settings.worker_poll_seconds)
                continue
            await process(job)
        except asyncio.CancelledError:
            logger.info("Worker %s stopping", worker_id)
            raise
        except Exception as exc:
            # Never let a loop-level error kill the worker; back off and retry.
            logger.exception("Worker %s loop error: %s", worker_id, exc)
            await asyncio.sleep(settings.worker_poll_seconds)


async def reclaim_loop() -> None:
    """Periodically return jobs abandoned by a dead process to the queue."""
    settings = get_settings()
    interval = max(30, settings.worker_job_timeout_seconds // 4)
    while True:
        try:
            await queue.reclaim_stalled(settings.worker_job_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - transient db blip
            logger.warning("Stalled-job sweep failed: %s", exc)
        await asyncio.sleep(interval)


class WorkerPool:
    """Owns the worker tasks for the process lifetime."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        settings = get_settings()
        if not settings.worker_enabled:
            logger.info("Worker disabled by WORKER_ENABLED=false")
            return
        # A restart leaves jobs marked running with no one running them.
        await queue.reclaim_stalled(0)
        prefix = f"{os.getpid()}"
        for index in range(max(1, settings.worker_concurrency)):
            self._tasks.append(
                asyncio.create_task(
                    worker_loop(f"{prefix}-{index}"), name=f"ingest-worker-{index}"
                )
            )
        self._tasks.append(asyncio.create_task(reclaim_loop(), name="ingest-reclaim"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
