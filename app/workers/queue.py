"""Postgres-backed job queue.

Claiming uses `SELECT ... FOR UPDATE SKIP LOCKED`, which lets several workers
share one table without blocking each other and without a broker. Render's
Starter tier is a single instance, so Redis and Celery would be two more moving
parts for no benefit.

Moving to Celery later means reimplementing `enqueue` and `claim` here. Nothing
outside this module knows how work is distributed.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_scope
from app.interfaces.types import DocumentStatus, JobStatus
from app.models.ingest_job import IngestJob

logger = logging.getLogger(__name__)


async def enqueue(
    session: AsyncSession, workspace_id: UUID, document_id: UUID
) -> IngestJob:
    job = IngestJob(
        id=uuid4(),
        workspace_id=workspace_id,
        document_id=document_id,
        status=JobStatus.PENDING,
        stage=DocumentStatus.PENDING,
        progress_pct=0,
        checkpoint={},
    )
    session.add(job)
    await session.flush()
    return job


async def claim(worker_id: str) -> IngestJob | None:
    """Take the oldest pending job, or None.

    The claim is a single statement so two workers can never take the same row:
    SKIP LOCKED makes the loser move to the next row instead of waiting.
    """
    async with session_scope() as session:
        candidate = (
            select(IngestJob.id)
            .where(IngestJob.status == JobStatus.PENDING)
            .order_by(IngestJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        result = await session.execute(
            update(IngestJob)
            .where(IngestJob.id == candidate)
            .values(
                status=JobStatus.RUNNING,
                attempts=IngestJob.attempts + 1,
                started_at=func.now(),
                heartbeat_at=func.now(),
                error_message=None,
            )
            .returning(IngestJob)
        )
        job = result.scalar_one_or_none()
        if job is not None:
            logger.info("Worker %s claimed job %s", worker_id, job.id)
        return job


async def heartbeat(job_id: UUID) -> None:
    async with session_scope() as session:
        await session.execute(
            update(IngestJob)
            .where(IngestJob.id == job_id)
            .values(heartbeat_at=func.now())
        )


async def update_progress(
    job_id: UUID,
    stage: str | None = None,
    progress_pct: int | None = None,
    checkpoint: dict | None = None,
) -> None:
    """Record a stage transition so `GET /api/jobs/{id}` means something."""
    values: dict = {"heartbeat_at": func.now()}
    if stage is not None:
        values["stage"] = stage
    if progress_pct is not None:
        values["progress_pct"] = max(0, min(100, int(progress_pct)))
    if checkpoint is not None:
        values["checkpoint"] = checkpoint
    async with session_scope() as session:
        await session.execute(
            update(IngestJob).where(IngestJob.id == job_id).values(**values)
        )


async def finish(job_id: UUID, error: str | None = None) -> None:
    async with session_scope() as session:
        await session.execute(
            update(IngestJob)
            .where(IngestJob.id == job_id)
            .values(
                status=JobStatus.FAILED if error else JobStatus.SUCCEEDED,
                stage=DocumentStatus.FAILED if error else DocumentStatus.READY,
                progress_pct=100 if not error else IngestJob.progress_pct,
                error_message=error,
                finished_at=func.now(),
            )
        )


async def release(job_id: UUID, error: str, max_attempts: int) -> None:
    """Hand a failed job back to the queue, or fail it for good.

    The checkpoint is left intact so the retry resumes where the run stopped.
    """
    async with session_scope() as session:
        job = (
            await session.execute(select(IngestJob).where(IngestJob.id == job_id))
        ).scalar_one_or_none()
        if job is None:
            return
        if job.attempts >= max_attempts:
            job.status = JobStatus.FAILED
            job.stage = DocumentStatus.FAILED
            job.error_message = error
            job.finished_at = func.now()
            logger.error("Job %s failed permanently: %s", job_id, error)
        else:
            job.status = JobStatus.PENDING
            job.error_message = error
            job.heartbeat_at = None
            logger.warning(
                "Job %s returned to the queue after attempt %d: %s",
                job_id,
                job.attempts,
                error,
            )


async def reclaim_stalled(timeout_seconds: int) -> int:
    """Return jobs whose worker died back to the queue.

    A job is stalled when it is still marked running but its heartbeat has gone
    quiet -- the signature of a process killed mid-run, which on Render happens
    on every deploy.
    """
    # The cutoff is computed by the database, not by Python. Heartbeats are
    # written with the database's now(), so comparing them against the app
    # server's clock would be comparing two clocks -- which on Render are two
    # machines. Any skew makes reclaiming either miss jobs or fire early.
    cutoff = func.now() - func.make_interval(0, 0, 0, 0, 0, 0, timeout_seconds)
    async with session_scope() as session:
        result = await session.execute(
            update(IngestJob)
            .where(
                IngestJob.status == JobStatus.RUNNING,
                func.coalesce(IngestJob.heartbeat_at, IngestJob.started_at) < cutoff,
            )
            .values(status=JobStatus.PENDING, heartbeat_at=None)
            .returning(IngestJob.id)
        )
        ids = [row[0] for row in result.all()]
    if ids:
        logger.warning("Reclaimed %d stalled job(s): %s", len(ids), ids)
    return len(ids)


async def pending_count() -> int:
    async with session_scope() as session:
        result = await session.execute(
            select(func.count())
            .select_from(IngestJob)
            .where(IngestJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]))
        )
        return int(result.scalar_one())
